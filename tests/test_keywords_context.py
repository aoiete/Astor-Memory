"""
Tests for v1.2.0 keywords + context schema columns + hybrid_merge Jaccard boost.

Pattern: verify extraction populates fields, schema migration runs cleanly,
promote_candidate stores the fields, hybrid_merge boost improves ranking.
"""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

from astor_memory._internal.acl import astor_init_acl
from astor_memory._internal.acl_layout import get_db_path, Tier
from astor_memory.bus.schema import astor_init_schema, SCHEMA_VERSION
from astor_memory.bus.store import astor_bus
from astor_memory.forge.extractor import AstorFact, astor_regex_extract
from astor_memory.nest.lex_index import hybrid_merge, _tokenize


@pytest.fixture
def fresh_bus(tmp_path, monkeypatch):
    """Fresh ASTOR_DIR with schema v5 initialized."""
    target = tmp_path / "astor_v5"
    monkeypatch.setenv("ASTOR_DIR", str(target))
    p = get_db_path(Tier.PUBLIC.value, "bus", None)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=5)
    astor_init_schema(conn)
    conn.close()
    astor_init_acl(actor='first_admin', role='first_admin', tier='public')
    bus = astor_bus(tier=Tier.PUBLIC.value, user_id=None)
    return bus, target


def _insert_with_keywords_context(bus, *, content: str, keywords: list[str], context: str):
    """Helper: insert a canonical row directly via SQL with explicit keywords/context."""
    event_id = bus.append_event(
        namespace='/test/kw',
        agent_id='pytest',
        source='rest.write',
        action='write',
        content=content,
        metadata={},
    )
    cand_id = bus.insert_candidate(
        event_id=event_id,
        namespace='/test/kw',
        content=content,
        kind='fact',
        keywords=keywords,
        context=context,
    )
    return bus.promote_candidate(
        candidate_id=cand_id,
        promoted_by='pytest',
        user_id=None,
        tier='public',
        scope_type='long_term',
    )


def test_schema_version_is_5():
    """SCHEMA_VERSION is currently 8 (post-v1.12 ACL hardening).
    Test was originally written for v5; updated as schema bumped."""
    assert SCHEMA_VERSION == 8


def test_canonical_has_keywords_and_context_columns(fresh_bus):
    """memory_canonical has keywords + context columns after schema init."""
    bus, _ = fresh_bus
    cols = {row[1] for row in bus.conn.execute(
        "PRAGMA table_info(memory_canonical)"
    ).fetchall()}
    assert 'keywords' in cols
    assert 'context' in cols


def test_astor_regex_extract_populates_keywords_and_context():
    """Regex extractor (heuristic path) populates keywords + context."""
    text = "I prefer dark roast coffee every morning"
    facts = astor_regex_extract(text)
    assert len(facts) == 1
    f = facts[0]
    # keyword 0 = kind ('user_preference'), rest are distinctive tokens from content
    assert f.keywords is not None and len(f.keywords) > 0
    assert f.keywords[0] == 'user_preference'
    assert f.context == text[:120].strip()
    assert f.context.startswith("I prefer dark roast")


def test_insert_candidate_stores_keywords_in_metadata(fresh_bus):
    """insert_candidate with keywords= stores them in candidate metadata JSON."""
    bus, _ = fresh_bus
    event_id = bus.append_event(
        namespace='/test/kw', agent_id='pytest', source='rest',
        action='write', content='test', metadata={},
    )
    cand_id = bus.insert_candidate(
        event_id=event_id,
        namespace='/test/kw',
        content='user prefers dark roast coffee',
        kind='user_preference',
        keywords=['coffee', 'dark-roast', 'preference', 'morning'],
        context='User stated preference for dark roast coffee over light',
    )
    # Candidate metadata JSON should carry __keywords__ + __context__
    row = bus.conn.execute(
        "SELECT metadata FROM memory_candidates WHERE id = ?",
        (cand_id,)).fetchone()
    meta = json.loads(row[0])
    assert meta.get('__keywords__') == ['coffee', 'dark-roast', 'preference', 'morning']
    assert meta.get('__context__') == 'User stated preference for dark roast coffee over light'


def test_promote_candidate_writes_keywords_and_context_to_canonical(fresh_bus):
    """After promote_candidate, canonical row has keywords + context columns."""
    bus, _ = fresh_bus
    canon_id = _insert_with_keywords_context(
        bus,
        content='user prefers dark roast coffee',
        keywords=['coffee', 'dark-roast', 'preference'],
        context='User likes dark roast coffee',
    )
    row = bus.conn.execute(
        "SELECT keywords, context FROM memory_canonical WHERE id = ?",
        (canon_id,)).fetchone()
    kws = json.loads(row[0])
    assert kws == ['coffee', 'dark-roast', 'preference']
    assert row[1] == 'User likes dark roast coffee'


