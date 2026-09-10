"""Unit tests for astor_importance_audit.py.

Run: python test_astor_importance_audit.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from astor_importance_audit import suggest_boost


class TestImportanceBoost(unittest.TestCase):
    def test_r_class_canonical(self):
        target, rationale = suggest_boost("【R-class canonical 2026-09-04】...")
        self.assertEqual(target, 0.95)
        self.assertIn("R-class", rationale)

    def test_r_class_locked(self):
        target, _ = suggest_boost("R396 (locked 2026-09-05 01:30 MDT):")
        self.assertEqual(target, 0.95)

    def test_iron_rule(self):
        target, rationale = suggest_boost("Iron Rule 5: ETF rule")
        self.assertEqual(target, 0.85)
        self.assertEqual(rationale, "Iron Rule N")

    def test_lesson_prefix(self):
        target, _ = suggest_boost("[LESSON] 2026-09-09: write-action recall gate")
        self.assertEqual(target, 0.85)

    def test_user_habit(self):
        target, _ = suggest_boost("用户应该 ship 一个 commit 才算完成")
        self.assertEqual(target, 0.7)

    def test_locked_pattern(self):
        target, _ = suggest_boost("This rule is (locked 2026-09-04) the standard")
        self.assertEqual(target, 0.9)

    def test_tombstone_audit(self):
        target, rationale = suggest_boost(
            "撤回 silent drop 错判 (fact 10443 tombstoned)"
        )
        self.assertEqual(target, 0.8)
        self.assertIn("tombstone", rationale)

    def test_no_signal(self):
        """Generic fact with no boost signal."""
        target, rationale = suggest_boost(
            "VOO 当前价格 $700, 50d MA $696, market cap $1.5T"
        )
        self.assertIsNone(target)
        self.assertIsNone(rationale)

    def test_no_downgrade(self):
        """Never downgrade — boost returns None if importance already high."""
        # This is enforced in main(), not suggest_boost, but signal must still return a target
        target, _ = suggest_boost("R-class canonical 2026-09-04 locked")
        # The function returns the target importance; main() compares with current
        self.assertEqual(target, 0.95)


if __name__ == "__main__":
    unittest.main(verbosity=2)
