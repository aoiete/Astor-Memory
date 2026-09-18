"""Tests for v1.14.66 _astor_classify_intent data/method separation.

User feedback (round 5): data 剥离, mode/method 留下来.
Classifier should:
  - Keep method-intent text public even if it mentions personal/financial keywords in passing
  - Demote strong personal/financial content (with first-person pronoun or multiple signals)
  - Treat single weak daily/emotion signal as public (too common)
  - No-signal default → public (release notes, dates, neutral)
"""
from __future__ import annotations

import unittest

from astor_memory.server import _astor_classify_intent


def classify(text):
    """Return 'public' or 'private' for easy assertion."""
    result = _astor_classify_intent(text, tier='public', user='admin')
    return {None: 'public', 'private': 'private'}.get(result, str(result))


# -----------------------------------------------------------------------
# Pure data — should demote to private
# -----------------------------------------------------------------------
class TestPurePersonal(unittest.TestCase):
    def test_personal_emotion(self):
        self.assertEqual(classify("我今天好累"), "private")

    def test_personal_buy_stock(self):
        self.assertEqual(classify("我买了 NVDA 100 股"), "private")

    def test_personal_my_day(self):
        self.assertEqual(classify("my day was terrible"), "private")

    def test_personal_first_alone(self):
        self.assertEqual(classify("我今天"), "private")

    def test_personal_in_middle(self):
        self.assertEqual(classify("今天我"), "private")

    def test_personal_repeated(self):
        self.assertEqual(classify("我 我 我"), "private")

    def test_personal_after_other_char(self):
        self.assertEqual(classify("用户 我"), "private")

    def test_personal_with_kan_kan(self):
        self.assertEqual(classify("我看看"), "private")


# -----------------------------------------------------------------------
# Pure method — should stay public
# -----------------------------------------------------------------------
class TestPureMethod(unittest.TestCase):
    def test_workflow_keyword(self):
        self.assertEqual(classify("如何配置 astor peer network 编辑 config.yaml 加 peer 节点然后重启服务"), "public")

    def test_rule_about_personal_filter(self):
        # Discussing a rule about personal filter — not personal data itself
        self.assertEqual(classify("R-class workflow rule about personal filter 必须 demote to private tier astor"), "public")

    def test_happy_path_is_technical_term(self):
        self.assertEqual(classify("happy path: astor 用户 login → dashboard 显示 fact 列表 步骤 1) 输入 query 2) recall hits"), "public")

    def test_workflow_kw(self):
        self.assertEqual(classify("workflow: astor 用户偏好 login 后显示 fact"), "public")

    def test_pure_date(self):
        # Just a date — no personal content
        self.assertEqual(classify("今天是星期三"), "public")

    def test_release_notes(self):
        self.assertEqual(classify("astor v1.14.66 ship 完成"), "public")

    def test_release_notes_with_steps(self):
        self.assertEqual(classify("今天 astor ship 完成了 step 1) install 2) test"), "public")

    def test_new_rule_no_personal(self):
        self.assertEqual(classify("新规则"), "public")


# -----------------------------------------------------------------------
# Method + personal — mixed cases
# -----------------------------------------------------------------------
class TestMethodPlusPersonal(unittest.TestCase):
    def test_first_person_subject_with_method(self):
        # "我今天想 ship" — first-person current action, method framing
        # The user is talking about their own action, demote.
        self.assertEqual(classify("我今天想 ship R-class rule 关于 personal demote content to private"), "private")

    def test_method_plus_strong_personal_financial(self):
        # 我 + 持仓 + workflow = private (real personal data)
        self.assertEqual(classify("我今天加仓 NVDA 100 股 because RSI 超卖 follow workflow"), "private")

    def test_method_plus_emotion(self):
        self.assertEqual(classify("我今天好累 workflow"), "private")

    def test_method_plus_financial_only(self):
        self.assertEqual(classify("我今天加仓 because 涨了 workflow"), "private")

    def test_rclass_with_personal_subject(self):
        # Even with R-class prefix, first-person subject demotes
        self.assertEqual(classify("R-class: 我今天加仓 NVDA workflow"), "private")

    def test_rclass_about_first_person(self):
        self.assertEqual(classify("R-class rule about 我今天加仓 NVDA"), "private")

    def test_first_person_in_middle(self):
        self.assertEqual(classify("今天 astor 我 ship 完成"), "private")


if __name__ == '__main__':
    unittest.main()
