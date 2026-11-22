"""--set has to actually load the set it was given.

The runner accepted --set, printed the path, and then loaded the
default set anyway: `eval_set = load_eval_set()` read the module
constant. Every run with a custom set was silently the default set,
which is how a paraphrase set came back with the default set's
numbers.
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


def _runner(metrics_dir: Path | None = None):
    """Load eval_runner, optionally with its metrics dir redirected.

    main() writes a run's detail/summary files into the metrics dir. The
    operator's live one (ROOT/astor/metrics) holds the eval history the
    dashboard reads, so a test that calls main() must not write there:
    this test used to leave a row in the operator's trend history for
    every run it made.
    """
    if metrics_dir is not None:
        os.environ["ASTOR_METRICS_DIR"] = str(metrics_dir)
    try:
        spec = importlib.util.spec_from_file_location(
            "eval_runner_path_test", TESTS / "eval_runner.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["eval_runner_path_test"] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        if metrics_dir is not None:
            os.environ.pop("ASTOR_METRICS_DIR", None)


class TestEvalSetPathIsUsed(unittest.TestCase):

    def test_load_eval_set_reads_the_given_path(self):
        mod = _runner()
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "custom.jsonl"
            rows = [{"qid": "X1", "query": "q", "expected_keywords": ["k"]}]
            p.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                         encoding="utf-8")
            loaded = mod.load_eval_set(p)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["qid"], "X1")

    def test_main_actually_loads_the_given_set(self):
        """The call site, not just the helper.

        The bug was in main(): it printed the path it was given and
        then loaded the default set. Testing load_eval_set(path) alone
        cannot see that.
        """
        with tempfile.TemporaryDirectory() as tmp:
            metrics = Path(tmp) / "metrics"
            mod = _runner(metrics)
            p = Path(tmp) / "custom.jsonl"
            rows = [{"qid": f"X{i}", "query": "q", "expected_keywords": ["k"],
                     "min_hits": 1, "category": "t", "tier": "public",
                     "user": "admin", "top_k": 10}
                    for i in range(3)]
            p.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                         encoding="utf-8")

            seen = {}

            def _fake_recall(**kw):
                seen.setdefault("queries", []).append(kw.get("query"))
                return [], 1.0

            original = mod.recall
            mod.recall = _fake_recall
            argv = sys.argv
            sys.argv = ["eval_runner.py", "--set", str(p),
                        "--variant", "baseline"]
            try:
                mod.main()
            finally:
                sys.argv = argv
                mod.recall = original
            self.assertEqual(
                len(seen.get("queries", [])), 3,
                "main() ran a different number of queries than the custom "
                "set holds -- the --set path was ignored")

    @unittest.skipUnless(
        (TESTS / "eval_set.jsonl").exists(),
        "the default eval set is the operator's own and is gitignored, "
        "so a public checkout does not have it to compare against")

    def test_the_paraphrase_set_is_not_the_default_set(self):
        mod = _runner()
        default = mod.load_eval_set()
        para = mod.load_eval_set(TESTS / "eval_paraphrase_set.jsonl")
        self.assertTrue(para, "the paraphrase set is empty")
        self.assertNotEqual(
            len(para), len(default),
            "the paraphrase set is the same size as the default set")
        for row in para:
            self.assertTrue(row.get("paraphrase"))
            for kw in row["expected_keywords"]:
                self.assertNotIn(
                    kw.lower(), row["query"].lower(),
                    "a paraphrase query contains its own expected keyword "
                    "-- the query would not be a paraphrase")


if __name__ == "__main__":
    unittest.main()
