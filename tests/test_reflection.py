"""
Tests for v1.2.2 reflection orchestrator (EverOS-style episodic consolidation).

Pattern: feed cluster of similar facts, run reflection, verify:
- Winner ID returned (highest importance)
- Loser IDs tombstoned
- Merged content has distinct parts from each member
- Audit rows written
- Idempotent (second run returns 0 since losers are tombstoned)
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from astor_memory._internal.acl import astor_init_acl
from astor_memory._internal.acl_layout import get_db_path, Tier
from astor_memory.bus.schema import astor_init_schema
from astor_memory.bus.store import astor_bus
from astor_memory.nest import reflection


def _insert_canonical(bus, *, content: str, kind: str = 'fact',
                     importance: float = 0.5, confidence: float = 0.7,
                     scope_type: str = 'long_term'):
    """Insert one canonical row directly via SQL helper."""
    event_id = bus.append_event(
        namespace='/test/reflection', agent_id='pytest',
        source='rest', action='write', content=content, metadata={},
    )
    cand_id = bus.insert_candidate(
        event_id=event_id, namespace='/test/reflection',
        content=content, kind=kind,
        confidence=confidence, importance=importance,
    )
    return bus.promote_candidate(
        candidate_id=cand_id, promoted_by='pytest',
        user_id=None, tier='public', scope_type=scope_type,
    )


@pytest.fixture
def fresh_bus(tmp_path, monkeypatch):
    target = tmp_path / "astor_refl"
    monkeypatch.setenv("ASTOR_DIR", str(target))
    p = get_db_path(Tier.PUBLIC.value, "bus", None)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=5)
    astor_init_schema(conn)
    conn.close()
    astor_init_acl(actor='admin:admin', role='admin', tier='public')
    return astor_bus(tier=Tier.PUBLIC.value, user_id=None)


def test_select_episode_clusters_empty_when_few_facts(fresh_bus):
    """No clusters when only 1 fact of a kind."""
    _insert_canonical(fresh_bus, content='Lone fact about cats and dogs playing')
    clusters = reflection.select_episode_clusters(fresh_bus, tier='public', user_id=None)
    assert clusters == []


def test_select_episode_clusters_finds_similar_pair(fresh_bus):
    """Two facts sharing >=3 distinctive tokens cluster together."""
    f1 = _insert_canonical(
        fresh_bus,
        content='user prefers dark roast coffee every morning for focus',
        kind='user_preference',
    )
    f2 = _insert_canonical(
        fresh_bus,
        content='dark roast coffee is the best morning drink for focus',
        kind='user_preference',
    )
    clusters = reflection.select_episode_clusters(fresh_bus, tier='public', user_id=None)
    assert len(clusters) == 1
    assert sorted(clusters[0]) == sorted([f1, f2])


def test_select_clusters_separates_by_kind(fresh_bus):
    """Facts of different kinds don't merge even with similar content."""
    f1 = _insert_canonical(
        fresh_bus,
        content='dark roast coffee morning focus',
        kind='user_preference',
    )
    _insert_canonical(
        fresh_bus,
        content='dark roast coffee morning focus',
        kind='observation',  # different kind -> different cluster
    )
    clusters = reflection.select_episode_clusters(fresh_bus, tier='public', user_id=None)
    # Both clusters have size 1 -> filtered out (min_size=2)
    assert clusters == []


def test_select_clusters_skips_length_mismatch(fresh_bus):
    """Facts with content length > 2x don't merge (sanity check)."""
    _insert_canonical(
        fresh_bus,
        content='short coffee preference note',
        kind='user_preference',
    )
    _insert_canonical(
        fresh_bus,
        content='prefer dark roast coffee each morning for focus and productivity and clarity',
        kind='user_preference',
    )
    clusters = reflection.select_episode_clusters(fresh_bus, tier='public', user_id=None)
    # Length ratio > 2x -> no cluster
    assert clusters == []


def test_merge_narrative_picks_highest_importance_winner(fresh_bus):
    """merge_narrative picks the winner with highest importance."""
    _insert_canonical(fresh_bus, content='dark roast coffee morning focus',
                      kind='user_preference', importance=0.3)
    f_winner = _insert_canonical(fresh_bus, content='dark roast coffee morning focus best',
                                 kind='user_preference', importance=0.9)
    _insert_canonical(fresh_bus, content='dark roast coffee morning focus great',
                      kind='user_preference', importance=0.5)
    facts = [
        {'id': 1, 'content': 'a', 'importance': 0.3, 'promoted_at': '', 'confidence': 0.5},
        {'id': 2, 'content': 'b', 'importance': 0.9, 'promoted_at': '', 'confidence': 0.5},
        {'id': 3, 'content': 'c', 'importance': 0.5, 'promoted_at': '', 'confidence': 0.5},
    ]
    merged = reflection.merge_narrative(facts)
    assert merged['winner_id'] == 2
    assert sorted(merged['loser_ids']) == [1, 3]
    assert merged['merged_importance'] == 1.0  # 0.9 + 0.1 capped


def test_merge_narrative_concatenates_distinct_content(fresh_bus):
    """Merged content joins all distinct member contents with separator."""
    facts = [
        {'id': 1, 'content': 'first paragraph', 'importance': 0.5, 'promoted_at': '', 'confidence': 0.5},
        {'id': 2, 'content': 'second paragraph', 'importance': 0.5, 'promoted_at': '', 'confidence': 0.5},
        {'id': 3, 'content': 'first paragraph', 'importance': 0.5, 'promoted_at': '', 'confidence': 0.5},
    ]
    merged = reflection.merge_narrative(facts)
    # 'first paragraph' should appear once (the winner), 'second paragraph' appended
    assert merged['merged_content'].count('first paragraph') == 1
    assert 'second paragraph' in merged['merged_content']
    assert '\n---\n' in merged['merged_content']


