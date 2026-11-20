"""A weight of exactly 0.0 is a real request, not a missing one.

The read path took the ranking weights from the body with
`body.get('bm25_weight') or default`. 0.0 is falsy, so a request for
a pure lexical ranking silently got the default vector weight instead.
That made the naive baseline arm -- "the corpus as a bag of words plus
a search tool" -- impossible to ask for, and there was no way to know
the difference between the two.

It also fixes the eval arm itself: eval_runner's bm25_only variant
asks for bm25_weight=1.0 / vec_weight=0.0.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astor_memory.server import _resolve_rank_weights  # noqa: E402


class TestRankWeightsResolveZero(unittest.TestCase):

    def test_the_defaults_still_apply(self):
        bm25, vec = _resolve_rank_weights({})
        self.assertAlmostEqual(bm25, 0.6)
        self.assertAlmostEqual(vec, 0.4)

    def test_a_zero_vector_weight_survives(self):
        bm25, vec = _resolve_rank_weights(
            {"bm25_weight": 1.0, "vec_weight": 0.0})
        self.assertAlmostEqual(
            vec, 0.0,
            "a vec_weight of 0.0 was treated as missing -- the naive "
            "lexical arm could not be requested at all")
        self.assertAlmostEqual(bm25, 1.0)

    def test_a_zero_bm25_weight_survives(self):
        bm25, vec = _resolve_rank_weights(
            {"bm25_weight": 0.0, "vec_weight": 1.0})
        self.assertAlmostEqual(
            bm25, 0.0,
            "a bm25_weight of 0.0 was treated as missing")

    def test_the_env_is_only_a_fallback(self):
        old = os.environ.get("ASTOR_BM25_WEIGHT")
        os.environ["ASTOR_BM25_WEIGHT"] = "0.9"
        try:
            bm25, _ = _resolve_rank_weights({"bm25_weight": 1.0})
            self.assertAlmostEqual(bm25, 1.0)
            bm25, _ = _resolve_rank_weights({})
            self.assertAlmostEqual(bm25, 0.9)
        finally:
            if old is None:
                os.environ.pop("ASTOR_BM25_WEIGHT", None)
            else:
                os.environ["ASTOR_BM25_WEIGHT"] = old


class TestEvalNaiveArm(unittest.TestCase):

    def _runner(self):
        path = Path(__file__).parent / "eval_runner.py"
        spec = importlib.util.spec_from_file_location("eval_runner_mod", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["eval_runner_mod"] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_the_naive_arm_exists_and_asks_for_pure_bm25(self):
        mod = self._runner()
        # the variant table lives inside run_variant; read it back the
        # cheap way -- call it with an empty set and read the body the
        # helper would send.
        sent = {}

        def _fake_recall(**kw):
            sent.update(kw)
            return [], 1.0

        original = mod.recall
        mod.recall = _fake_recall
        try:
            mod.run_variant("bm25_only", [{"qid": "q1", "query": "q",
                                            "expected_keywords": ["x"]}])
        finally:
            mod.recall = original
        self.assertTrue(sent, "the variant never called read")
        self.assertEqual(sent.get("bm25_weight"), 1.0)
        self.assertEqual(sent.get("vec_weight"), 0.0)
        self.assertEqual(sent.get("rerank"), "off")


if __name__ == "__main__":
    unittest.main()
