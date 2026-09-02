"""
Server endpoint tests for reflection. Kept separate from test_reflection.py
to avoid fixture complexity.
"""
import os
import sqlite3

import pytest

from astor_memory._internal.acl import astor_init_acl
from astor_memory._internal import bot_binding as bb
from astor_memory._internal.acl_layout import get_db_path, Tier
from astor_memory.bus.schema import astor_init_schema
from astor_memory.bus.store import astor_bus
from astor_memory.server import create_app


def _insert_canonical(bus, *, content, kind='fact', importance=0.5, confidence=0.7):
    event_id = bus.append_event(
        namespace='/test/refl', agent_id='pytest', source='rest',
        action='write', content=content, metadata={},
    )
    cand_id = bus.insert_candidate(
        event_id=event_id, namespace='/test/refl',
        content=content, kind=kind, confidence=confidence, importance=importance,
    )
    return bus.promote_candidate(
        candidate_id=cand_id, promoted_by='pytest',
        user_id=None, tier='public', scope_type='long_term',
    )


@pytest.fixture
def env_and_seed(tmp_path, monkeypatch):
    target = tmp_path / "astor_refl_server"
    monkeypatch.setenv("ASTOR_DIR", str(target))
    # Pre-create public bus DB
    p = get_db_path(Tier.PUBLIC.value, "bus", None)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=5)
    astor_init_schema(conn)
    conn.close()
    # Bind ACL for direct bus calls
    astor_init_acl(actor='admin:admin', role='admin', tier='public')
    # Seed facts
    bus = astor_bus(tier=Tier.PUBLIC.value, user_id=None)
    _insert_canonical(bus, content='dark roast coffee morning focus', kind='user_preference')
    _insert_canonical(bus, content='dark roast coffee morning focus best',
                      kind='user_preference', importance=0.9)
    return target


def test_reflection_endpoint_first_admin(env_and_seed, monkeypatch):
    monkeypatch.setattr(bb, '_con', None)
    bb.upsert_user(user_id='admin', short_alias='admin', role='admin',
                   subscription_plan='power')
    app = create_app(os.environ['ASTOR_DIR'])
    client = app.test_client()
    r = client.post('/v1/reflection/run',
                    json={'user': 'admin', 'tier': 'public', 'min_size': 2})
    assert r.status_code == 200
    body = r.get_json()
    assert body['clusters_found'] == 1
    assert body['clusters_merged'] == 1


def test_reflection_endpoint_blocks_regular_user(env_and_seed, monkeypatch):
    monkeypatch.setattr(bb, '_con', None)
    bb.upsert_user(user_id='alice', short_alias='alice', role='user',
                   subscription_plan='vip')
    app = create_app(os.environ['ASTOR_DIR'])
    client = app.test_client()
    r = client.post('/v1/reflection/run',
                    json={'user': 'alice', 'tier': 'public', 'min_size': 2})
    assert r.status_code == 403
