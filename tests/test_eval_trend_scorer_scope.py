"""The trend tools must not compare across a scorer change.

v1.17.13 (2026-10-28): eval_runner began recording scorer_hash -- the
hash of the scoring code itself -- because eval_set_hash alone cannot
tell "recall improved" from "the scorer was loosened".

The history contains runs from before that change. Those runs describe a
different measurement, so comparing against them reads a measurement
change as a recall regression. Worse, before the read-cache fix, a
variant comparison could not execute a variant at all -- so the old
numbers are not merely differently-measured, they are wrong.

Both trend readers (tests/eval_trend.py and astor_memory/dashboard_data.py)
must therefore restrict the window to runs under the current scorer.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TREND = Path(__file__).parent / "eval_trend.py"
_spec = importlib.util.spec_from_file_location("eval_trend", _TREND)
eval_trend = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eval_trend)


def _run(variant: str, hr: float, mrr: float, scorer: str | None) -> dict:
    return {
        "variant": variant, "hit_rate_at_k": hr, "mrr": mrr,
        "ts": "2026-10-28T00:00:00Z", "scorer_hash": scorer,
        "p95_latency_ms": 100,
    }


class TestScorerHashScoping(unittest.TestCase):

    def test_current_scorer_hash_is_the_latest_hashed_baseline(self):
        hist = [
            _run("baseline", 0.9, 0.8, "OLD"),
            _run("baseline", 0.93, 0.85, "NEW"),
            _run("baseline", 0.94, 0.86, "NEWER"),
        ]
        self.assertEqual(eval_trend._current_scorer_hash(hist), "NEWER")

    def test_current_scorer_hash_is_none_without_hashed_runs(self):
        hist = [_run("baseline", 0.9, 0.8, None)]
        self.assertIsNone(eval_trend._current_scorer_hash(hist))

    def test_window_excludes_runs_from_a_different_scorer(self):
        """The old number must not become the baseline."""
        hist = [
            _run("baseline", 0.98, 0.87, None),   # pre-hash, and fake
            _run("baseline", 0.93, 0.86, "CUR"),
            _run("baseline", 0.94, 0.87, "CUR"),
        ]
        cur = eval_trend._current_scorer_hash(hist)
        scoped = [h for h in hist
                  if h.get("variant") == "baseline"
                  and h.get("scorer_hash") == cur]
        self.assertEqual(len(scoped), 2)
        self.assertTrue(all(h["hit_rate_at_k"] < 0.98 for h in scoped),
                        "the pre-hash run must be excluded from the window")

    def test_dashboard_uses_the_same_rule(self):
        """dashboard_data._eval_trend is a separate implementation."""
        src = (ROOT / "astor_memory" / "dashboard_data.py").read_text(
            encoding="utf-8")
        self.assertIn("scorer_hash", src,
                      "the dashboard trend must scope by scorer hash too")


class TestScorerHashIsRecorded(unittest.TestCase):
    """eval_runner must record the scorer hash, or none of the above holds."""

    def test_eval_runner_records_scorer_hash(self):
        src = (Path(__file__).parent / "eval_runner.py").read_text(
            encoding="utf-8")
        self.assertIn("scorer_hash", src)
        self.assertIn("_scorer_hash", src)

    def test_scorer_hash_changes_when_the_scorer_changes(self):
        """A hash that ignores its input is not a hash."""
        path = Path(__file__).parent / "eval_runner.py"
        original = path.read_text(encoding="utf-8")
        try:
            spec = importlib.util.spec_from_file_location(
                "eval_runner_probe", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            h1 = mod._scorer_hash()
            path.write_text(original + "\n# a trailing comment\n", encoding="utf-8")
            spec2 = importlib.util.spec_from_file_location(
                "eval_runner_probe2", path)
            mod2 = importlib.util.module_from_spec(spec2)
            spec2.loader.exec_module(mod2)
            h2 = mod2._scorer_hash()
        finally:
            path.write_text(original, encoding="utf-8")
        self.assertTrue(h1)
        self.assertNotEqual(h1, h2,
                            "scorer_hash must change when the scorer file changes")


if __name__ == "__main__":
    unittest.main()
