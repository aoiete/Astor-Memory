"""Unit tests for astor_skill_extractor.py classifier.

Validates that each pattern correctly classifies representative samples
and avoids known false positives.

Run: python test_astor_skill_extractor.py
"""
import sys
import unittest
from pathlib import Path

# Make script importable
sys.path.insert(0, str(Path(__file__).parent))
from astor_skill_extractor import classify_one


class TestClassifier(unittest.TestCase):
    def test_r_class_canonical(self):
        kind, conf, _ = classify_one(
            "【R-class canonical 2026-09-04】Agent 引用任何 memory fact..."
        )
        self.assertEqual(kind, "rule")
        self.assertGreaterEqual(conf, 0.85)

    def test_r_class_locked(self):
        kind, conf, _ = classify_one(
            "R396 (locked 2026-09-05 01:30 MDT, COMPREHENSIVE FIX SHIP):"
        )
        self.assertEqual(kind, "rule")
        self.assertGreaterEqual(conf, 0.8)

    def test_user_pref_in_quotes(self):
        """User pref inside quotes should NOT match user_preference (it's a rule reference)."""
        kind, _, _ = classify_one(
            "陈述 '我之前做过 X / 用户偏好是 W' 等具体事实"
        )
        # Hits negative rule: 用户偏好是 <word>
        self.assertEqual(kind, "rule")

    def test_user_pref_declaration(self):
        """'用户偏好: xxx' is a rule declaration."""
        kind, _, _ = classify_one(
            "用户偏好: agent 引用前必须 grounding"
        )
        self.assertEqual(kind, "rule")

    def test_user_pref_positive(self):
        """Genuine user preference (not at very start, not in quotes)."""
        kind, _, _ = classify_one(
            "中文短句回复是用户偏好,不用英语"
        )
        self.assertEqual(kind, "user_preference")

    def test_decision_user_authorized(self):
        kind, _, _ = classify_one(
            "用户授权 agent 主动读 .env 文件"
        )
        self.assertEqual(kind, "decision")

    def test_failure_at_start(self):
        kind, _, _ = classify_one(
            "失败模式: silent drop 因为 broker cache stale"
        )
        self.assertEqual(kind, "failure_pattern")

    def test_failure_in_middle_references_lesson(self):
        """Fact with '误判 silent drop' as reference (not main topic) → lesson."""
        kind, _, _ = classify_one(
            "2026-09-09 session 终局 audit (verified)\n\n"
            "关键 R-class:\n- fact 10443 (tombstoned): 误判 silent drop"
        )
        self.assertEqual(kind, "lesson")

    def test_lesson_bracket(self):
        kind, _, _ = classify_one(
            "[LESSON] 2026-09-09: write-action recall gate"
        )
        self.assertEqual(kind, "lesson")

    def test_ship_log_final(self):
        kind, _, _ = classify_one(
            "[ship log v1.14.11 final] Memmy-style initial report"
        )
        self.assertEqual(kind, "lesson")

    def test_session_audit(self):
        kind, _, _ = classify_one(
            "session 终局 audit (verified): 5 orders active"
        )
        self.assertEqual(kind, "lesson")

    def test_generic_fact(self):
        """Default: no rule matched → kind=fact with low confidence."""
        kind, conf, _ = classify_one(
            "VOO 当前价格 $700, 50d MA $696, market cap $1.5T"
        )
        self.assertEqual(kind, "fact")
        self.assertLess(conf, 0.6)

    def test_user_pref_habit(self):
        """User habit pattern."""
        kind, _, _ = classify_one(
            "用户应该 ship 一个 commit 才算完成"
        )
        self.assertEqual(kind, "user_preference")


if __name__ == "__main__":
    unittest.main(verbosity=2)
