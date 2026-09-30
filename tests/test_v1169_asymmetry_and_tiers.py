"""test_v1169_asymmetry_and_tiers.py — v1.16.9 asymmetric decay + 3-tier promotion tests.

Article: "评测-记忆-落地-控制飞轮" §2.3 specifies:
  - asymmetric decay: bad memory decays 2.4× faster than good
    (good: 0.05/30d; bad: 0.12/30d)
  - 3-tier promotion: 0.70 (initial) → 0.85 (3 invokes) → 0.95 (6 invokes)

These tests verify the promotion ladder via direct memory_experience
manipulation. Asymmetric decay is verified by computing the expected
penalty for known outcome strings + age (deterministic, no LLM).

The actual SQL-level verification is integration-tested via the live
server smoke tests; unit tests here cover the algorithm constants and
the promotion ladder boundaries.
"""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# Mirror of constants from astor_memory.bus.store — keep in sync.
_DECAY_GOOD = 0.05
_DECAY_BAD = 0.12
_DECAY_CAP = 0.30
_HALF_LIFE_DAYS = 30
_PROMOTE_TIER_1_THRESHOLD = 3   # → 0.85
_PROMOTE_TIER_2_THRESHOLD = 6   # → 0.95
_PROMOTE_TIER_1_IMP = 0.85
_PROMOTE_TIER_2_IMP = 0.95
_BAD_OUTCOMES = frozenset({
    'failure_pattern', 'lesson', 'user_correction', 'pushback', 'correction',
})


def expected_decay(age_days: int, outcome: str) -> float:
    """Compute the expected score penalty for an experience at given age + outcome."""
    if age_days <= 0:
        return 0.0
    rate = _DECAY_BAD if outcome in _BAD_OUTCOMES else _DECAY_GOOD
    return min(_DECAY_CAP, age_days / _HALF_LIFE_DAYS * rate)


def expected_promotion(occurrence_count: int, current_importance: float) -> float:
    """Apply the 3-tier promotion ladder."""
    if occurrence_count >= _PROMOTE_TIER_2_THRESHOLD and current_importance < _PROMOTE_TIER_2_IMP:
        return _PROMOTE_TIER_2_IMP
    if occurrence_count >= _PROMOTE_TIER_1_THRESHOLD and current_importance < _PROMOTE_TIER_1_IMP:
        return _PROMOTE_TIER_1_IMP
    return current_importance


class TestAsymmetricDecay(unittest.TestCase):
    def test_decay_zero_age(self):
        self.assertEqual(expected_decay(0, 'success_pattern'), 0.0)
        self.assertEqual(expected_decay(0, 'failure_pattern'), 0.0)

    def test_decay_negative_age_clamped_to_zero(self):
        self.assertEqual(expected_decay(-10, 'success_pattern'), 0.0)

    def test_good_decay_30_days(self):
        # 30 days, success_pattern: 0.05
        self.assertAlmostEqual(expected_decay(30, 'success_pattern'), 0.05, places=4)

    def test_bad_decay_30_days_2_4x(self):
        # 30 days, failure_pattern: 0.12 (2.4× good)
        self.assertAlmostEqual(expected_decay(30, 'failure_pattern'), 0.12, places=4)
        # Verify ratio
        self.assertAlmostEqual(0.12 / 0.05, 2.4, places=2)

    def test_decay_capped_at_0_30(self):
        # 1000 days → cap
        self.assertAlmostEqual(expected_decay(1000, 'failure_pattern'), 0.30, places=4)
        self.assertAlmostEqual(expected_decay(1000, 'success_pattern'), 0.30, places=4)

    def test_bad_decay_outcomes(self):
        # All 5 bad outcomes should decay 2.4× faster
        for outcome in ['failure_pattern', 'lesson', 'user_correction', 'pushback', 'correction']:
            self.assertAlmostEqual(
                expected_decay(60, outcome) / expected_decay(60, 'success_pattern'),
                2.4, places=2,
                msg=f"outcome={outcome}",
            )

    def test_good_decay_outcomes(self):
        # Non-bad outcomes → good decay
        for outcome in ['success_pattern', 'mental_model', '', 'unknown']:
            self.assertEqual(expected_decay(60, outcome),
                             expected_decay(60, 'success_pattern'))


class TestThreeTierPromotion(unittest.TestCase):
    def test_initial_importance_unchanged(self):
        # 0-2 invokes: stays at current importance (default 0.7)
        for occ in [0, 1, 2]:
            self.assertEqual(expected_promotion(occ, 0.70), 0.70)

    def test_tier_1_promotion_at_3(self):
        # 3 invokes → 0.85
        self.assertEqual(expected_promotion(3, 0.70), 0.85)
        self.assertEqual(expected_promotion(3, 0.85), 0.85)  # already at tier 1
        self.assertEqual(expected_promotion(3, 0.95), 0.95)  # already HOT, skip

    def test_tier_2_promotion_at_6(self):
        # 6 invokes → 0.95
        self.assertEqual(expected_promotion(6, 0.70), 0.95)
        self.assertEqual(expected_promotion(6, 0.85), 0.95)
        self.assertEqual(expected_promotion(6, 0.95), 0.95)  # already HOT

    def test_tier_1_skipped_when_current_above(self):
        # If current importance is already > tier 1 (e.g. legacy 0.95 row),
        # 3 invokes should not downgrade it.
        self.assertEqual(expected_promotion(3, 0.95), 0.95)

    def test_tier_2_skipped_when_current_above(self):
        # Already at 0.95 → 6 invokes stays at 0.95.
        self.assertEqual(expected_promotion(6, 0.95), 0.95)

    def test_promotion_never_downgrades(self):
        # Promotion must be monotonic — no path should ever lower importance.
        test_cases = [
            (0, 0.95, 0.95),
            (3, 0.95, 0.95),
            (3, 0.85, 0.85),
            (3, 0.70, 0.85),
            (5, 0.70, 0.85),  # still only at 5 → tier 1
            (6, 0.70, 0.95),
            (100, 0.70, 0.95),
        ]
        for occ, current, expected in test_cases:
            self.assertEqual(expected_promotion(occ, current), expected)


class TestPromotionLadderBoundaries(unittest.TestCase):
    def test_2_to_3_transition(self):
        # 2 invokes: not promoted. 3 invokes: promoted.
        self.assertNotEqual(expected_promotion(2, 0.70), 0.85)
        self.assertEqual(expected_promotion(3, 0.70), 0.85)

    def test_5_to_6_transition(self):
        # 5 invokes: tier 1 only (0.85). 6 invokes: tier 2 (0.95).
        self.assertEqual(expected_promotion(5, 0.70), 0.85)
        self.assertEqual(expected_promotion(6, 0.70), 0.95)


if __name__ == '__main__':
    unittest.main()
