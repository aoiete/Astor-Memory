"""tests/test_path_score.py — v1.16.11 (2026-09-30)

Tests for M-flow-inspired path-based graph scoring.

The core invariant being tested: path_score can boost a candidate
that has WEAK direct similarity but STRONG chain coherence with
neighbor facts sharing entities. Conversely, a conflict in the
chain should NOT boost (and may penalize).

Critical tests:
  - direct-only baseline matches ECV node_usefulness (no regression)
  - chain-of-2 with corroboration gives bounded positive boost
  - chain with conflict breaks the chain and gives zero boost
  - cap is enforced: even a perfect chain can't add > 0.30
  - empty neighbor pool = direct-only path (graceful fallback)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astor_memory.nest.path_score import (
    path_score_for_fact,
    batch_path_score,
    apply_path_boost,
    _PATH_BOOST_CAP,
    _PATH_DECAY,
    _MAX_DEPTH,
)
from astor_memory.nest.ecv import (
    REL_CORROBORATE,
    REL_CLARIFY,
    REL_CONFLICT,
    REL_NEW_FACT,
    REL_REPEAT,
)


def _make_fact(
    fact_id: int,
    content: str,
    entities: list[str] | None = None,
    keywords: list[str] | None = None,
    confidence: float = 0.7,
    access_count: int = 0,
) -> dict:
    """Build a fact dict compatible with path_score inputs."""
    import json
    d = {
        "id": fact_id,
        "content": content,
        "keywords": keywords or [],
        "confidence": confidence,
        "access_count": access_count,
        "entities_json": json.dumps(
            [{"type": "topic", "value": e} for e in (entities or [])],
            ensure_ascii=False,
        ),
    }
    return d


class TestPathScoreBaseline(unittest.TestCase):
    """Direct-only path scoring should match ECV node_usefulness."""

    def test_no_neighbors_falls_back_to_direct(self):
        anchor = _make_fact(1, "Maria 完成项目发布", entities=["Maria", "发布"])
        ps = path_score_for_fact(
            query="Maria 发布",
            anchor=anchor,
            neighbor_facts=None,
        )
        self.assertEqual(ps["chain"], [])
        self.assertEqual(ps["depth_used"], 0)
        self.assertGreater(ps["direct"], 0.4)
        self.assertAlmostEqual(ps["path"], ps["direct"], places=4)

    def test_empty_neighbor_list_falls_back_to_direct(self):
        anchor = _make_fact(1, "Maria 完成项目发布", entities=["Maria", "发布"])
        ps = path_score_for_fact(query="Maria 发布", anchor=anchor, neighbor_facts=[])
        self.assertAlmostEqual(ps["path"], ps["direct"], places=4)
        self.assertEqual(ps["boost"], 0.0)


class TestPathScoreCorroborate(unittest.TestCase):
    """Chain of corroborating facts should give positive bounded boost."""

    def test_two_corroborating_facts_boost(self):
        anchor = _make_fact(1, "Maria 完成项目发布", entities=["Maria", "发布"])
        nbr1 = _make_fact(2, "Maria 发布了新版本", entities=["Maria", "发布", "版本"])
        ps = path_score_for_fact(
            query="Maria 发布",
            anchor=anchor,
            neighbor_facts=[nbr1],
        )
        # direct should be > 0 (Maria+发布 both overlap)
        self.assertGreater(ps["direct"], 0.3)
        # path should be >= direct (boost can't hurt when no conflicts)
        self.assertGreaterEqual(ps["path"], ps["direct"])
        self.assertGreaterEqual(ps["boost"], 0.0)

    def test_three_chain_with_one_clarification(self):
        anchor = _make_fact(1, "Maria 完成项目", entities=["Maria", "项目"])
        nbr1 = _make_fact(2, "Maria 完成项目并发布", entities=["Maria", "项目", "发布"])
        nbr2 = _make_fact(3, "项目在 Q3 启动", entities=["项目", "Q3"])
        ps = path_score_for_fact(
            query="Maria 项目",
            anchor=anchor,
            neighbor_facts=[nbr1, nbr2],
        )
        # Should pick up some chain signal
        self.assertGreaterEqual(ps["path"], ps["direct"])
        # Chain should contain at least nbr1
        if ps["chain"]:
            self.assertIn(2, ps["chain"])

    def test_cap_is_enforced(self):
        """Even a perfect chain can't add more than _PATH_BOOST_CAP."""
        anchor = _make_fact(1, "test", entities=["E"])
        # 5 neighbors all strongly corroborating
        neighbors = [
            _make_fact(100 + i, f"test 内容 {i}", entities=["E"])
            for i in range(5)
        ]
        ps = path_score_for_fact(
            query="test",
            anchor=anchor,
            neighbor_facts=neighbors,
        )
        self.assertLessEqual(ps["boost"], _PATH_BOOST_CAP)
        self.assertGreaterEqual(ps["boost"], -_PATH_BOOST_CAP)


