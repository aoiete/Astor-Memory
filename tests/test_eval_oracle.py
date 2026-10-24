"""test_eval_oracle.py — the oracle side of the evaluation.

2026-10-23 (Uni-Mem, arXiv 2610.29671): Uni-Mem separates "the evidence
was stored badly" from "the evidence was stored and the search did not
surface it", by scoring the memory with a frozen answer model reading
the evidence directly.

Astor had no such split: every metric was a retrieval metric, so a miss
had no owner and every fix was a guess. eval_oracle.py asks the store
what it holds without going through the ranking layer, so the two sides
can be compared.

The hard part is that the two sides must be scored by the SAME rule. An
early version of the oracle required every keyword in one row while the
retrieval path accepted any keyword, and it reported 46% of the corpus
as missing -- a gap that did not exist, manufactured by comparing two
different questions. These tests pin the shared rule.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_oracle import bus_path, classify, coverage, oracle_search  # noqa: E402


def _make_bus(runtime: Path, tier: str = "public",
              user_id: str | None = None, rows=()) -> Path:
    if tier == "private":
        db = runtime / "users" / (user_id or "admin") / "memory" / \
             f"astor_bus_{user_id or 'admin'}.db"
    else:
        db = runtime / tier / "memory" / f"astor_bus_{tier}.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE IF NOT EXISTS memory_canonical ("
        " id INTEGER PRIMARY KEY, content TEXT, importance REAL DEFAULT 0.5,"
        " tombstoned INTEGER DEFAULT 0, invalidated_reason TEXT DEFAULT '')"
    )
    for i, (content, tomb) in enumerate(rows, start=1):
        con.execute(
            "INSERT OR REPLACE INTO memory_canonical "
            "(id, content, tombstoned) VALUES (?, ?, ?)",
            (i, content, tomb),
        )
    con.commit()
    con.close()
    return db


@pytest.fixture()
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    return tmp_path


# --- the classification ------------------------------------------------

def test_classify_covers_all_three_cases():
    assert classify(True, True) == "hit"
    assert classify(False, False) == "storage_gap"
    assert classify(True, False) == "retrieval_gap"


# --- the OR rule (the one that was wrong the first time) ---------------

def test_any_keyword_matches(runtime):
    _make_bus(runtime, rows=[
        ("德州扑克现金局指南", 0),
    ])
    rows = oracle_search(["德州", "健身", "计划"], tier="public")
    assert len(rows) == 1, "one keyword is enough, same as the recall path"


def test_tombstoned_rows_are_not_visible(runtime):
    _make_bus(runtime, rows=[
        ("德州扑克现金局指南", 1),
    ])
    rows = oracle_search(["德州"], tier="public")
    assert rows == [], "a dead fact is not in the store"


def test_no_match_is_no_match(runtime):
    _make_bus(runtime, rows=[("德州扑克现金局指南", 0)])
    assert oracle_search(["健身"], tier="public") == []


def test_missing_db_is_empty_not_a_crash(runtime):
    assert bus_path("public") is None
    assert oracle_search(["德州"], tier="public") == []


def test_missing_tier_directory_is_empty_not_a_crash(runtime):
    assert oracle_search(["德州"], tier="source") == []


# --- coverage, scored like the retrieval path --------------------------

def test_coverage_uses_min_hits(runtime):
    _make_bus(runtime, rows=[
        ("德州扑克现金局指南", 0),
        ("健身计划一周表", 0),
    ])
    c1 = coverage(["德州", "健身"], tier="public", min_hits=1)
    assert c1["oracle_hit"] is True
    assert c1["oracle_n"] == 2

    c2 = coverage(["德州", "健身"], tier="public", min_hits=3)
    assert c2["oracle_hit"] is False, "only two rows match, min_hits=3"
    assert c2["oracle_n"] == 2


def test_coverage_reports_which_keyword_is_missing(runtime):
    _make_bus(runtime, rows=[("德州扑克现金局指南", 0)])
    c = coverage(["德州", "健身"], tier="public", min_hits=1)
    assert "德州" in c["oracle_covered"]
    assert "健身" in c["oracle_missed"]


def test_coverage_on_empty_store(runtime):
    c = coverage(["德州"], tier="public")
    assert c["oracle_hit"] is False
    assert c["oracle_n"] == 0
