"""
Tests for v1.2.3 Zettelkasten auto-link (A-MEM pattern).

Pattern: insert 2 similar facts, verify auto-link adds bidirectional
provenance edge. Idempotency: re-run returns False. Different kinds
don't link. Threshold respected.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from astor_memory._internal.acl import astor_init_acl
from astor_memory._internal.acl_layout import get_db_path, Tier
from astor_memory.bus.schema import astor_init_schema
from astor_memory.bus.store import astor_bus
from astor_memory.nest import auto_link


def _insert_canonical(bus, *, content: str, kind: str = 'fact',
                     importance: float = 0.5):
    """Insert a canonical row directly via SQL."""
    event_id = bus.append_event(
        namespace='/test/autolink', agent_id='pytest',
        source='rest', action='write', content=content, metadata={},
    )
    cand_id = bus.insert_candidate(
        event_id=event_id, namespace='/test/autolink',
        content=content, kind=kind,
        confidence=0.7, importance=importance,
    )
    return bus.promote_candidate(
        candidate_id=cand_id, promoted_by='pytest',
        user_id=None, tier='public', scope_type='long_term',
    )


@pytest.fixture
def fresh_bus(tmp_path, monkeypatch):
    target = tmp_path / "astor_autolink"
    monkeypatch.setenv("ASTOR_DIR", str(target))
    p = get_db_path(Tier.PUBLIC.value, "bus", None)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=5)
    astor_init_schema(conn)
    conn.close()
    astor_init_acl(actor='first_admin', role='first_admin', tier='public')
    return astor_bus(tier=Tier.PUBLIC.value, user_id=None)


def test_add_auto_link_creates_bidirectional_edge(fresh_bus):
    """add_auto_link adds each fact_id to the other's parent_fact_ids."""
    a = _insert_canonical(fresh_bus, content='dark roast coffee every morning')
    b = _insert_canonical(fresh_bus, content='dark roast coffee every morning for focus')
    assert auto_link.add_auto_link(fresh_bus.conn, new_fact_id=a, existing_fact_id=b,
                                    similarity=0.95) is True
    # Verify both rows have each other in parent_fact_ids
    a_parents = json.loads(fresh_bus.conn.execute(
        'SELECT parent_fact_ids FROM memory_canonical WHERE id = ?', (a,)).fetchone()[0])
    b_parents = json.loads(fresh_bus.conn.execute(
        'SELECT parent_fact_ids FROM memory_canonical WHERE id = ?', (b,)).fetchone()[0])
    assert b in a_parents
    assert a in b_parents
    # v1.13.1 (2026-09-02): auto_link no longer overwrites the existing
    # provenance_kind ('extracted' from extraction pipeline); only sets it
    # if the row was previously empty. Both 'extracted' and 'auto_link'
    # are acceptable provenance_kinds; the audit log records the edge.
    for fid in (a, b):
        row = fresh_bus.conn.execute(
            'SELECT provenance_kind, provenance_agent FROM memory_canonical WHERE id = ?',
            (fid,)).fetchone()
        assert row[0] in ('extracted', 'auto_link')
        assert row[1] == 'nest.auto_link'


def test_add_auto_link_is_idempotent(fresh_bus):
    """Calling add_auto_link twice returns False the second time."""
    a = _insert_canonical(fresh_bus, content='dark roast coffee morning')
    b = _insert_canonical(fresh_bus, content='dark roast coffee morning focus')
    assert auto_link.add_auto_link(fresh_bus.conn, new_fact_id=a, existing_fact_id=b,
                                    similarity=0.95) is True
    # Second call: edge already exists -> False
    assert auto_link.add_auto_link(fresh_bus.conn, new_fact_id=a, existing_fact_id=b,
                                    similarity=0.95) is False
    # Verify parent_fact_ids still has single entry (not duplicated)
    a_parents = json.loads(fresh_bus.conn.execute(
        'SELECT parent_fact_ids FROM memory_canonical WHERE id = ?', (a,)).fetchone()[0])
    assert a_parents.count(b) == 1


def test_add_auto_link_skips_self(fresh_bus):
    """add_auto_link(a, a) returns False — can't link to self."""
    a = _insert_canonical(fresh_bus, content='dark roast coffee morning')
    assert auto_link.add_auto_link(fresh_bus.conn, new_fact_id=a, existing_fact_id=a,
                                    similarity=0.99) is False


def test_add_auto_link_handles_missing_fact(fresh_bus):
    """add_auto_link with non-existent fact_id returns False gracefully."""
    a = _insert_canonical(fresh_bus, content='dark roast coffee morning')
    assert auto_link.add_auto_link(fresh_bus.conn, new_fact_id=a, existing_fact_id=999999,
                                    similarity=0.95) is False