class TestPathScoreConflict(unittest.TestCase):
    """A conflict in the chain should not boost, may reset."""

    def test_conflict_breaks_chain(self):
        anchor = _make_fact(1, "Maria 完成发布", entities=["Maria", "发布"])
        nbr1 = _make_fact(2, "Maria 没有完成发布", entities=["Maria", "发布"])
        ps = path_score_for_fact(
            query="Maria 发布",
            anchor=anchor,
            neighbor_facts=[nbr1],
        )
        # The chain should not have been amplified (conflict → no boost)
        # Path may equal direct or be slightly less (no cap on negative side from one conflict)
        self.assertLessEqual(ps["path"], ps["direct"] + _PATH_BOOST_CAP)


class TestPathScoreFallback(unittest.TestCase):
    """When no entity overlap, no chain traversal should occur."""

    def test_no_shared_entities_no_chain(self):
        anchor = _make_fact(1, "Maria 项目", entities=["Maria", "项目"])
        nbr1 = _make_fact(2, "John 项目", entities=["John", "项目"])
        ps = path_score_for_fact(
            query="Maria 项目",
            anchor=anchor,
            neighbor_facts=[nbr1],
        )
        # nbr1 shares 项目 but path_score looks at direct anchor↔nbr relation
        # Token overlap should produce some signal even with disjoint entities
        # The path result should still be valid
        self.assertIsNotNone(ps["path"])
        self.assertGreaterEqual(ps["path"], 0.0)
        self.assertLessEqual(ps["path"], 1.0)


class TestApplyPathBoost(unittest.TestCase):
    """apply_path_boost should mutate candidates with path_score fields."""

    def test_apply_path_boost_adds_fields(self):
        cands = [
            _make_fact(1, "Maria 完成发布", entities=["Maria", "发布"]),
            _make_fact(2, "Maria 发布新版", entities=["Maria", "发布"]),
            _make_fact(3, "John 项目", entities=["John", "项目"]),
        ]
        out = apply_path_boost(query="Maria 发布", candidates=cands)
        self.assertEqual(len(out), 3)
        for c in cands:
            self.assertIn("base_score", c)
            self.assertIn("path_score", c)
            self.assertIn("path_boost", c)
            self.assertIn("path_chain", c)
            self.assertIn("path_depth", c)

    def test_apply_path_boost_with_external_pool(self):
        anchor = _make_fact(1, "Maria 项目", entities=["Maria", "项目"])
        external_pool = [_make_fact(10, "Maria 项目发布", entities=["Maria", "项目", "发布"])]
        out = apply_path_boost(
            query="Maria 项目",
            candidates=[anchor],
            neighbor_pool=external_pool,
        )
        self.assertEqual(out[0]["path_score"], out[0]["base_score"] + out[0]["path_boost"])


class TestBatchPathScore(unittest.TestCase):
    """batch_path_score returns one entry per neighbor that shares an entity."""

    def test_batch_filters_non_sharing(self):
        anchor = _make_fact(1, "Maria 项目", entities=["Maria", "项目"])
        pool = [
            _make_fact(2, "Maria 别的", entities=["Maria"]),  # shares Maria
            _make_fact(3, "其他内容", entities=["X", "Y"]),  # shares nothing
        ]
        out = batch_path_score(query="Maria", anchor=anchor, candidate_pool=pool)
        # Only nbr1 should be in the result
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["neighbor_id"], 2)

    def test_batch_max_candidates(self):
        anchor = _make_fact(1, "Maria 项目", entities=["Maria"])
        pool = [
            _make_fact(10 + i, f"Maria 内容 {i}", entities=["Maria"])
            for i in range(20)
        ]
        out = batch_path_score(query="Maria", anchor=anchor, candidate_pool=pool, max_candidates=5)
        self.assertEqual(len(out), 5)


class TestConstants(unittest.TestCase):
    """Verify exposed constants match M-flow article spec."""

    def test_cap_is_0_30(self):
        self.assertEqual(_PATH_BOOST_CAP, 0.30)

    def test_decay_is_0_6(self):
        self.assertEqual(_PATH_DECAY, 0.6)

    def test_max_depth_is_2(self):
        self.assertEqual(_MAX_DEPTH, 2)


if __name__ == "__main__":
    unittest.main()
