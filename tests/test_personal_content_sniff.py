"""test_personal_content_sniff.py — Layer 2 personal content sniff tests.

Verifies that detect_personal_content correctly categorizes non-PII personal
content patterns that the 45-pattern PII scanner misses. Layer 2 is warn-only
(never blocks writes); tests cover categorization accuracy on representative
samples covering each of the 4 categories: name_chinese / location /
relationship / financial.

v1.17.91: this file was a script (main() + sys.exit()) so pytest collected
nothing from it -- the assertions below had never run. It is pytest-style
now, and it pins the name_chinese narrowing: the old pattern matched ANY
2-4 Chinese chars followed by a verb, so ordinary technical prose ("丢失
发生在", "尚未定位到") read as a person's name and pushed shared-tier
writes into the private tier.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory.nest.memory_defense import detect_personal_content


def test_name_chinese_detected():
    assert "name_chinese" in detect_personal_content("今天张三家发生 X")


def test_location_detected():
    assert "location" in detect_personal_content("我在杭州")


def test_relationship_detected():
    assert "relationship" in detect_personal_content("我女朋友叫 X")


def test_financial_detected():
    assert "financial" in detect_personal_content("账户余额 50 万")


def test_clean_behavior_text():
    assert detect_personal_content("操作 moomoo 流程：1. 打开 2. 登录 3. 下单") == []


def test_empty_string():
    assert detect_personal_content("") == []


def test_multiple_categories_at_once():
    res = detect_personal_content("我女朋友叫小李，她在杭州工作，账户余额 50 万")
    assert "relationship" in res
    assert "location" in res
    assert "financial" in res


# v1.17.91: technical prose must not read as a person. Each of these matched
# the old "2-4 Chinese chars + verb" shape and cost a shared-tier write its
# shared tier.
def test_technical_prose_is_not_a_chinese_name():
    for text in (
        "丢失发生在 lexical stage 之后",
        "尚未定位到具体函数",
        "merged 截断到 top_k",
        "该次清理已用 quick_check 回滚",
        "数据还留在数据库里",
        "这条走的是向量召回路径",
    ):
        assert "name_chinese" not in detect_personal_content(text), text


def test_real_chinese_names_are_still_detected():
    for text in ("张三去了北京", "李四说她到了公司", "王五和赵六一起去", "陈老板说他来了"):
        assert "name_chinese" in detect_personal_content(text), text
