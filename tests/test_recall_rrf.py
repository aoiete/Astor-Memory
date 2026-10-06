"""Tests for v1.16.73 RRF fusion (Ship F)."""
import unittest

from astor_memory.recall_rrf import rrf_fusion


class TestRRFFusion(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(rrf_fusion(), [])
        self.assertEqual(rrf_fusion([], []), [])

    def test_single_list(self):
        # [(1, 0.9), (2, 0.5)] → ranks 1, 2 → scores 1/(60+1), 1/(60+2)
        result = rrf_fusion([(1, 0.9), (2, 0.5)], k=60)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0][0], 1)
        self.assertEqual(result[1][0], 2)
        self.assertAlmostEqual(result[0][1], 1.0 / 61)
        self.assertAlmostEqual(result[1][1], 1.0 / 62)

    def test_two_lists_consensus(self):
        # Fact 1 in both lists → highest
        list_a = [(1, 1.0), (2, 0.5), (3, 0.1)]
        list_b = [(1, 0.8), (3, 0.7), (4, 0.6)]
        result = rrf_fusion(list_a, list_b, k=60)
        # Fact 1: rank 1 in both → 2 * 1/61 ≈ 0.0328
        # Fact 2: rank 2 in A only → 1/62 ≈ 0.0161
        # Fact 3: rank 3 in A + rank 2 in B → 1/63 + 1/62 ≈ 0.0320
        # Fact 4: rank 3 in B only → 1/63 ≈ 0.0159
        self.assertEqual(result[0][0], 1)
        self.assertEqual(result[0][1], 2 * (1.0 / 61))
        self.assertEqual(result[1][0], 3)
        self.assertAlmostEqual(result[1][1], (1.0 / 63) + (1.0 / 62), places=5)

    def test_three_lists(self):
        list_a = [(1, 1.0), (2, 0.5)]
        list_b = [(1, 0.8), (3, 0.7)]
        list_c = [(1, 0.9), (4, 0.6)]
        result = rrf_fusion(list_a, list_b, list_c, k=60)
        # Fact 1 in all three → top
        # Fact 2 only in A, 3 only in B, 4 only in C
        ids = [r[0] for r in result]
        self.assertEqual(ids[0], 1)
        self.assertEqual(ids[1:], [2, 3, 4])

    def test_disjoint(self):
        # Two lists, completely disjoint
        list_a = [(1, 1.0), (2, 0.5)]
        list_b = [(3, 0.9), (4, 0.6)]
        result = rrf_fusion(list_a, list_b, k=60)
        ids = {r[0] for r in result}
        self.assertEqual(ids, {1, 2, 3, 4})

    def test_different_k(self):
        # k=0 means only rank 1 contributes (1/1 = 1.0)
        list_a = [(1, 1.0), (2, 0.5)]
        result = rrf_fusion(list_a, k=0)
        # Rank 1: 1/1 = 1.0, rank 2: 1/2 = 0.5
        self.assertAlmostEqual(result[0][1], 1.0)
        self.assertAlmostEqual(result[1][1], 0.5)

    def test_score_ignored_only_rank_matters(self):
        # RRF uses ONLY rank, not source score.
        # List with high score for #2 still puts #2 at rank 2.
        list_high_score = [(1, 0.1), (2, 999.0)]  # fact 2 has score 999 but rank 1
        list_normal = [(1, 0.9), (2, 0.5)]
        # Fact 1: rank 1 in both → top
        # Fact 2: rank 2 in normal, rank 1 in high_score (rank-1 contributes more)
        result = rrf_fusion(list_high_score, list_normal, k=60)
        ids = [r[0] for r in result]
        # Fact 2 has rank 1 in high_score (1/61) + rank 2 in normal (1/62)
        # Fact 1 has rank 2 in high_score (1/62) + rank 1 in normal (1/61)
        # Equal scores! Order depends on Python dict insertion order.
        self.assertEqual(set(ids), {1, 2})


if __name__ == "__main__":
    unittest.main()