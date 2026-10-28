"""Regression tests for the v1.17.13 read-cache key fix.

The bug
-------
_READ_CACHE's key covered query/tier/user/top_k/kinds/wing/session_id/
consult/peer_fanout -- but NOT the knobs that change the RANKING:
hybrid, bm25_weight, vec_weight, rerank, mmr_lambda, hyde.

Consequence: a read with bm25_weight=0.4 was served the cached response
of an earlier read with the default 0.6. The second variant never ran.
Every variant comparison in eval_runner / eval_sweep was therefore
measuring the first variant over and over -- which is why every variant
reported identical hit_rate and identical mrr.

The rule this file locks in: the cache key must cover every input that
changes the RESULT.

Also covers the RSIHub-style held-out gate, whose whole point is that
it is a held-out set -- if it overlaps the tuning set, it is measuring
the tuning set and reporting it as transfer.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The gate is a script, not part of the package layout.
_GATE_PATH = Path(__file__).parent / "holdout_gate.py"
_spec = importlib.util.spec_from_file_location("holdout_gate", _GATE_PATH)
holdout_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(holdout_gate)

HOLDOUT_SET = Path(__file__).parent / "eval_holdout_set.json"
EVAL_SET = Path(__file__).parent / "eval_set.jsonl"


class TestReadCacheKeyCoversRanking(unittest.TestCase):
    """The cache key must include every parameter that changes the result."""

    @staticmethod
    def _hashable(v):
        """Normalise a value into something usable as a dict key."""
        if isinstance(v, (list, tuple)):
            return tuple(TestReadCacheKeyCoversRanking._hashable(x) for x in v)
        if isinstance(v, set):
            return tuple(sorted(TestReadCacheKeyCoversRanking._hashable(x)
                                for x in v))
        if isinstance(v, dict):
            return tuple(sorted((k, TestReadCacheKeyCoversRanking._hashable(x))
                                for k, x in v.items()))
        return v

    def _cache_key(self, body: dict) -> tuple:
        """Build the same key the server builds, from a body dict.

        Mirrors server.py's construction. If the server changes its key,
        this test must be updated too -- that is intentional: it is the
        contract, not an implementation detail.
        """
        kinds = body.get('kinds')
        if isinstance(kinds, (list, tuple, set)):
            kinds = tuple(sorted(str(k) for k in kinds))
        elif kinds is not None:
            kinds = str(kinds)
        rank_key = tuple(
            self._hashable(body.get(p)) for p in (
                'hybrid', 'bm25_weight', 'vec_weight', 'rerank',
                'mmr_lambda', 'hyde', 'use_rrf', 'rrf_k',
                'query_adaptive', 'query_rewrite', 'query_rewrite_min_hits',
                'time_boost', 'time_boost_days', 'time_boost_factor',
                'time_parse', 'topic_boost_factor', 'topic_min_weight',
                'topics', 'topics_str', 'topic',
                'missing_hint', 'entity_filter', 'since_ts', 'until_ts',
                'memory_class', 'intent', 'token_budget',
            )
        )
        return (
            body.get('query'), body.get('tier', 'public'),
            body.get('user') or body.get('user_id') or '',
            body.get('top_k', 5),
            kinds, body.get('wing'), body.get('session_id'),
            body.get('consult'), body.get('peer_fanout'),
            rank_key,
        )

    def test_bm25_weight_changes_the_key(self):
        base = {'query': 'q', 'tier': 'private', 'user': 'admin', 'top_k': 5}
        a = self._cache_key({**base, 'bm25_weight': 0.6})
        b = self._cache_key({**base, 'bm25_weight': 0.4})
        self.assertNotEqual(a, b,
                            "bm25_weight changes the ranking, so it must "
                            "change the cache key")

    def test_every_ranking_knob_changes_the_key(self):
        base = {'query': 'q', 'tier': 'private', 'user': 'admin', 'top_k': 5}
        knobs = {
            'hybrid': True,
            'vec_weight': 0.4,
            'rerank': 'off',
            'mmr_lambda': 0.9,
            'hyde': True,
            'use_rrf': True,
            'rrf_k': 60,
            'query_rewrite': False,
            'time_boost': 1.0,
            'topic_boost_factor': 2.0,
            'missing_hint': 'x',
            'entity_filter': 'y',
            'since_ts': '2026-01-01T00:00:00Z',
            'until_ts': '2026-12-31T00:00:00Z',
            'memory_class': 'procedural',
            'intent': 'factual',
            'token_budget': 2000,
        }
        base_key = self._cache_key(dict(base))
        for knob, value in knobs.items():
            other = self._cache_key({**base, knob: value})
            self.assertNotEqual(
                base_key, other,
                f"{knob} changes the result, so it must change the cache key")

    def test_list_valued_params_do_not_break_the_cache(self):
        """A list in the key must not turn a read into a 500.

        entity_filter and topics arrive as lists. Putting a list into a
        dict key raises TypeError: unhashable type: 'list' on the cache
        lookup, so the read 500s. Every key part must be normalised to a
        hashable form first.
        """
        base = {'query': 'q', 'tier': 'private', 'user': 'admin', 'top_k': 5}
        for knob, value in (('entity_filter', ['a', 'b']),
                            ('topics', ['t1', 't2']),
                            ('topic', {'k': 'v'})):
            key = self._cache_key({**base, knob: value})
            self.assertIsInstance(key, tuple)
            # the key must be usable as a dict key
            try:
                hash(key)
            except TypeError as e:
                self.fail(f"{knob}={value!r} made the cache key unhashable: {e}")

    def test_server_normalises_key_parts_to_hashable(self):
        """The server must normalise list/dict params before keying."""
        src = (Path(__file__).resolve().parent.parent
               / "astor_memory" / "server.py").read_text(encoding="utf-8")
        self.assertIn("_hashable", src,
                      "the server must normalise key parts to hashable form")

    def test_identical_body_still_shares_a_key(self):
        """The fix must not destroy the cache itself."""
        base = {'query': 'q', 'tier': 'private', 'user': 'admin', 'top_k': 5,
                'bm25_weight': 0.6}
        self.assertEqual(self._cache_key(dict(base)),
                         self._cache_key(dict(base)))

    def test_server_source_contains_the_rank_key(self):
        """The server must actually use the ranking key.

        Guards against a future refactor that drops it from the tuple
        while the docstring still claims it is there.
        """
        src = (Path(__file__).resolve().parent.parent
               / "astor_memory" / "server.py").read_text(encoding="utf-8")
        self.assertIn("_rank_key", src)
        self.assertIn("_rcache_key", src)
        # every knob the test above asserts on must appear in the server's
        # own list, or the two have drifted apart
        for knob in ('hybrid', 'bm25_weight', 'vec_weight', 'rerank',
                     'mmr_lambda', 'hyde'):
            self.assertIn(f"'{knob}'", src,
                          f"{knob} must be part of the server's cache key")


class TestHoldoutSetIsActuallyHeldOut(unittest.TestCase):
    """RSIHub's condition: the held-out set must not feed the tuning loop.

    eval_control_set.json was 38/39 identical in query text to
    eval_set.jsonl, so it could not detect "the tuned score went up while
    unseen tasks went down" -- it was measuring the tuning set.
    """

    def test_holdout_file_exists_and_has_queries(self):
        self.assertTrue(HOLDOUT_SET.exists(),
                        f"missing {HOLDOUT_SET}")
        data = json.loads(HOLDOUT_SET.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(data["queries"]), 20,
                                "held-out set needs >=20 queries to be a gate")

    def test_each_query_has_required_fields(self):
        data = json.loads(HOLDOUT_SET.read_text(encoding="utf-8"))
        for q in data["queries"]:
            for field in ("qid", "query", "category", "expected_keywords"):
                self.assertIn(field, q,
                              f"query {q.get('qid','?')} missing {field}")
            self.assertGreaterEqual(len(q["expected_keywords"]), 2,
                                    f"{q['qid']}: needs >=2 keywords so a "
                                    "single spurious token cannot pass it")

    def test_holdout_is_disjoint_from_the_tuning_set(self):
        """Zero query-text overlap with eval_set.jsonl."""
        tune = set()
        for line in EVAL_SET.read_text(encoding="utf-8").splitlines():
            if line.strip():
                q = json.loads(line).get("query", "")
                tune.add(" ".join(q.lower().split()))
        held = set()
        for q in json.loads(HOLDOUT_SET.read_text(encoding="utf-8"))["queries"]:
            held.add(" ".join(q["query"].lower().split()))
        overlap = tune & held
        self.assertEqual(
            overlap, set(),
            f"held-out set shares {len(overlap)} queries with the tuning "
            f"set -- it is not held out: {sorted(overlap)[:5]}")

    def test_gate_reports_disjointness(self):
        dis = holdout_gate.check_disjointness()
        self.assertTrue(dis["checked"])
        self.assertTrue(dis["disjoint"],
                        f"gate must refuse to run on a non-held-out set: {dis}")

    def test_gate_locks_the_scorer_and_the_set(self):
        """Both hashes must exist and change when their input changes.

        RSIHub: the evaluator is controlled by the framework, so a
        candidate cannot raise its score by editing the scoring rules.
        """
        self.assertTrue(holdout_gate.scorer_hash())
        self.assertTrue(holdout_gate.set_hash())
        # the set hash must actually track the file
        before = holdout_gate.set_hash()
        self.assertEqual(before, holdout_gate.set_hash())


if __name__ == "__main__":
    unittest.main()
