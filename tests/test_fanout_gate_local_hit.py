"""The fanout gate must not treat a vector neighbour as an answer.

The gate asks whether one document here carries the question. A vector
store answers every query with its nearest neighbour, so "there is a
result" says nothing -- that is what kept the peer network from ever
being asked (R-class 13720). The gate therefore measures lexical
coverage, and the fallback for when that measurement cannot be made at
all must not reintroduce the neighbour heuristic.

These tests pin the two paths through the gate:

    a stored fact is answered here -- the peers are not asked
    a subject this node never stored -- the peers are asked
"""
from __future__ import annotations

import sqlite3

import pytest

from astor_memory.nest.lex_index import AstorLex


@pytest.fixture()
def lex(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    instance = AstorLex(tier="private", user_id="u")
    yield instance
    try:
        instance.close()
    except Exception:
        pass


def _add(lex, fact_id: int, text: str) -> None:
    lex.index_fact(fact_id, text)


def test_stored_subject_is_answered_here(lex):
    _add(lex, 1, "peer fanout latency threshold measured in seconds")
    share, _m = lex.idf_coverage("peer fanout latency threshold", limit=5)
    assert share >= 0.40, "a stored subject must not fan out"


def test_unknown_subject_scores_below_the_threshold(lex):
    _add(lex, 1, "peer fanout latency threshold measured in seconds")
    share, _m = lex.idf_coverage(
        "quantum entanglement calibration protocol", limit=5)
    assert share < 0.40, "an unknown subject must fan out"


def test_coverage_failure_falls_back_to_lexical_hits_not_neighbours(lex):
    """When the coverage cannot be measured, the fallback is lexical.

    The vector store always returns a nearest neighbour, so falling
    back to "there is a result" would mean the gate never opens. The
    fallback must be the lexical hit set, which is empty for a subject
    this node never stored.
    """
    _add(lex, 1, "weixin passcode session hygiene")
    lexical_hits = lex.bm25_search("quantum entanglement calibration", limit=5)
    assert not lexical_hits, "the fallback set must be empty for a miss"
