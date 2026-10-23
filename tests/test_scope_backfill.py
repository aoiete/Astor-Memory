"""test_scope_backfill.py — 回填存量 fact 的 scope 判定 (v1.16.94)。

回填用的是**同一条**规则, 只是多带了两个只有事后才知道的信号:

  - confirmations (被召回过多少次) —— "正确经验经过多次验证后谨慎晋升"
    的证据。写时判定拿不到它, 事后判定可以。
  - 内容里有没有"长期有效"的标记 —— 抽取器会把"用户偏好:xxx"这种
    判成 kind='fact' + importance=0.5, 于是按 kind 判定会把它算成
    "未验证的一次性观察"。实测 #25「用户偏好:使用astor记忆系统存储数据」
    就是这样被漏判的。

回填是**保守**的: 从没被召回过的才给到期时间 (真的会被降级), 被召回过
的只标记为"暂定" (不设到期, 但被反复召回 3 次就升 long_term)。
"""
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from astor_memory.nest.scope_backfill import (  # noqa: E402
    _looks_durable, _outcome_from_metadata, backfill_tier, plan_backfill,
)
from astor_memory.nest.scope_policy import LONG_TERM, SHORT_TERM  # noqa: E402


def _bus(rows):
    conn = sqlite3.connect(':memory:', check_same_thread=False)
    conn.executescript("""
        CREATE TABLE memory_canonical (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'fact',
            importance REAL NOT NULL DEFAULT 0.5,
            provenance_kind TEXT,
            metadata TEXT NOT NULL DEFAULT '{}',
            scope_type TEXT NOT NULL DEFAULT 'long_term',
            expires_at DATETIME,
            access_count INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME,
            tombstoned INTEGER NOT NULL DEFAULT 0
        );
    """)
    for r in rows:
        conn.execute(
            "INSERT INTO memory_canonical "
            "(content, kind, importance, provenance_kind, metadata, "
            " access_count) VALUES (?,?,?,?,?,?)", r)
    conn.commit()

    class _B:
        pass
    b = _B()
    b.conn = conn
    return b


def test_recurring_fact_stays_permanent():
    """被反复召回的 fact 是被验证过的, 不能算"未验证的一次性观察"。"""
    bus = _bus([('astor 是用户的记忆系统,助手应该主动调用 recall 和写入',
                 'fact', 0.525, 'extracted', '{}', 349)])
    plan = plan_backfill(bus)
    assert plan['to_temporary'] == 0, "被召回 349 次的规则不该进临时层"


def test_never_recalled_fact_becomes_temporary_with_expiry():
    bus = _bus([('after sleep 17', 'fact', 0.5, 'extracted', '{}', 0)])
    plan = plan_backfill(bus)
    assert plan['to_temporary'] == 1
    assert plan['items'][0]['expiring'] is True


def test_durable_marker_keeps_fact_permanent():
    """抽取器漏判 user_preference 的情况, 内容里的标记要兜住。"""
    bus = _bus([('用户偏好:使用astor记忆系统存储数据,而不是依赖filesystem',
                 'fact', 0.5, 'extracted', '{}', 1)])
    plan = plan_backfill(bus)
    assert plan['to_temporary'] == 0, "内容带'用户偏好'的应该保持永久"


def test_durable_marker_detection():
    for text in ('用户偏好:xxx', 'RULE [locked 2026-10-07]', '铁律: 不许编造',
                 '必须 GTC', '永远不要用 taskkill /IM'):
        assert _looks_durable(text), text
    for text in ('after sleep 17', 'final audit vip method', '今天天气不错'):
        assert not _looks_durable(text), text


def test_backfill_applies_two_groups():
    bus = _bus([
        ('never used observation', 'fact', 0.5, 'extracted', '{}', 0),
        ('used once, no marker', 'fact', 0.5, 'extracted', '{}', 1),
    ])
    rep = backfill_tier(bus, dry_run=False)
    assert rep['applied'] == 2
    assert rep['applied_with_expiry'] == 1
    assert rep['applied_provisional_only'] == 1
    rows = bus.conn.execute(
        "SELECT content, scope_type, expires_at FROM memory_canonical "
        "ORDER BY id").fetchall()
    by_content = {r[0]: r for r in rows}
    assert by_content['never used observation'][1] == SHORT_TERM
    assert by_content['never used observation'][2] is not None, \
        "从没被召回的必须带到期时间"
    assert by_content['used once, no marker'][1] == SHORT_TERM
    assert by_content['used once, no marker'][2] is None, \
        "被召回过的不设到期时间 —— 一次召回是弱验证, 30 天没用就降级太激进"


def test_dry_run_changes_nothing():
    bus = _bus([('x', 'fact', 0.5, 'extracted', '{}', 0)])
    rep = backfill_tier(bus, dry_run=True)
    assert rep['to_temporary'] == 1
    assert rep.get('applied', 0) == 0
    scopes = bus.conn.execute(
        "SELECT scope_type FROM memory_canonical").fetchall()
    assert scopes[0][0] == LONG_TERM


def test_outcome_from_metadata():
    assert _outcome_from_metadata(json.dumps({'outcome': 'failure'})) == 'failure'
    assert _outcome_from_metadata('{}') is None
    assert _outcome_from_metadata(None) is None
    assert _outcome_from_metadata('not json') is None


def test_verified_outcome_stays_permanent():
    """outcome=failure 的 fact 是失败教训, 属于最该长期保留的一类。"""
    bus = _bus([('OpenD 会话过期导致 RemoteClose, 根因是...',
                 'fact', 0.5, 'extracted', json.dumps({'outcome': 'failure'}), 0)])
    plan = plan_backfill(bus)
    assert plan['to_temporary'] == 0
