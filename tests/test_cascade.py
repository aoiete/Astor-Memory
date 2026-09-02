"""
Tests for the cascade write queue (2026-08-16 v1.2.0 ship).

Pattern: simulate nest.store() failure, verify row lands in cascade_state,
verify replay endpoint drains it.

These tests use a temp ASTOR_DIR + tmp_path for hermes_home isolation.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from astor_memory._internal.acl_layout import get_astor_dir, get_db_path, Tier
from astor_memory.bus import cascade
from astor_memory.bus.schema import astor_init_schema
from astor_memory.bus.store import astor_bus
from astor_memory._internal.acl import astor_init_acl


@pytest.fixture
def fresh_bus(tmp_path, monkeypatch):
    """Fresh ASTOR_DIR with one canonical row we can attempt to cascade."""
    target = tmp_path / "astor"
    monkeypatch.setenv("ASTOR_DIR", str(target))
    # Pre-create public bus DB + init schema (cascade_state table gets created here).
    p = get_db_path(Tier.PUBLIC.value, "bus", None)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=5)
    astor_init_schema(conn)
    conn.close()
    # Direct astor_bus() calls need an ACL context (the production server
    # binds it in before_request; tests bypass Flask and call directly).
    astor_init_acl(actor='admin:admin', role='admin', tier='public')
    bus = astor_bus(tier=Tier.PUBLIC.value, user_id=None)
    return bus, target


def _insert_one_canonical(bus) -> int:
    """Insert one canonical row by going through append_event + insert_candidate + promote_candidate.

    This produces a real canonical row with proper candidate_id FK + stable_id.
    """
    event_id = bus.append_event(
        namespace='/test/cascade',
        agent_id='pytest',
        source='rest_api',
        action='write',
        content='health-check cascade test fact',
        metadata={},
    )
    candidate_id = bus.insert_candidate(
        event_id=event_id,
        namespace='/test/cascade',
        content='health-check cascade test fact',
        kind='fact',
    )
    return bus.promote_candidate(
        candidate_id=candidate_id,
        promoted_by='pytest',
        user_id=None,
        tier='public',
        scope_type='long_term',
    )


def test_cascade_enqueue_creates_pending_row(fresh_bus):
    """enqueue() inserts a row with status=pending + payload + last_error."""
    bus, _ = fresh_bus
    fact_id = _insert_one_canonical(bus)

    cid = cascade.enqueue(
        bus,
        fact_id=fact_id,
        operation='embed_insert',
        tier='public',
        user_id=None,
        payload={'content': 'health-check cascade test fact'},
        error='embedding model OOM (simulated)',
    )
    assert cid > 0

    # Verify row exists with correct fields.
    row = bus.conn.execute(
        "SELECT fact_id, operation, tier, user_id, status, last_error, payload "
        "FROM cascade_state WHERE id = ?", (cid,)
    ).fetchone()
    assert row is not None
    assert row[0] == fact_id
    assert row[1] == 'embed_insert'
    assert row[2] == 'public'
    assert row[3] is None
    assert row[4] == 'pending'
    assert 'OOM' in row[5]
    import json as _json
    assert _json.loads(row[6])['content'] == 'health-check cascade test fact'


def test_cascade_stats_counts_by_status(fresh_bus):
    """stats() returns {pending, succeeded, failed, last_attempt_at}."""
    bus, _ = fresh_bus
    f1 = _insert_one_canonical(bus)
    f2 = _insert_one_canonical(bus)

    cascade.enqueue(bus, f1, 'embed_insert', 'public', None, {}, 'e1')
    cid2 = cascade.enqueue(bus, f2, 'embed_insert', 'public', None, {}, 'e2')
    # Force one to succeeded
    bus.conn.execute("UPDATE cascade_state SET status='succeeded' WHERE id=?", (cid2,))
    bus.conn.commit()

    s = cascade.stats(bus)
    assert s['pending'] == 1
    assert s['succeeded'] == 1
    assert s['failed'] == 0
    assert s['last_attempt_at'] is not None


def test_cascade_list_pending_returns_fifo_order(fresh_bus):
    """list_pending returns rows in enqueue order (FIFO)."""
    bus, _ = fresh_bus
    f1 = _insert_one_canonical(bus)
    f2 = _insert_one_canonical(bus)
    f3 = _insert_one_canonical(bus)

    cid1 = cascade.enqueue(bus, f1, 'embed_insert', 'public', None, {}, 'e1')
    time.sleep(0.01)  # ensure enqueued_at differs
    cid2 = cascade.enqueue(bus, f2, 'embed_insert', 'public', None, {}, 'e2')
    time.sleep(0.01)
    cid3 = cascade.enqueue(bus, f3, 'embed_insert', 'public', None, {}, 'e3')

    pending = cascade.list_pending(bus, limit=10)
    assert [p['id'] for p in pending] == [cid1, cid2, cid3]
    assert all(p['status'] == 'pending' for p in pending)


def test_cascade_replay_one_succeeds_and_flips_status(fresh_bus, monkeypatch):
    """replay_one() calls nest.store, on success flips status=succeeded."""
    bus, _ = fresh_bus
    fact_id = _insert_one_canonical(bus)

    cid = cascade.enqueue(
        bus, fact_id, 'embed_insert', 'public', None,
        {'content': 'health-check cascade test fact'}, 'fake-failure',
    )

    # Don't actually need a working embedding model — mock nest.
    from astor_memory import nest as _nest_mod
    class _FakeNest:
        def __init__(self):
            self.calls = []
        def store(self, fid, content):
            self.calls.append((fid, content))

    fake = _FakeNest()
    # monkeypatch astor_nest to return fake
    monkeypatch.setattr(_nest_mod, 'astor_nest', lambda tier, user_id=None: fake)

    out = cascade.replay_one(bus, cid)
    assert out['ok'] is True
    assert out['status'] == 'succeeded'
    assert out['attempt_count'] == 1
    assert fake.calls == [(fact_id, 'health-check cascade test fact')]

    # DB confirms status flip.
    row = bus.conn.execute(
        "SELECT status, attempt_count, last_error FROM cascade_state WHERE id=?",
        (cid,)).fetchone()
    assert row[0] == 'succeeded'
    assert row[1] == 1
    assert row[2] is None


def test_cascade_replay_one_failure_keeps_pending_until_max_attempts(fresh_bus, monkeypatch):
    """replay_one() on failure increments attempt_count, status stays pending
    until attempt_count >= max_attempts, then flips to 'failed'."""
    bus, _ = fresh_bus
    fact_id = _insert_one_canonical(bus)

    cid = cascade.enqueue(
        bus, fact_id, 'embed_insert', 'public', None,
        {'content': 'will keep failing'}, 'first-failure',
    )

    from astor_memory import nest as _nest_mod
    def boom(tier, user_id=None):
        raise RuntimeError('embed model still down')

    monkeypatch.setattr(_nest_mod, 'astor_nest', boom)

    # Replay up to max_attempts=2; first replay stays pending (attempt=1),
    # second replay flips to failed (attempt=2 >= max_attempts).
    out1 = cascade.replay_one(bus, cid, max_attempts=2)
    assert out1['ok'] is False
    assert out1['status'] == 'pending'
    assert out1['attempt_count'] == 1
    assert 'still down' in out1['error']

    out2 = cascade.replay_one(bus, cid, max_attempts=2)
    assert out2['ok'] is False
    assert out2['status'] == 'failed'
    assert out2['attempt_count'] == 2

    # Confirm DB state.
    row = bus.conn.execute(
        "SELECT status, attempt_count FROM cascade_state WHERE id=?",
        (cid,)).fetchone()
    assert row[0] == 'failed'
    assert row[1] == 2


def test_cascade_replay_pending_processes_multiple(fresh_bus, monkeypatch):
    """replay_pending() drains N rows in one call, returns summary."""
    bus, _ = fresh_bus
    fact_ids = [_insert_one_canonical(bus) for _ in range(5)]
    for fid in fact_ids:
        cascade.enqueue(bus, fid, 'embed_insert', 'public', None,
                         {'content': f'fact-{fid}'}, 'sim-failure')

    from astor_memory import nest as _nest_mod
    fake_calls = []
    class FakeNest:
        def store(self, fid, content):
            fake_calls.append((fid, content))
    monkeypatch.setattr(_nest_mod, 'astor_nest', lambda tier, user_id=None: FakeNest())

    out = cascade.replay_pending(bus, limit=10)
    assert out['processed'] == 5
    assert out['succeeded'] == 5
    assert out['failed'] == 0
    assert out['still_pending'] == 0
    assert len(fake_calls) == 5


def test_cascade_purge_clears_old_succeeded_rows(fresh_bus):
    """purge() deletes rows older than N days for given status."""
    bus, _ = fresh_bus
    fid = _insert_one_canonical(bus)
    cid = cascade.enqueue(bus, fid, 'embed_insert', 'public', None, {}, 'e')
    bus.conn.execute("UPDATE cascade_state SET status='succeeded', "
                     "enqueued_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-10 days') WHERE id=?",
                     (cid,))
    bus.conn.commit()

    # Purge older_than_days=7: should delete the 10-day-old succeeded row.
    deleted = cascade.purge(bus, status='succeeded', older_than_days=7)
    assert deleted == 1

    # Verify gone.
    row = bus.conn.execute("SELECT id FROM cascade_state WHERE id=?", (cid,)).fetchone()
    assert row is None


def test_cascade_purge_does_not_touch_pending(fresh_bus):
    """purge() with status='succeeded' never deletes pending rows."""
    bus, _ = fresh_bus
    fid = _insert_one_canonical(bus)
    cid = cascade.enqueue(bus, fid, 'embed_insert', 'public', None, {}, 'e')

    deleted = cascade.purge(bus, status='succeeded', older_than_days=0)
    assert deleted == 0

    row = bus.conn.execute("SELECT status FROM cascade_state WHERE id=?", (cid,)).fetchone()
    assert row[0] == 'pending'


def test_promote_candidate_enqueues_on_embed_failure(tmp_path, monkeypatch):
    """promote_candidate() with broken embedding model still writes canonical
    row + enqueues a cascade_state row for later replay."""
    from astor_memory._internal.acl_layout import (
        get_astor_dir, get_db_path, Tier, Store,
    )
    target = tmp_path / "astor_promote"
    monkeypatch.setenv("ASTOR_DIR", str(target))
    # Pre-create public bus + nest DBs.
    for store_name in ('bus', 'nest', 'forge'):
        p = get_db_path(Tier.PUBLIC.value, store_name, None)
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p), timeout=5)
        astor_init_schema(conn) if store_name == 'bus' else None
        conn.close()

    # Tests that call astor_bus() directly bypass Flask's before_request,
    # so bind ACL first.
    astor_init_acl(actor='admin:admin', role='admin', tier='public')
    bus = astor_bus(tier=Tier.PUBLIC.value, user_id=None)


    from astor_memory import nest as _nest_mod
    monkeypatch.setattr(
        _nest_mod, 'astor_nest',
        lambda tier, user_id=None: type('Boom', (), {
            'store': staticmethod(lambda fid, content: (_ for _ in ()).throw(
                RuntimeError('embedding OOM (simulated)')))
        })()
    )

    # Insert a candidate, then promote.
    event_id = bus.append_event(
        namespace='/test/cascade',
        agent_id='pytest',
        source='rest_api',
        action='write',
        content='promote cascade enqueue test',
        metadata={},
    )
    candidate_id = bus.insert_candidate(
        event_id=event_id,
        namespace='/test/cascade',
        content='promote cascade enqueue test',
        kind='fact',
    )
    canonical_id = bus.promote_candidate(
        candidate_id=candidate_id,
        promoted_by='pytest',
        user_id=None,
        tier='public',
        scope_type='long_term',
    )
    assert canonical_id > 0

    # canonical row exists.
    row = bus.conn.execute(
        "SELECT content, tombstoned FROM memory_canonical WHERE id=?",
        (canonical_id,)).fetchone()
    assert row is not None
    assert 'promote cascade enqueue test' in row[0]
    assert row[1] == 0  # not tombstoned

    # cascade_state row exists with status=pending.
    rows = bus.conn.execute(
        "SELECT fact_id, operation, status FROM cascade_state WHERE fact_id=?",
        (canonical_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == canonical_id
    assert rows[0][1] == 'embed_insert'
    assert rows[0][2] == 'pending'

    # audit_log row exists with metadata.queued_for_replay=True
    audit = bus.conn.execute(
        "SELECT metadata FROM audit_log WHERE event='embedding_failed' AND target_id=?",
        (str(canonical_id),)).fetchall()
    assert len(audit) >= 1
    import json as _json
    meta = _json.loads(audit[0][0])
    assert meta.get('queued_for_replay') is True


def test_cascade_via_server_endpoint(tmp_path, monkeypatch):
    """POST /v1/cascade/replay returns summary; GET /v1/cascade/stats returns counts."""
    target = tmp_path / "astor_server"
    monkeypatch.setenv("ASTOR_DIR", str(target))
    from astor_memory._internal.acl_layout import (
        get_db_path, Tier,
    )
    p = get_db_path(Tier.PUBLIC.value, "bus", None)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=5)
    astor_init_schema(conn)
    conn.close()

    # Seed admin user in bot-binding.db so before_request binds first_admin.
    from astor_memory._internal import bot_binding as bb
    monkeypatch.setattr(bb, '_con', None)
    bb.upsert_user(user_id='admin', short_alias='admin', role='admin',
                   subscription_plan='power')

    # Tests that call astor_bus() directly bypass Flask's before_request,
    # so bind ACL first.
    astor_init_acl(actor='admin:admin', role='admin', tier='public')
    # Insert a fact + enqueue a cascade row.
    bus = astor_bus(tier=Tier.PUBLIC.value, user_id=None)
    fid = _insert_one_canonical(bus)
    cascade.enqueue(bus, fid, 'embed_insert', 'public', None,
                     {'content': 'via server'}, 'e')

    from astor_memory.server import create_app
    app = create_app(str(target))
    client = app.test_client()

    # 1. stats shows 1 pending
    r = client.get('/v1/cascade/stats')
    assert r.status_code == 200
    body = r.get_json()
    assert body['pending'] == 1

    # 2. replay as first_admin (admin user has first_admin role via upsert)
    from astor_memory import nest as _nest_mod
    monkeypatch.setattr(
        _nest_mod, 'astor_nest',
        lambda tier, user_id=None: type('OK', (), {
            'store': staticmethod(lambda fid, content: None)
        })()
    )

    r = client.post('/v1/cascade/replay', json={'limit': 5})
    assert r.status_code == 200
    body = r.get_json()
    assert body['succeeded'] == 1
    assert body['failed'] == 0

    # 3. stats now shows 0 pending, 1 succeeded
    r = client.get('/v1/cascade/stats')
    body = r.get_json()
    assert body['pending'] == 0
    assert body['succeeded'] == 1


def test_cascade_server_endpoint_requires_first_admin(tmp_path, monkeypatch):
    """POST /v1/cascade/replay from non-first_admin returns 403."""
    target = tmp_path / "astor_acl"
    monkeypatch.setenv("ASTOR_DIR", str(target))
    from astor_memory._internal.acl_layout import get_db_path, Tier
    p = get_db_path(Tier.PUBLIC.value, "bus", None)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=5)
    astor_init_schema(conn)
    conn.close()

    # Seed a regular user (not first_admin).
    from astor_memory._internal import bot_binding as bb
    monkeypatch.setattr(bb, '_con', None)
    bb.upsert_user(user_id='alice', short_alias='alice', role='user',
                   subscription_plan='vip')

    # Tests that call astor_bus() directly bypass Flask's before_request,
    # so bind ACL first (admin role for this test — alice's role check
    # happens inside the server's before_request).
    astor_init_acl(actor='admin:admin', role='admin', tier='public')
    bus = astor_bus(tier=Tier.PUBLIC.value, user_id=None)
    fid = _insert_one_canonical(bus)
    cascade.enqueue(bus, fid, 'embed_insert', 'public', None, {}, 'e')

    from astor_memory.server import create_app
    app = create_app(str(target))
    client = app.test_client()
    r = client.post('/v1/cascade/replay',
                    json={'user': 'alice', 'tier': 'public', 'limit': 5})
    assert r.status_code == 403