def test_promote_handles_missing_keywords_with_defaults(fresh_bus):
    """Older candidates (no __keywords__/__context__ in metadata) get safe defaults."""
    bus, _ = fresh_bus
    # Insert candidate manually WITHOUT keywords/context to simulate legacy data
    event_id = bus.append_event(
        namespace='/test/legacy', agent_id='pytest', source='rest',
        action='write', content='legacy fact', metadata={},
    )
    cand_id = bus.insert_candidate(
        event_id=event_id, namespace='/test/legacy',
        content='legacy fact', kind='fact',
    )
    # Promote — should default keywords=[] and context=''
    canon_id = bus.promote_candidate(
        candidate_id=cand_id, promoted_by='pytest',
        user_id=None, tier='public', scope_type='long_term',
    )
    row = bus.conn.execute(
        "SELECT keywords, context FROM memory_canonical WHERE id = ?",
        (canon_id,)).fetchone()
    assert json.loads(row[0]) == []  # default '[]'
    assert row[1] == ''  # default ''


def test_hybrid_merge_jaccard_boost_promotes_matching_fact(fresh_bus):
    """hybrid_merge with keyword_boost should rank facts whose keywords
    match the query higher than they would have without boost."""
    # Same cosine + BM25 scores for both candidates, but one has matching
    # keywords and one doesn't — Jaccard boost should rank matching one higher.
    bm25_hits = [(1, 10.0), (2, 10.0)]
    vector_hits = [(1, 0.8), (2, 0.8)]
    keyword_hits = {
        1: ['coffee', 'dark-roast', 'preference'],  # matches query below
        2: ['unrelated', 'topic'],
    }
    query_keywords = _tokenize('user prefers dark roast coffee')
    # With boost:
    ranked = hybrid_merge(
        bm25_hits=bm25_hits,
        vector_hits=vector_hits,
        limit=10,
        keyword_boost=0.15,
        keyword_hits=keyword_hits,
        query_keywords=query_keywords,
    )
    assert ranked[0][0] == 1  # fact_id 1 should be first (matching keywords)
    # Without boost (legacy behavior):
    ranked_no_boost = hybrid_merge(
        bm25_hits=bm25_hits,
        vector_hits=vector_hits,
        limit=10,
    )
    # Both get same score (tie) — order is arbitrary
    assert {r[0] for r in ranked_no_boost} == {1, 2}


def test_hybrid_merge_backward_compat_no_keywords(fresh_bus):
    """hybrid_merge with keyword_hits=None + query_keywords=None is byte-identical
    to legacy behavior (no regression for callers that don't pass keywords)."""
    bm25_hits = [(1, 10.0), (2, 5.0)]
    vector_hits = [(1, 0.9), (2, 0.7)]
    # Legacy call: no keyword args.
    out = hybrid_merge(bm25_hits=bm25_hits, vector_hits=vector_hits, limit=10)
    # Verify ranking unchanged
    assert out[0][0] == 1  # fact 1 has higher BM25+vector score
    assert out[1][0] == 2
    # Verify no zero/negative scores from boost
    assert out[0][1] == 0.4 * 1.0 + 0.6 * 0.9  # 0.94
    assert out[1][1] == 0.4 * (5.0 / 10.0) + 0.6 * 0.7  # 0.62


def test_hybrid_merge_keywords_mismatch_keeps_baseline(fresh_bus):
    """When query keywords don't overlap with any fact's keywords,
    boost=0 contribution — ranking driven only by BM25 + vector."""
    bm25_hits = [(1, 10.0), (2, 5.0)]
    vector_hits = [(1, 0.9), (2, 0.7)]
    keyword_hits = {1: ['topic-a'], 2: ['topic-b']}
    query_keywords = _tokenize('completely different query')
    out = hybrid_merge(
        bm25_hits=bm25_hits, vector_hits=vector_hits, limit=10,
        keyword_boost=0.5,  # big boost
        keyword_hits=keyword_hits, query_keywords=query_keywords,
    )
    # No overlap → boost contributes 0 → scores == legacy
    assert out[0][1] == 0.4 * 1.0 + 0.6 * 0.9
    assert out[1][1] == 0.4 * (5.0 / 10.0) + 0.6 * 0.7


def test_hybrid_merge_keywords_partial_overlap_uses_jaccard(fresh_bus):
    """Jaccard = |A ∩ B| / |A ∪ B|. Verify with a known overlap."""
    bm25_hits = [(1, 10.0)]  # single candidate for clean math
    vector_hits = [(1, 0.0)]  # zero vector so boost is the only differentiator
    keyword_hits = {1: ['coffee', 'dark', 'roast']}  # fact_kw
    query_keywords = ['coffee', 'milk']  # overlap = {coffee}; union = {coffee, dark, roast, milk}
    # jaccard = 1/4 = 0.25
    out = hybrid_merge(
        bm25_hits=bm25_hits, vector_hits=vector_hits, limit=10,
        keyword_boost=0.5,  # so contribution = 0.125
        keyword_hits=keyword_hits, query_keywords=query_keywords,
    )
    # BM25 normalized to 1.0 (only 1 hit), vector = 0
    # Score = 0.4*1.0 + 0.6*0 + 0.5*0.25 = 0.4 + 0.125 = 0.525
    assert abs(out[0][1] - 0.525) < 0.01


def test_keywords_migration_idempotent(fresh_bus):
    """_astor_upgrade_v4_to_v5 is idempotent — running twice doesn't error."""
    from astor_memory.bus.schema import _astor_upgrade_v4_to_v5
    bus, _ = fresh_bus
    conn = bus.conn
    # Run twice
    _astor_upgrade_v4_to_v5(conn)
    _astor_upgrade_v4_to_v5(conn)
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(memory_canonical)"
    ).fetchall()}
    assert 'keywords' in cols
    assert 'context' in cols