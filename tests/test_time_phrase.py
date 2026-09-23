"""Unit tests for nest.time_phrase (v1.15.x time-scoped recall).

Fixed `now` = Tuesday 2026-09-22 10:00 so every expectation is deterministic.
"""
from datetime import datetime

from astor_memory.nest.time_phrase import parse_time_range

NOW = datetime(2026, 9, 22, 10, 0, 0)  # Tuesday


def test_no_phrase_returns_none():
    assert parse_time_range('what is astor memory', now=NOW) is None
    assert parse_time_range('仓位分析一下', now=NOW) is None
    assert parse_time_range('', now=NOW) is None
    assert parse_time_range(None, now=NOW) is None


def test_iso_date_anywhere():
    assert parse_time_range('2026-09-15 那天说了啥', now=NOW) == (
        '2026-09-15', '2026-09-15', '2026-09-15')


def test_chinese_days():
    assert parse_time_range('今天 market 怎么样', now=NOW) == ('2026-09-22', '2026-09-22', '今天')
    assert parse_time_range('昨天聊了啥', now=NOW) == ('2026-09-21', '2026-09-21', '昨天')
    # 昨天晚上 must resolve to yesterday, not get swallowed by 昨天
    assert parse_time_range('昨天晚上那个 bug', now=NOW) == ('2026-09-21', '2026-09-21', '昨晚')
    assert parse_time_range('昨晚的 poker', now=NOW) == ('2026-09-21', '2026-09-21', '昨晚')
    assert parse_time_range('前天 ship 了什么', now=NOW) == ('2026-09-20', '2026-09-20', '前天')


def test_chinese_weeks():
    # 本周 = Mon 9/21 .. today 9/22
    assert parse_time_range('本周的进展', now=NOW) == ('2026-09-21', '2026-09-22', '本周')
    # 上周 = Mon 9/14 .. Sun 9/20
    assert parse_time_range('上周复盘', now=NOW) == ('2026-09-14', '2026-09-20', '上周')
    # 上周二 = 9/15
    assert parse_time_range('上周二决定的', now=NOW) == ('2026-09-15', '2026-09-15', '上周二')
    # bare 周二 on a Tuesday = today
    assert parse_time_range('周二下午 poker', now=NOW) == ('2026-09-22', '2026-09-22', '周二')
    # bare 周三 on a Tuesday = most recent Wednesday = last week 9/16
    assert parse_time_range('周三的会议', now=NOW) == ('2026-09-16', '2026-09-16', '周三')
    # 下周一 = 9/28 (future is valid — planned events)
    assert parse_time_range('下周一的 plan', now=NOW) == ('2026-09-28', '2026-09-28', '下周一')


def test_chinese_months_and_relative():
    assert parse_time_range('上个月的总结', now=NOW) == ('2026-08-01', '2026-08-31', '上月')
    assert parse_time_range('本月到现在', now=NOW) == ('2026-09-01', '2026-09-22', '本月')
    assert parse_time_range('3天前的事', now=NOW) == ('2026-09-19', '2026-09-19', '3天前')
    assert parse_time_range('最近7天的 recall', now=NOW) == ('2026-09-15', '2026-09-22', '最近7天')
    assert parse_time_range('两周前的决定', now=NOW) == ('2026-09-07', '2026-09-13', '两周前')


def test_english():
    assert parse_time_range('yesterday we shipped X', now=NOW) == ('2026-09-21', '2026-09-21', 'yesterday')
    assert parse_time_range('last week poker sessions', now=NOW) == ('2026-09-14', '2026-09-20', 'last week')
    assert parse_time_range('last tuesday deploy', now=NOW) == ('2026-09-15', '2026-09-15', 'last tuesday')
    assert parse_time_range('3 days ago we fixed', now=NOW) == ('2026-09-19', '2026-09-19', '3 days ago')
    assert parse_time_range('past 7 days recalls', now=NOW) == ('2026-09-15', '2026-09-22', 'past 7 days')
    assert parse_time_range('this month stats', now=NOW) == ('2026-09-01', '2026-09-22', 'this month')
