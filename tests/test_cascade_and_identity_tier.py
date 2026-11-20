"""Two fixes that share one theme: a deleted fact must not come back, and a
declared tier must actually be used.

1. cascade.replay_one() only skipped a cascade row when the canonical row
   still existed with tombstoned=1. A HARD forget deletes the canonical row,
   so the lookup returned None -- which the code read as "not forgotten" --
   and the replay re-indexed the plaintext from cascade_state.payload,
   resurrecting the deleted content.

2. agent_identity.context_to_request_body() parsed default_tier off the
   local-direct profile but never put it on the wire, so a caller that
   declared a tier still had the server fall back to tier='public'.
"""
from __future__ import annotations

import sqlite3

import pytest

from astor_memory._internal.acl import astor_init_acl
from astor_memory._internal.acl_layout import get_db_path, Tier
from astor_memory.agent_identity import (
    context_to_request_body,
    parse_local_direct_profile,
)
from astor_memory.bus import cascade
from astor_memory.bus.schema import astor_init_schema
from astor_memory.bus.store import astor_bus


@pytest.fixture
def fresh_bus(tmp_path, monkeypatch):
    target = tmp_path / "astor"
    monkeypatch.setenv("ASTOR_DIR", str(target))
    p = get_db_path(Tier.PUBLIC.value, "bus", None)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=5)
    astor_init_schema(conn)
    conn.close()
    astor_init_acl(actor='admin:admin', role='admin', tier='public')
    bus = astor_bus(tier=Tier.PUBLIC.value, user_id=None)
    return bus, target


def _insert_one_canonical(bus) -> int:
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
        visibility='commons',
        scope_type='long_term',
    )


class TestHardDeletedFactIsNotResurrected:
    def test_replay_skips_a_fact_whose_row_is_gone(self, fresh_bus, monkeypatch):
        """A hard delete removes the canonical row; the replay must skip."""
        bus, _ = fresh_bus
        fact_id = _insert_one_canonical(bus)

        cid = cascade.enqueue(
            bus, fact_id, 'embed_insert', 'public', None,
            {'content': 'health-check cascade test fact'}, 'fake-failure',
        )

        # The hard forget: the canonical row is gone, not tombstoned.
        bus.conn.execute("DELETE FROM memory_canonical WHERE id = ?", (fact_id,))
        bus.conn.commit()

        # The nest must not be touched -- nothing may be re-indexed.
        from astor_memory import nest as _nest_mod

        class _FakeNest:
            def __init__(self):
                self.calls = []

            def store(self, fid, content):
                self.calls.append((fid, content))

        fake = _FakeNest()
        monkeypatch.setattr(_nest_mod, 'astor_nest',
                            lambda tier, user_id=None: fake)

        out = cascade.replay_one(bus, cid)

        assert out['ok'] is True
        assert out['status'] == cascade.STATUS_SKIPPED
        assert fake.calls == [], "a hard-deleted fact was re-indexed"

        row = bus.conn.execute(
            "SELECT status, last_error FROM cascade_state WHERE id=?", (cid,)
        ).fetchone()
        assert row[0] == cascade.STATUS_SKIPPED
        assert 'deleted' in (row[1] or '')

    def test_a_tombstoned_fact_is_still_skipped(self, fresh_bus, monkeypatch):
        """The pre-existing tombstone guard must keep working."""
        bus, _ = fresh_bus
        fact_id = _insert_one_canonical(bus)

        cid = cascade.enqueue(
            bus, fact_id, 'embed_insert', 'public', None,
            {'content': 'health-check cascade test fact'}, 'fake-failure',
        )
        bus.conn.execute(
            "UPDATE memory_canonical SET tombstoned = 1 WHERE id = ?", (fact_id,))
        bus.conn.commit()

        from astor_memory import nest as _nest_mod

        class _FakeNest:
            calls = []

            def store(self, fid, content):
                _FakeNest.calls.append((fid, content))

        fake = _FakeNest()
        monkeypatch.setattr(_nest_mod, 'astor_nest',
                            lambda tier, user_id=None: fake)

        out = cascade.replay_one(bus, cid)
        assert out['status'] == cascade.STATUS_SKIPPED
        assert fake.calls == []

    def test_a_live_fact_is_still_replayed(self, fresh_bus, monkeypatch):
        """The skip must not fire for a fact that is still there."""
        bus, _ = fresh_bus
        fact_id = _insert_one_canonical(bus)

        cid = cascade.enqueue(
            bus, fact_id, 'embed_insert', 'public', None,
            {'content': 'health-check cascade test fact'}, 'fake-failure',
        )

        from astor_memory import nest as _nest_mod

        class _FakeNest:
            def __init__(self):
                self.calls = []

            def store(self, fid, content):
                self.calls.append((fid, content))

        fake = _FakeNest()
        monkeypatch.setattr(_nest_mod, 'astor_nest',
                            lambda tier, user_id=None: fake)

        out = cascade.replay_one(bus, cid)
        assert out['status'] == 'succeeded'
        assert fake.calls == [(fact_id, 'health-check cascade test fact')]


class TestDeclaredTierReachesTheWire:
    def test_a_declared_default_tier_is_forwarded(self):
        ctx = parse_local_direct_profile({
            'user_id': 'admin',
            'agent_id': 'astor_memory_mcp',
            'default_tier': 'admin',
        })
        body = context_to_request_body(ctx)
        assert body['tier'] == 'admin'

    def test_no_declared_tier_means_no_tier_on_the_wire(self):
        ctx = parse_local_direct_profile()
        body = context_to_request_body(ctx)
        assert 'tier' not in body
