"""A run that measured someone else's question set must not land on the
operator's trend track.

eval_history.jsonl is the ruler's track: eval_trend.py and the dashboard
read it to answer "did recall get better, or did the ruler change". A
row in that file is compared against, and averaged with, every other row
in it -- so a row that measured a DIFFERENT question set does not belong
there, no matter how it got there.

Two ways this went wrong, both observed live on this host:

1. A unit test called eval_runner's main() with a three-row scratch set.
   The runner appended the result to the shared history, so the file
   carried 22 rows of hit_rate 0.0 that were never a measurement of
   anything, and the "last 3 baselines" median that the snapshot is
   computed from was taken over those rows.

2. _eval_set_hash() hashed the DEFAULT set no matter which set was
   loaded, so a run against the paraphrase set recorded the default
   set's hash -- the one field that says which questions a run measured.
   The history claimed those runs were about the operator's set.

Both are one defect seen from two sides: the track has to know which set
each row measured, and only the operator's set belongs on it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).parent
EVAL_RUNNER = TESTS / "eval_runner.py"


def _runner(metrics_dir: Path):
    """Load eval_runner with its metrics dir pointed at `metrics_dir`."""
    os.environ["ASTOR_METRICS_DIR"] = str(metrics_dir)
    try:
        spec = importlib.util.spec_from_file_location(
            "eval_runner_pollution_test", EVAL_RUNNER)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["eval_runner_pollution_test"] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        os.environ.pop("ASTOR_METRICS_DIR", None)


class TestEvalHistoryNotPolluted(unittest.TestCase):

    def test_off_track_run_does_not_reach_the_shared_history(self):
        """A scratch-set run must not append to eval_history.jsonl."""
        with tempfile.TemporaryDirectory() as tmp:
            metrics = Path(tmp) / "metrics"
            mod = _runner(metrics)

            scratch = Path(tmp) / "scratch.jsonl"
            rows = [{"qid": f"X{i}", "query": "q", "expected_keywords": ["k"],
                     "min_hits": 1, "category": "t", "tier": "public",
                     "user": "admin", "top_k": 10} for i in range(3)]
            scratch.write_text(
                "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

            mod.recall = lambda **kw: ([], 1.0)
            argv = sys.argv
            sys.argv = ["eval_runner.py", "--set", str(scratch),
                        "--variant", "baseline"]
            try:
                mod.main()
            finally:
                sys.argv = argv

            shared = metrics / "eval_history.jsonl"
            self.assertFalse(
                shared.exists(),
                "a run against a scratch set wrote to the shared trend "
                "history -- the operator's ruler now averages a run that "
                "measured nothing they asked for")

            off = metrics / "eval_history_offtrack.jsonl"
            self.assertTrue(
                off.exists(),
                "the off-track run was not recorded anywhere -- it should "
                "be kept, just not on the shared track")
            recorded = [json.loads(l) for l in
                        off.read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(len(recorded), 1)

    def test_the_recorded_hash_is_the_set_that_was_measured(self):
        """The set hash must describe the set the run loaded."""
        with tempfile.TemporaryDirectory() as tmp:
            metrics = Path(tmp) / "metrics"
            mod = _runner(metrics)

            scratch = Path(tmp) / "scratch.jsonl"
            scratch.write_text(
                json.dumps({"qid": "X1", "query": "q", "expected_keywords": ["k"],
                            "min_hits": 1, "category": "t", "tier": "public",
                            "user": "admin", "top_k": 10}) + "\n",
                encoding="utf-8")

            default_hash = mod._eval_set_hash(mod.EVAL_SET)
            scratch_hash = mod._eval_set_hash(scratch)
            self.assertIsNotNone(scratch_hash)
            self.assertNotEqual(
                scratch_hash, default_hash,
                "the scratch set and the default set hash the same -- the "
                "hash cannot tell them apart")

            mod.recall = lambda **kw: ([], 1.0)
            argv = sys.argv
            sys.argv = ["eval_runner.py", "--set", str(scratch),
                        "--variant", "baseline"]
            try:
                mod.main()
            finally:
                sys.argv = argv

            off = metrics / "eval_history_offtrack.jsonl"
            recorded = [json.loads(l) for l in
                        off.read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(
                recorded[0].get("eval_set_hash"), scratch_hash,
                "the run recorded the DEFAULT set's hash while it measured "
                "the scratch set -- the field that says which questions a "
                "run measured is lying")

    def test_operator_set_run_does_reach_the_shared_history(self):
        """The fix must not silence the real ruler."""
        if not (TESTS / "eval_set.jsonl").exists():
            self.skipTest("the default eval set is the operator's own and "
                          "is gitignored")
        with tempfile.TemporaryDirectory() as tmp:
            metrics = Path(tmp) / "metrics"
            mod = _runner(metrics)

            mod.recall = lambda **kw: ([], 1.0)
            argv = sys.argv
            sys.argv = ["eval_runner.py", "--set",
                        str(TESTS / "eval_set.jsonl"), "--variant", "baseline"]
            try:
                mod.main()
            finally:
                sys.argv = argv

            shared = metrics / "eval_history.jsonl"
            self.assertTrue(
                shared.exists(),
                "a run against the operator's own set did not reach the "
                "shared history -- the ruler would have nothing to read")


if __name__ == "__main__":
    unittest.main()
