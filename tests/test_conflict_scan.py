"""Two facts that contradict each other must not both be reinforced.

v1.17.18. The write side has had conflict detection for a while
(`nest/ecv.py`): a new fact is classified against an existing anchor as
new_fact / clarification / corroboration / repeat / conflict. But it
only ever ran when a fact was written. On the read side, a fact that
contradicts another one still came back ranked, got its access counter
bumped like every other hit, and the caller had no way to know the two
statements disagreed.

Bumping the counter is what keeps a fact out of the decay sweep, so a
fact that is contradicted every time it surfaces was being kept hot on
the strength of the contradiction.

These tests pin the behaviour, not the implementation.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory.nest.ecv import scan_conflicts  # noqa: E402


def _f(fid: int, content: str) -> dict:
    return {"fact_id": fid, "content": content}


class TestScanConflicts(unittest.TestCase):

    def test_a_contradiction_is_found(self):
        facts = [
            _f(1, "astor 的 public tier 存 public 数据"),
            _f(2, "astor 的 public tier 不存 private 数据"),
        ]
        pairs = scan_conflicts(facts, min_jaccard=0.5)
        self.assertEqual(len(pairs), 1, f"a contradiction must be found: {pairs}")
        self.assertEqual({pairs[0][0], pairs[0][1]}, {1, 2})

    def test_english_negation_is_found(self):
        facts = [
            _f(1, "the relay stores every query in plaintext"),
            _f(2, "the relay does not store every query in plaintext"),
        ]
        pairs = scan_conflicts(facts, min_jaccard=0.5)
        self.assertEqual(len(pairs), 1, f"must be found: {pairs}")

    def test_two_facts_that_agree_are_not_a_conflict(self):
        facts = [
            _f(1, "astor 的 public tier 存 public 数据"),
            _f(2, "astor 的 public tier 存 public 数据并且只出 commons"),
        ]
        self.assertEqual(scan_conflicts(facts, min_jaccard=0.5), [])

    def test_two_different_topics_are_not_a_conflict(self):
        """One of them contains a negation word, but they are about
        different things. The write-side rule fires on a shared entity
        alone; this one must not."""
        facts = [
            _f(1, "astor 的 public tier 存 public 数据"),
            _f(2, "maker_pocket 的日亏损上限是 1.5%，不是 9%"),
        ]
        self.assertEqual(scan_conflicts(facts, min_jaccard=0.6), [])

    def test_low_overlap_is_not_a_conflict(self):
        facts = [
            _f(1, "astor 的 public tier 存 public 数据"),
            _f(2, "完全无关的另一条事实，讲的是别的事情和别的词"),
        ]
        self.assertEqual(scan_conflicts(facts), [])

    def test_a_fact_without_an_id_is_skipped(self):
        facts = [
            {"content": "astor 的 public tier 存 public 数据"},
            _f(2, "astor 的 public tier 不存 private 数据"),
        ]
        self.assertEqual(scan_conflicts(facts, min_jaccard=0.5), [])

    def test_the_scan_reports_pairs_not_verdicts(self):
        """It says which pairs conflict; it does not hide anything."""
        facts = [
            _f(1, "astor 的 public tier 存 public 数据"),
            _f(2, "astor 的 public tier 不存 private 数据"),
            _f(3, "astor 的 private tier 存 private 数据"),
        ]
        pairs = scan_conflicts(facts, min_jaccard=0.5)
        self.assertTrue(all(len(p) == 3 for p in pairs))


if __name__ == "__main__":
    unittest.main()