def test_deprecate_old_facts_tombstones_and_audits(fresh_bus):
    """deprecate_old_facts tombstones losers + writes audit row."""
    f1 = _insert_canonical(fresh_bus, content='dark roast coffee morning focus',
                           kind='user_preference')
    f2 = _insert_canonical(fresh_bus, content='dark roast coffee morning focus best',
                           kind='user_preference', importance=0.9)
    n_deprecated = reflection.deprecate_old_facts(
        fresh_bus, loser_ids=[f1], winner_id=f2, actor='test')
    assert n_deprecated == 1
    # Verify tombstoned
    row = fresh_bus.conn.execute(
        'SELECT tombstoned, tombstoned_at FROM memory_canonical WHERE id = ?',
        (f1,)).fetchone()
    assert row[0] == 1
    assert row[1] is not None
    # Verify audit row
    audit = fresh_bus.conn.execute(
        "SELECT actor, severity, metadata, old_state, new_state FROM audit_log "
        "WHERE event='reflection_deprecated' AND target_id = ?",
        (str(f1),)).fetchone()
    assert audit is not None
    assert audit[0] == 'test'
    assert audit[1] == 'info'
    # v1.13.1 (2026-09-02): write_audit now uses dedicated 'old_state'
    # and 'new_state' DB columns (not stuffed into 'metadata' anymore).
    # Read from column indexes directly.
    old_state = json.loads(audit[3])
    assert 'columns' in old_state
    assert old_state['columns']['content'].startswith('dark roast')
    new_state = json.loads(audit[4])
    assert new_state['merged_into'] == f2


def test_apply_merge_updates_winner(fresh_bus):
    """apply_merge updates winner's content + importance + promoted_at."""
    f1 = _insert_canonical(fresh_bus, content='dark roast coffee morning focus',
                           kind='user_preference', importance=0.5)
    reflection.apply_merge(
        fresh_bus, winner_id=f1,
        merged_content='dark roast coffee morning focus [merged epic]',
        merged_importance=0.7,
        actor='test',
    )
    row = fresh_bus.conn.execute(
        'SELECT content, importance, promoted_at, last_confirmed_at FROM memory_canonical WHERE id = ?',
        (f1,)).fetchone()
    assert '[merged epic]' in row[0]
    assert row[1] == 0.7
    assert row[2] is not None
    assert row[3] is not None
    # Audit row
    audit = fresh_bus.conn.execute(
        "SELECT actor FROM audit_log WHERE event='reflection_merged' AND target_id = ?",
        (str(f1),)).fetchone()
    assert audit is not None
    assert audit[0] == 'test'


def test_run_reflection_full_pipeline(fresh_bus):
    """End-to-end: insert 3 similar facts, run reflection, verify winner + tombstones."""
    f1 = _insert_canonical(fresh_bus, content='dark roast coffee morning focus',
                           kind='user_preference', importance=0.3)
    f2 = _insert_canonical(fresh_bus, content='dark roast coffee morning focus best',
                           kind='user_preference', importance=0.9)
    f3 = _insert_canonical(fresh_bus, content='dark roast coffee morning focus great',
                           kind='user_preference', importance=0.5)
    result = reflection.run_reflection(fresh_bus, tier='public', user_id=None)
    assert result['clusters_found'] == 1
    assert result['clusters_merged'] == 1
    assert result['facts_deprecated'] == 2  # 2 losers
    # Winner is f2 (highest importance)
    assert result['merge_log'][0]['winner_id'] == f2
    assert sorted(result['merge_log'][0]['merged_from']) == [f1, f3]
    # Verify winner content was updated
    row = fresh_bus.conn.execute(
        'SELECT content, importance FROM memory_canonical WHERE id = ?',
        (f2,)).fetchone()
    assert 'dark roast coffee morning focus' in row[0]
    assert row[1] >= 0.9  # bumped
    # Verify losers tombstoned
    for lid in [f1, f3]:
        row = fresh_bus.conn.execute(
            'SELECT tombstoned FROM memory_canonical WHERE id = ?',
            (lid,)).fetchone()
        assert row[0] == 1


def test_run_reflection_idempotent(fresh_bus):
    """Second run finds 0 clusters (losers are tombstoned, won't match)."""
    f1 = _insert_canonical(fresh_bus, content='dark roast coffee morning focus',
                           kind='user_preference')
    f2 = _insert_canonical(fresh_bus, content='dark roast coffee morning focus best',
                           kind='user_preference', importance=0.9)
    # First run
    result1 = reflection.run_reflection(fresh_bus, tier='public', user_id=None)
    assert result1['clusters_merged'] == 1
    # Second run — losers are tombstoned, no cluster
    result2 = reflection.run_reflection(fresh_bus, tier='public', user_id=None)
    assert result2['clusters_found'] == 0
    assert result2['clusters_merged'] == 0


def test_run_reflection_filters_by_kinds(fresh_bus):
    """--kinds filter restricts which kinds are considered."""
    _insert_canonical(
        fresh_bus,
        content='dark roast coffee morning focus',
        kind='user_preference',
    )
    _insert_canonical(
        fresh_bus,
        content='dark roast coffee morning focus',
        kind='observation',  # different kind
    )
    # With kinds=['observation'], only 1 fact of observation -> no cluster
    result = reflection.run_reflection(
        fresh_bus, tier='public', user_id=None, kinds=['observation'])
    assert result['clusters_found'] == 0
    # With kinds=['user_preference'], only 1 fact -> no cluster
    result = reflection.run_reflection(
        fresh_bus, tier='public', user_id=None, kinds=['user_preference'])
    assert result['clusters_found'] == 0
