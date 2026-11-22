"""A run measured against a different question set is not comparable.

The trend tools average baseline runs to answer "did recall get better,
or did the ruler change". Two runs can only answer that question
together if they measured the same questions and were scored by the
same rule. A run against a different set answers a different question.

This is not a reporting nicety: _regression_check.py rolls the bm25
weight back when it reads a regression. A run from another set in the
window makes it roll a good weight back for a regression that never
happened.

The scorer-hash half of this guard predates it; the set-hash half is
what the runner's hash field is FOR, and it was not being read by
anyone.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _row(**kw):
    row = {
        "variant": "baseline",
        "ts": "2026-11-22T00:00:00+00:00",
        "hit_rate_at_k": 0.98,
        "mrr": 0.82,
        "scorer_hash": "sc1",
        "eval_set_hash": "set1",
        "kwargs": {},
    }
    row.update(kw)
    return row


class TestOnlyComparableRunsAreCompared(unittest.TestCase):

    def test_a_run_from_another_set_is_excluded(self):
        reg = _load("_reg_check_under_test", TESTS / "_regression_check.py")
        lines = [
            _row(ts="2026-11-21T00:00:00+00:00", hit_rate_at_k=0.98),
            # a different set, appended last: it is the most recent run,
            # so it defines the current set
            _row(ts="2026-11-22T00:00:00+00:00", eval_set_hash="set2",
                 hit_rate_at_k=0.10, mrr=0.05),
        ]
        kept = reg._comparable_baselines(lines)
        self.assertEqual([r["eval_set_hash"] for r in kept], ["set2"],
                         "runs against the old set were kept and averaged "
                         "with a run against a different set")

    def test_a_run_from_another_scorer_is_excluded(self):
        reg = _load("_reg_check_under_test2", TESTS / "_regression_check.py")
        lines = [
            _row(ts="2026-11-21T00:00:00+00:00", scorer_hash="sc0"),
            _row(ts="2026-11-22T00:00:00+00:00", scorer_hash="sc1"),
        ]
        kept = reg._comparable_baselines(lines)
        self.assertEqual([r["scorer_hash"] for r in kept], ["sc1"])

    def test_runs_without_a_hash_are_left_alone(self):
        """An unhashed history cannot be compared -- but it is not lost."""
        reg = _load("_reg_check_under_test3", TESTS / "_regression_check.py")
        lines = [
            _row(ts="2026-11-21T00:00:00+00:00", eval_set_hash=None,
                 scorer_hash=None),
            _row(ts="2026-11-22T00:00:00+00:00", eval_set_hash=None,
                 scorer_hash=None),
        ]
        kept = reg._comparable_baselines(lines)
        self.assertEqual(len(kept), 2)

    def test_the_trend_tools_apply_the_same_rule(self):
        """eval_trend.py and the dashboard must agree with each other.

        The dashboard carries its own copy of the window logic. If one
        filters by set and the other does not, they report two different
        truths about the same history.
        """
        trend = _load("_eval_trend_under_test", TESTS / "eval_trend.py")
        history = [
            _row(ts="2026-11-21T00:00:00+00:00", hit_rate_at_k=0.98),
            _row(ts="2026-11-22T00:00:00+00:00", eval_set_hash="set2",
                 hit_rate_at_k=0.10),
        ]
        self.assertEqual(trend._current_set_hash(history), "set2")
        self.assertEqual(trend._current_scorer_hash(history), "sc1")

    def test_the_runner_records_the_set_it_measured(self):
        """The hash must describe the set that was loaded."""
        runner = _load("_eval_runner_under_test", TESTS / "eval_runner.py")
        default = runner.EVAL_SET
        if not default.exists():
            self.skipTest("the default eval set is the operator's own and "
                          "is gitignored")
        para = TESTS / "eval_paraphrase_set.jsonl"
        self.assertNotEqual(runner._eval_set_hash(default),
                            runner._eval_set_hash(para),
                            "the two sets hash the same -- the hash cannot "
                            "tell which questions a run measured")


if __name__ == "__main__":
    unittest.main()
