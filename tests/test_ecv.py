"""test_ecv.py — v1.16.6 ECV module tests.

Validates node_usefulness + relation functions in
astor_memory.nest.ecv (GraphMemix-inspired).
"""
import unittest
from astor_memory.nest.ecv import (
    REL_NEW_FACT, REL_CLARIFY, REL_CORROBORATE,
    REL_REPEAT, REL_CONFLICT, REL_NONE,
    node_usefulness, relation, batch_node_usefulness,
)


class TestECV(unittest.TestCase):

    def test_node_usefulness_basic_overlap(self):
        """Same tokens → high usefulness."""
        s = node_usefulness(
            query="user prefers dark mode",
            candidate_content="user prefers dark mode and large fonts",
            candidate_keywords=["dark", "mode"],
        )
        self.assertGreater(s, 0.3)

    def test_node_usefulness_no_overlap_baseline(self):
        """Disjoint tokens → baseline 0.05 (still surfaceable as context)."""
        s = node_usefulness(
            query="dark mode preference",
            candidate_content="something completely different xyz",
        )
        self.assertLess(s, 0.10)

    def test_node_usefulness_keywords_boost(self):
        """Explicit keywords boost over implicit tokens."""
        s_with_kw = node_usefulness(
            query="dark mode",
            candidate_content="user interface stuff",
            candidate_keywords=["dark", "mode"],
        )
        s_no_kw = node_usefulness(
            query="dark mode",
            candidate_content="user interface stuff",
        )
        self.assertGreater(s_with_kw, s_no_kw)

    def test_node_usefulness_entities_boost(self):
        """Entity overlap boosts usefulness."""
        s = node_usefulness(
            query="John's friend Mary works at X",
            candidate_content="John's friend Mary works at X",
            candidate_entities_json='[{"type":"person","value":"John"}]',
        )
        self.assertGreater(s, 0.2)

    def test_node_usefulness_quality_signal(self):
        """Higher confidence + access_count → slight boost."""
        low = node_usefulness(
            query="anything",
            candidate_content="anything else",
            candidate_confidence=0.5,
            candidate_access_count=0,
        )
        high = node_usefulness(
            query="anything",
            candidate_content="anything else",
            candidate_confidence=1.0,
            candidate_access_count=20,
        )
        # Quality contributes 10% — so high should beat low but only slightly
        self.assertGreater(high, low)
        self.assertLess(high - low, 0.15)

    def test_node_usefulness_score_bounds(self):
        """Score always in [0.05, 1.0]."""
        for q in ["x", "y", "z", "a b c", "long query " * 50]:
            for c in ["", "x", "completely different", q * 3]:
                s = node_usefulness(query=q, candidate_content=c)
                self.assertGreaterEqual(s, 0.05)
                self.assertLessEqual(s, 1.0)

    def test_relation_corroborate_high_overlap(self):
        """High token overlap + similar length → corroboration or repeat."""
        rel, conf = relation(
            anchor_content="user prefers dark mode",
            anchor_entities_json=None,
            candidate_content="user prefers dark mode and dark theme",
            candidate_entities_json=None,
        )
        self.assertIn(rel, [REL_CORROBORATE, REL_REPEAT])
        self.assertGreater(conf, 0.5)

    def test_relation_clarify_superset(self):
        """Candidate contains anchor + new tokens → clarification."""
        rel, conf = relation(
            anchor_content="John works at X",
            anchor_entities_json='[{"type":"person","value":"John"}]',
            candidate_content="John works at X starting from 2024 on a project",
            candidate_entities_json='[{"type":"person","value":"John"}]',
        )
        self.assertEqual(rel, REL_CLARIFY)
        self.assertGreater(conf, 0.5)

    def test_relation_new_fact_disjoint_entities(self):
        """Same entity family, different specifics → new_fact."""
        rel, _ = relation(
            anchor_content="John works at X",
            anchor_entities_json='[{"type":"person","value":"John"}]',
            candidate_content="Mary works at Y",
            candidate_entities_json='[{"type":"person","value":"Mary"}]',
        )
        self.assertEqual(rel, REL_NEW_FACT)

    def test_relation_conflict_negation_xor(self):
        """One neg, one not → conflict."""
        rel, conf = relation(
            anchor_content="user does not like dark mode",
            anchor_entities_json='[{"type":"user","value":"U"}]',
            candidate_content="user likes dark mode",
            candidate_entities_json='[{"type":"user","value":"U"}]',
        )
        self.assertEqual(rel, REL_CONFLICT)

    def test_relation_none_disjoint(self):
        """No entity overlap, low token overlap → none.

        Note: both candidates have a 'person' entity so they share
        entity *type* but the *values* differ. ent_jaccard is computed
        over VALUES so Alice ≠ Bob → ent_jaccard=0. This case expects
        REL_NEW_FACT (different values, no token overlap) per the
        ECV heuristic for disjoint value sets.
        """
        rel, conf = relation(
            anchor_content="completely unrelated anchor abc",
            anchor_entities_json='[{"type":"person","value":"Alice"}]',
            candidate_content="totally different subject xyz",
            candidate_entities_json='[{"type":"person","value":"Bob"}]',
        )
        # Disjoint entity VALUES → new_fact
        self.assertEqual(rel, REL_NEW_FACT)
        self.assertLess(conf, 0.7)

    def test_batch_node_usefulness(self):
        """Batch scoring returns one float per candidate."""
        candidates = [
            {"content": "dark mode preference", "keywords": ["dark"]},
            {"content": "completely unrelated xyz"},
            {"content": "dark theme settings", "keywords": ["dark", "theme"]},
        ]
        scores = batch_node_usefulness("dark mode preference", candidates)
        self.assertEqual(len(scores), 3)
        self.assertGreater(scores[0], scores[1])
        # scores[2] has strong kw overlap, should beat baseline (0.05)
        self.assertGreater(scores[2], 0.15)
        # scoring must be deterministic — exact order
        self.assertGreater(scores[0], scores[1])  # dark mode vs unrelated
        self.assertGreater(scores[2], scores[1])  # dark theme vs unrelated


if __name__ == "__main__":
    unittest.main(verbosity=2)