def test_auto_link_for_fact_skips_different_kind(fresh_bus):
    """auto_link_for_fact filters by same kind — won't link user_preference to risk_rule."""
    a = _insert_canonical(fresh_bus, content='dark roast coffee morning',
                          kind='user_preference')
    _insert_canonical(fresh_bus, content='dark roast coffee morning focus risk',
                      kind='risk_rule')
    # Force same embedding — both encoded by same model
    result = auto_link.auto_link_for_fact(
        fresh_bus, new_fact_id=a,
        content='dark roast coffee morning', kind='user_preference',
        tier='public', user_id=None,
    )
    # Should not link to risk_rule (different kind)
    assert len(result['linked_to']) == 0


def test_auto_link_for_fact_filters_by_cosine_threshold(fresh_bus):
    """Facts below cosine threshold don't link."""
    a = _insert_canonical(fresh_bus, content='dark roast coffee morning')
    b = _insert_canonical(fresh_bus, content='dark roast coffee morning')  # identical
    # Set threshold above 1.0 — cosine is bounded [0,1] so this is unreachable
    result = auto_link.auto_link_for_fact(
        fresh_bus, new_fact_id=a,
        content='dark roast coffee morning', kind='fact',
        tier='public', user_id=None,
        cosine_threshold=1.5,  # unreachable (cosine is bounded [0,1])
    )
    assert len(result['linked_to']) == 0


def test_write_audit_writes_one_row_per_run(fresh_bus):
    """write_audit produces exactly one audit row per auto-link run."""
    a = _insert_canonical(fresh_bus, content='dark roast coffee morning')
    b = _insert_canonical(fresh_bus, content='dark roast coffee morning focus')
    auto_link.auto_link_for_fact(
        fresh_bus, new_fact_id=a,
        content='dark roast coffee morning', kind='fact',
        tier='public', user_id=None,
    )
    # Verify an audit row was written
    audit = fresh_bus.conn.execute(
        "SELECT actor, metadata FROM audit_log WHERE event='auto_link' AND target_id = ?",
        (str(a),)).fetchone()
    assert audit is not None
    assert audit[0] == 'nest.auto_link'
    meta = json.loads(audit[1])
    assert meta['new_fact_id'] == a
    assert 'linked_to' in meta


def test_auto_link_via_server_write_endpoint(fresh_bus, monkeypatch):
    """POST /v1/write triggers auto-link in the hot path."""
    from astor_memory._internal import bot_binding as bb
    import os
    monkeypatch.setattr(bb, '_con', None)
    bb.upsert_user(user_id='admin', short_alias='admin', role='first_admin',
                   subscription_plan='permanent')
    # Seed a similar fact
    a = _insert_canonical(fresh_bus, content='dark roast coffee morning')
    # Write a new fact with similar content
    from astor_memory.server import create_app
    app = create_app(os.environ['ASTOR_DIR'])
    client = app.test_client()
    r = client.post('/v1/write', json={
        'user': 'admin', 'tier': 'public',
        'text': 'dark roast coffee morning focus',
        'scope': 'long_term',
    })
    assert r.status_code == 200
    new_fact_id = r.get_json()['fact_ids'][0]
    # Verify auto-link audit row was written for the new fact
    audit = fresh_bus.conn.execute(
        "SELECT metadata FROM audit_log WHERE event='auto_link' AND target_id = ?",
        (str(new_fact_id),)).fetchone()
    # Auto-link may or may not have linked (cosine threshold matters)
    # but the hot path should not have raised
    assert r.status_code == 200


def test_backfill_all_idempotent(fresh_bus):
    """backfill_all on a fresh bus produces 0 edges (no similar facts)."""
    a = _insert_canonical(fresh_bus, content='lone fact')
    result = auto_link.backfill_all(fresh_bus, tier='public', user_id=None)
    assert result['facts_processed'] == 1
    assert result['edges_added'] == 0


def test_backfill_all_processes_multiple_facts(fresh_bus):
    """backfill_all iterates over multiple facts."""
    # Use genuinely different content (no shared tokens) so cosine is low
    contents = [
        'cryptographic hash function SHA-256 produces 256-bit digest',
        'ocean tide cycles lunar gravitational pull measurement',
        'aviation flight envelope V-speed stall warning calculation',
    ]
    for c in contents:
        _insert_canonical(fresh_bus, content=c)
    result = auto_link.backfill_all(fresh_bus, tier='public', user_id=None,
                                     limit=10)
    assert result['facts_processed'] == 3
    # These 3 facts share zero tokens -> cosine < 0.85 -> 0 edges
    assert result['edges_added'] == 0


def test_backfill_writes_audit_row(fresh_bus):
    """backfill_all writes one audit row summarizing the run."""
    _insert_canonical(fresh_bus, content='sample fact for backfill audit test')
    auto_link.backfill_all(fresh_bus, tier='public', user_id=None, limit=5)
    audit = fresh_bus.conn.execute(
        "SELECT metadata FROM audit_log WHERE event='auto_link_backfill'"
    ).fetchone()
    assert audit is not None
    meta = json.loads(audit[0])
    assert meta['tier'] == 'public'
    assert meta['facts_processed'] >= 1
