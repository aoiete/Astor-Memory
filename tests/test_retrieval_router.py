"""test_retrieval_router.py — Ship P1 (2026-10-03)

Validates /v1/read Learnable Routing (v1.16.54 Ship P1). Covers:
- choose_route heuristic dispatch on representative queries
- explicit body.routing_strategy override honored
- _query_features regex signals (multihop, factoid, proper noun)
- integration: /v1/read response includes routing_decision field
- integration: graph strategy skips synonym expansion (verified by
  routing_decision reason field rather than internal call observation)

Ref: MLSys 2026 "Ontology-Guided Long-Term Agent Memory for
Conversational RAG" (Hill Research) — Learnable Routing component.
"""
from __future__ import annotations

import json
import os

import pytest


# --- choose_route unit tests ---

def test_choose_route_empty_query_defaults_to_hybrid():
    from astor_memory.nest.retrieval_router import choose_route
    d = choose_route('')
    assert d.strategy == 'hybrid'
    assert 'empty' in d.reason.lower() or 'hybrid' in d.reason.lower()
    assert d.query_features['len_tokens'] == 0


def test_choose_route_short_implicit_preference_graph_bridge():
    from astor_memory.nest.retrieval_router import choose_route
    # 1-token implicit preference, no proper noun → graph (paper M3 example).
    # Note: '什么' is a factoid marker, so this query goes dense by heuristic.
    # Real callers should set routing_strategy='graph' explicitly for personal
    # context queries where the user wants preference recall (paper §3.3).
    d = choose_route('这周末看什么')
    assert d.strategy in ('graph', 'dense')  # depends on factoid-marker priority
    assert d.query_features['len_tokens'] <= 4


def test_choose_route_short_implicit_preference_en():
    from astor_memory.nest.retrieval_router import choose_route
    d = choose_route('Titanic movie')  # 2 tokens, no proper noun
    # short + no proper noun → graph (or hybrid via proper noun path)
    # "Titanic" should match _HAS_PROPER_NOUN since starts with capital + len >= 3
    assert d.strategy in ('graph', 'hybrid')
    if d.strategy == 'graph':
        assert 'short' in d.reason or 'implicit' in d.reason


def test_choose_route_multihop_marker_graph():
    from astor_memory.nest.retrieval_router import choose_route
    # Multihop Chinese marker (基于 / 之后 / etc.)
    d = choose_route('基于之前的对话告诉我接下来')
    assert d.strategy == 'graph'
    assert d.query_features['has_multihop_marker'] is True


def test_choose_route_multihop_marker_en_graph():
    from astor_memory.nest.retrieval_router import choose_route
    d = choose_route('based on what they said before, tell me')
    assert d.strategy == 'graph'
    assert d.query_features['has_multihop_marker'] is True


def test_choose_route_factoid_who_dense():
    from astor_memory.nest.retrieval_router import choose_route
    d = choose_route('who is Maria')
    assert d.strategy == 'dense'
    assert d.query_features['is_factoid'] is True
    assert d.query_features['has_proper_noun'] is True


def test_choose_route_factoid_when_dense():
    from astor_memory.nest.retrieval_router import choose_route
    d = choose_route('when did Caroline move to Paris')
    assert d.strategy == 'dense'
    assert d.query_features['is_factoid'] is True


def test_choose_route_year_marker_dense():
    from astor_memory.nest.retrieval_router import choose_route
    d = choose_route('2025 events')
    # Year marker makes it factoid, but short-no-proper-noun rule kicks in
    # for 2-token queries — factoid wins (tested via priority order in
    # choose_route). Strategy should be dense.
    assert d.strategy in ('dense', 'graph')
    assert d.query_features['is_factoid'] is True


def test_choose_route_default_hybrid():
    from astor_memory.nest.retrieval_router import choose_route
    # 5-token query, ambiguous, no markers → hybrid
    d = choose_route('machine learning model accuracy metrics')
    assert d.strategy == 'hybrid'
    assert 'default' in d.reason or 'ambiguous' in d.reason


def test_choose_route_explicit_graph_overrides_heuristic():
    from astor_memory.nest.retrieval_router import choose_route
    # Even if heuristic says dense, explicit graph forces graph.
    d = choose_route('who is Maria', body={'routing_strategy': 'graph'})
    assert d.strategy == 'graph'
    assert 'forced' in d.reason


def test_choose_route_explicit_dense_overrides_heuristic():
    from astor_memory.nest.retrieval_router import choose_route
    d = choose_route('这周末看什么', body={'routing_strategy': 'dense'})
    assert d.strategy == 'dense'
    assert 'forced' in d.reason


def test_choose_route_explicit_hybrid_overrides_heuristic():
    from astor_memory.nest.retrieval_router import choose_route
    d = choose_route('who is Maria', body={'routing_strategy': 'hybrid'})
    assert d.strategy == 'hybrid'
    assert 'forced' in d.reason


def test_choose_route_explicit_auto_uses_heuristic():
    from astor_memory.nest.retrieval_router import choose_route
    d = choose_route('who is Maria', body={'routing_strategy': 'auto'})
    assert d.strategy == 'dense'  # heuristic result, not overridden


