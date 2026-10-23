"""test_memory_hygiene.py — 清理工具的回归测试。

最重要的一条: **同源不同章节不能被当成近似重复**。

2026-10-23 实测: 原来的近似重复判定只比前 40 个字符, 而知识分页的 fact
都长这样:

    [source: 12-why-not-prediction.md]
    [section: 證據三：卦德 > 五行（解釋力約 5 倍）]
    [tag: 命理反思]
    [topic: 命理边界]

    <真正的正文>

同一个源文件的每个章节共享这段头部, 前 40 字完全一样, 但正文讲的是不同
的东西。工具因此把 4 个章节当成一组"重复", 只留最早的 _preamble, 删掉了
另外三个 —— 其中一个是"卦德 > 五行, 解释力约 5 倍"。实测误删 49 条
(public 29 + private 20)。

修法: 形状键 = 剥掉结构性头部 + 掩码数字, 再比较。这样"只差时间戳的
同一模板"照样落进同一组, 而"同源不同章节"不会。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.dirname(__file__))

from memory_hygiene import _shape_key  # noqa: E402


def _section(source: str, section: str, body: str) -> str:
    return (f"[source: {source}]\n"
            f"[section: {section}]\n"
            f"[tag: 命理反思]\n"
            f"[topic: 命理边界]\n"
            f"\n{body}")


def test_same_source_different_sections_are_not_duplicates():
    a = _section("12-why-not-prediction.md", "_preamble", "# 為什麼不是預測")
    b = _section("12-why-not-prediction.md", "證據二：姊妹專案 iching", "遮蔽判詞實驗")
    c = _section("12-why-not-prediction.md", "所以該怎麼用", "| 不要這樣 | 要這樣 |")
    assert _shape_key(a) != _shape_key(b)
    assert _shape_key(a) != _shape_key(c)
    assert _shape_key(b) != _shape_key(c)


def test_same_template_differing_only_in_numbers_is_a_duplicate():
    """真垃圾: 同一个模板只差一个时间戳/计数。"""
    a = ('maker_pocket V085_TP_CAP: {"entry_price": 84461.9, '
         '"ts": "2026-10-21T10:17:14.163835", "event": "V085_TP_CAP"}')
    b = ('maker_pocket V085_TP_CAP: {"entry_price": 80000.0, '
         '"ts": "2026-10-22T03:04:05.123456", "event": "V085_TP_CAP"}')
    assert _shape_key(a) == _shape_key(b)


def test_ship_test_counter_facts_are_duplicates():
    a = "Ship J bypass rate-limit test fact #1"
    b = "Ship J bypass rate-limit test fact #17"
    assert _shape_key(a) == _shape_key(b)


def test_daily_summaries_with_different_dates_group_together():
    """daily 指标日志: 同模板, 只差日期和计数。"""
    a = "[Doc timestamp: 2026-09-09] Astor daily summary: peak-check signal counts = EARLY=0, EXIT=0"
    b = "[Doc timestamp: 2026-10-07] Astor daily summary: peak-check signal counts = EARLY=1, EXIT=0"
    assert _shape_key(a) == _shape_key(b)


def test_different_bodies_stay_different():
    a = _section("a.md", "s1", "卦德（健順動入陷麗止說）效應範圍廣")
    b = _section("a.md", "s1", "完全不同的另一段正文，讲的是别的事情")
    assert _shape_key(a) != _shape_key(b)
