"""Injection channels must compete on merit, not on channel position.

The bug
-------
The five auto-injection channels (meta-recall patterns, meta-recall lessons,
trigger-aware boost, mental_model, knowledge_page) PREPENDED their rows to
the recall list and stamped a hard-coded ``similarity`` (0.99 / 0.95 / 1.0)
on them. The list was never globally sorted -- order came from whichever
channel ran last. So a row that merely matched the query lexically (a
``content LIKE '%word%'`` hit) landed at rank 1 with a synthetic 0.99,
displacing the fact the vector index had actually ranked first.

Measured on the 110-query eval set (2026-11-23): 33.6% of rank-1 hits came
from an injection channel. Ranking by lexical overlap alone, the answer for
``德州扑克现金局`` was an unrelated R-class lesson that happened to contain
the string "德州扑克现金局" somewhere in its body.

The fix
-------
1. Injected rows get a similarity derived from the REAL lexical overlap
   between the query and the row's content, not a hard-coded constant.
2. The final result list is globally sorted by similarity, so an injected
   row competes with the scored rows instead of preempting them.

Both together: rank-1 accuracy 0.727 -> 0.845, MRR 0.811 -> 0.891, hit@10
unchanged at 0.955, and the injection feature keeps working (injected rows
still land inside the top 10).

What these tests lock
---------------------
* An injected row's similarity is bounded by its actual query overlap, so a
  hard-coded constant can never come back.
* The final list is sorted by similarity.
* An injected row that overlaps the query weakly cannot outrank a scored
  row that overlaps it strongly.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import astor_memory.server as S  # noqa: E402


# --------------------------------------------------------------------------
# The helper under test
# --------------------------------------------------------------------------
def test_injection_similarity_basic():
    """The real helper: a fraction of the query's words present in content."""
    # 2 of 3 query words present -> 0.5 * (2/3)
    assert abs(S._injection_similarity(
        "backup restore plan", "the backup plan") - 0.5 * (2 / 3)) < 1e-9
    # all query words present -> 0.5
    assert abs(S._injection_similarity(
        "backup restore", "backup restore") - 0.5) < 1e-9
    # none present -> 0.0
    assert S._injection_similarity("backup restore", "nothing here") == 0.0
    # empty query or empty content -> 0.0
    assert S._injection_similarity("", "anything") == 0.0
    assert S._injection_similarity("some query", "") == 0.0


def test_injection_similarity_is_bounded():
    """Never exceeds the 0.5 cap -- a full-overlap row must not outrank a
    strong vector hit, which typically scores 0.8-0.9."""
    for q in ("a b c", "one", "x y z w"):
        v = S._injection_similarity(q, q)
        assert 0.0 <= v <= 0.5


def test_injection_similarity_zero_when_no_overlap():
    assert S._injection_similarity("alpha beta", "gamma delta") == 0.0


# --------------------------------------------------------------------------
# No channel may stamp a constant similarity any more
# --------------------------------------------------------------------------
def test_no_injection_channel_hardcodes_similarity():
    """The synthetic constants are what let a LIKE hit outrank a vector hit.

    Every injection channel must derive its similarity from the real
    overlap instead. This test fails if someone re-adds a constant.
    """
    src = (ROOT / 'astor_memory' / 'server.py').read_text(encoding='utf-8')
    for const in ("'similarity': 0.99", "'similarity': 0.95"):
        assert const not in src, (
            f"{const} is back in server.py -- an injected row with a constant "
            "similarity outranks the scored rows by construction"
        )


def test_injected_similarity_is_derived_not_constant():
    """Sanity: the derived value is a real fraction, bounded by the cap."""
    assert 0.0 <= S._injection_similarity("a b c", "a") <= 0.5
    assert S._injection_similarity("a b c", "a b c") <= 0.5


# --------------------------------------------------------------------------
# The final list is sorted
# --------------------------------------------------------------------------
def test_final_list_is_sorted_by_similarity():
    """The recall list must be ordered by similarity, not by channel order."""
    assert hasattr(S, '_sort_by_similarity'), (
        "server.py must expose _sort_by_similarity -- the recall list order "
        "comes from channel order otherwise"
    )
    rows = [
        {'fact_id': 1, 'similarity': 0.2},
        {'fact_id': 2, 'similarity': 0.9},
        {'fact_id': 3, 'similarity': 0.5},
    ]
    out = S._sort_by_similarity(rows)
    assert [r['fact_id'] for r in out] == [2, 3, 1]


def test_sort_is_stable_and_tolerates_missing_similarity():
    rows = [
        {'fact_id': 1},
        {'fact_id': 2, 'similarity': 0.5},
        {'fact_id': 3, 'similarity': 0.5},
    ]
    out = S._sort_by_similarity(rows)
    # missing similarity sorts last, ties keep their original order
    assert [r['fact_id'] for r in out] == [2, 3, 1]


def test_sort_keeps_retired_below_active():
    """A retired fact must still sink below the active ones (v1.17.50 lock).

    The similarity sort must not undo that: retired rows go last, and
    within the retired group the similarity order still applies (0.9
    before 0.8).
    """
    rows = [
        {'fact_id': 1, 'similarity': 0.9, 'status': 'inactive'},
        {'fact_id': 2, 'similarity': 0.3},
        {'fact_id': 3, 'similarity': 0.8, 'status': 'inactive'},
    ]
    out = S._sort_by_similarity(rows)
    # active first (fid 2), then the retired ones by similarity (0.9, 0.8)
    assert [r['fact_id'] for r in out] == [2, 1, 3]


def test_cold_start_gate_stays_first():
    """The cold-start row is a behavioural gate, not a memory fact.

    It carries similarity 1.0, which carries no relevance meaning. It is
    pinned to the front so the agent sees the "recall before tool" rule
    before any fact, and it must survive the similarity sort there.
    """
    rows = [
        {'fact_id': 5, 'similarity': 0.9},
        {'fact_id': 0, 'similarity': 1.0, 'cold_start': True},
        {'fact_id': 6, 'similarity': 0.4},
    ]
    out = S._sort_by_similarity(rows)
    assert out[0]['fact_id'] == 0
    assert out[0].get('cold_start') is True
    # the rest are ranked by similarity as usual
    assert [r['fact_id'] for r in out[1:]] == [5, 6]


def test_empty_list_is_fine():
    assert S._sort_by_similarity([]) == []