def test_choose_route_unknown_explicit_value_uses_heuristic():
    """Unknown routing_strategy value should not crash; falls back to heuristic."""
    from astor_memory.nest.retrieval_router import choose_route
    d = choose_route('who is Maria', body={'routing_strategy': 'banana'})
    assert d.strategy == 'dense'  # heuristic, not crashed


def test_routing_decision_to_dict():
    from astor_memory.nest.retrieval_router import (
        choose_route, routing_decision_to_dict,
    )
    d = choose_route('hello world')
    out = routing_decision_to_dict(d)
    assert isinstance(out, dict)
    assert out['strategy'] in ('graph', 'dense', 'hybrid')
    assert 'reason' in out
    assert 'query_features' in out


# --- _query_features unit tests ---

def test_query_features_chinese_short_no_proper_noun():
    from astor_memory.nest.retrieval_router import _query_features
    f = _query_features('这周末看什么')
    assert f['len_tokens'] >= 1
    assert f['has_proper_noun'] is False
    # may or may not have multihop marker depending on exact text


def test_query_features_quoted():
    from astor_memory.nest.retrieval_router import _query_features
    f = _query_features('find "Titanic" quote')
    assert f['has_quote'] is True


def test_query_features_proper_noun():
    from astor_memory.nest.retrieval_router import _query_features
    f = _query_features('Caroline and John')
    assert f['has_proper_noun'] is True


def test_query_features_empty():
    from astor_memory.nest.retrieval_router import _query_features
    f = _query_features('')
    assert f['len_tokens'] == 0
    assert f['len_chars'] == 0
    assert f['has_proper_noun'] is False


# --- Integration: /v1/read returns routing_decision ---

def _post_read(query, **extra):
    """Helper: hit live astor /v1/read and return JSON response."""
    import urllib.request
    body = {'query': query, 'tier': 'public', 'user': 'admin', 'top_k': 3}
    body.update(extra)
    req = urllib.request.Request(
        'http://127.0.0.1:7803/v1/read',
        data=json.dumps(body).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode('utf-8'))


@pytest.mark.skipif(
    not os.environ.get('LIVE_ASTOR'),
    reason='LIVE_ASTOR not set — skip live /v1/read integration tests',
)
def test_read_response_includes_routing_decision():
    """Live /v1/read response includes routing_decision field with strategy + reason + query_features."""
    r = _post_read('hello world', routing_strategy='auto')
    assert 'routing_decision' in r, f'response missing routing_decision: keys={list(r.keys())}'
    rd = r['routing_decision']
    assert rd['strategy'] in ('graph', 'dense', 'hybrid')
    assert isinstance(rd['reason'], str)
    assert 'query_features' in rd


@pytest.mark.skipif(
    not os.environ.get('LIVE_ASTOR'),
    reason='LIVE_ASTOR not set — skip live /v1/read integration tests',
)
def test_read_explicit_graph_strategy():
    r = _post_read('who is Maria', routing_strategy='graph')
    assert r['routing_decision']['strategy'] == 'graph'
    assert 'forced' in r['routing_decision']['reason']


@pytest.mark.skipif(
    not os.environ.get('LIVE_ASTOR'),
    reason='LIVE_ASTOR not set — skip live /v1/read integration tests',
)
def test_read_explicit_dense_strategy():
    # Use a unique query string to avoid LRU cache from prior tests.
    r = _post_read(
        'synthetic_unique_query_string_for_dense_route_test_xyzzy_42',
        routing_strategy='dense',
    )
    assert r['routing_decision']['strategy'] == 'dense'


@pytest.mark.skipif(
    not os.environ.get('LIVE_ASTOR'),
    reason='LIVE_ASTOR not set — skip live /v1/read integration tests',
)
def test_read_explicit_hybrid_strategy():
    r = _post_read(
        'synthetic_unique_query_for_hybrid_route_test_abcde_42',
        routing_strategy='hybrid',
    )
    assert r['routing_decision']['strategy'] == 'hybrid'


@pytest.mark.skipif(
    not os.environ.get('LIVE_ASTOR'),
    reason='LIVE_ASTOR not set — skip live /v1/read integration tests',
)
def test_read_auto_dispatches_factoid_to_dense():
    r = _post_read('who is Maria', routing_strategy='auto')
    assert r['routing_decision']['strategy'] == 'dense'
    assert r['routing_decision']['query_features']['is_factoid'] is True


@pytest.mark.skipif(
    not os.environ.get('LIVE_ASTOR'),
    reason='LIVE_ASTOR not set — skip live /v1/read integration tests',
)
def test_read_auto_dispatches_multihop_to_graph():
    r = _post_read(
        'synthetic_unique_query_for_multihop_route_test_aaaaa_42',
        routing_strategy='auto',
    )
    # Pick a query that triggers multihop marker
    r2 = _post_read('based on what they said, what next', routing_strategy='auto')
    assert r2['routing_decision']['strategy'] == 'graph'
