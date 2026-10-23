"""test_scope_policy.py — v1.16.92 证据分级 scope 机制的测试。

覆盖:
  - classify_scope 的证据阶梯 (7 条规则各一个用例 + 边界)
  - scope_weight: long_term 权重高于 short_term
  - expiry_from_now: TTL 换算
  - scope_lifecycle: renew / promote / demote
  - 读路径: 过期事实被排除

背景: 《Self-Evolving 四》要求"错误经验一旦被证伪, 要快速降级或删除;
正确经验则应该经过多次验证后谨慎晋升"。astor 之前只有"永久"一种状态。
"""
import datetime as _dt
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from astor_memory.nest.scope_policy import (  # noqa: E402
    LONG_TERM, SHORT_TERM, DEFAULT_TTL_DAYS, VALIDATED_IMPORTANCE_FLOOR,
    classify_scope, expiry_from_now, scope_weight,
)


# --------------------------------------------------------------- 证据阶梯
def test_explicit_is_long_term():
    d = classify_scope(explicit=True)
    assert d.scope_type == LONG_TERM
    assert d.ttl_days is None


@pytest.mark.parametrize("pk", ["manual", "user_distilled", "rule", "operator"])
def test_explicit_provenance_is_long_term(pk):
    d = classify_scope(provenance_kind=pk, kind="fact", importance=0.3)
    assert d.scope_type == LONG_TERM, f"{pk} 应该长期保留"


@pytest.mark.parametrize("kind", [
    "lesson", "failure_pattern", "success_pattern", "rule",
    "user_preference", "decision", "mental_model", "knowledge",
    "knowledge_page", "skill", "reflection", "correction",
])
def test_distilled_kinds_are_long_term(kind):
    d = classify_scope(kind=kind, importance=0.3)
    assert d.scope_type == LONG_TERM, f"kind={kind} 是提炼过的类别, 应长期保留"


@pytest.mark.parametrize("oc", ["failure", "lesson", "success"])
def test_verified_outcomes_are_long_term(oc):
    d = classify_scope(kind="fact", outcome=oc, importance=0.3)
    assert d.scope_type == LONG_TERM


def test_capture_intent_is_long_term():
    d = classify_scope(kind="fact", capture_intent=True, importance=0.3)
    assert d.scope_type == LONG_TERM


def test_validated_importance_is_long_term():
    d = classify_scope(kind="fact", importance=VALIDATED_IMPORTANCE_FLOOR)
    assert d.scope_type == LONG_TERM
    # 边界: 刚好低一点就该是临时层 (neutral 的上限是 0.6)
    d2 = classify_scope(kind="fact", importance=0.69)
    assert d2.scope_type == SHORT_TERM


def test_unverified_observation_is_short_term():
    d = classify_scope(kind="fact", importance=0.5)
    assert d.scope_type == SHORT_TERM
    assert d.ttl_days == DEFAULT_TTL_DAYS
    assert d.is_temporary


def test_short_term_reason_is_auditable():
    d = classify_scope(kind="fact", importance=0.5)
    assert "unverified" in d.reason
    d2 = classify_scope(kind="lesson", importance=0.5)
    assert "lesson" in d2.reason


# ------------------------------------------------------------------ 权重
def test_scope_weight_prefers_long_term():
    assert scope_weight(LONG_TERM) == 1.0
    assert scope_weight(SHORT_TERM) < 1.0
    assert scope_weight(None) == 1.0, "缺省应按 long_term 处理"


def test_expiry_from_now():
    now = "2026-10-22T00:00:00+00:00"
    exp = expiry_from_now(30, now)
    assert exp is not None and exp.startswith("2026-11-21")
    assert expiry_from_now(None, now) is None
    assert expiry_from_now(0, now) is None


# ------------------------------------------------------------- 生命周期
def _bus():
    """最小 bus stub —— 只带 scope_lifecycle 用到的 conn。"""
    conn = sqlite3.connect(':memory:', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE memory_canonical (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'fact',
            scope_type TEXT NOT NULL DEFAULT 'long_term',
            expires_at DATETIME,
            access_count INTEGER NOT NULL DEFAULT 0,
            tombstoned INTEGER NOT NULL DEFAULT 0,
            tombstoned_at DATETIME,
            invalidated_reason TEXT,
            last_confirmed_at DATETIME,
            importance REAL NOT NULL DEFAULT 0.5
        );
    """)

    class _B:
        pass
    b = _B()
    b.conn = conn
    return b


def _insert(bus, content, scope_type, expires_at=None, access_count=0):
    cur = bus.conn.execute(
        "INSERT INTO memory_canonical "
        "(content, scope_type, expires_at, access_count) VALUES (?,?,?,?)",
        (content, scope_type, expires_at, access_count))
    return cur.lastrowid


def test_renew_on_confirm_extends_expiry():
    from astor_memory.nest.scope_lifecycle import renew_on_confirm
    bus = _bus()
    past = "2020-01-01T00:00:00Z"
    fid = _insert(bus, "x", SHORT_TERM, past)
    n = renew_on_confirm(bus, [fid])
    assert n == 1
    exp = bus.conn.execute(
        "SELECT expires_at FROM memory_canonical WHERE id=?", (fid,)).fetchone()[0]
    assert exp > "2026-" and exp is not past


def test_renew_does_not_touch_long_term():
    from astor_memory.nest.scope_lifecycle import renew_on_confirm
    bus = _bus()
    fid = _insert(bus, "x", LONG_TERM, None)
    assert renew_on_confirm(bus, [fid]) == 0
    exp = bus.conn.execute(
        "SELECT expires_at FROM memory_canonical WHERE id=?", (fid,)).fetchone()[0]
    assert exp is None, "long_term 的 expires_at 必须保持 NULL"


def test_promote_recurring():
    from astor_memory.nest.scope_lifecycle import promote_recurring
    bus = _bus()
    hot = _insert(bus, "hot", SHORT_TERM, "2030-01-01T00:00:00Z", access_count=5)
    cold = _insert(bus, "cold", SHORT_TERM, "2030-01-01T00:00:00Z", access_count=1)
    n = promote_recurring(bus, min_confirmations=3)
    assert n == 1
    row = bus.conn.execute(
        "SELECT scope_type, expires_at FROM memory_canonical WHERE id=?",
        (hot,)).fetchone()
    assert row[0] == LONG_TERM and row[1] is None
    cold_row = bus.conn.execute(
        "SELECT scope_type FROM memory_canonical WHERE id=?", (cold,)).fetchone()
    assert cold_row[0] == SHORT_TERM, "确认次数不够的不该晋升"


def test_demote_expired_dry_run_does_not_change_anything():
    from astor_memory.nest.scope_lifecycle import demote_expired
    bus = _bus()
    _insert(bus, "old", SHORT_TERM, "2020-01-01T00:00:00Z")
    rep = demote_expired(bus, dry_run=True)
    assert rep["candidates"] == 1
    assert rep["tombstoned"] == 0
    alive = bus.conn.execute(
        "SELECT COUNT(*) FROM memory_canonical WHERE tombstoned=0").fetchone()[0]
    assert alive == 1, "dry-run 不能改数据"


def test_demote_expired_apply_tombstones():
    from astor_memory.nest.scope_lifecycle import demote_expired
    bus = _bus()
    _insert(bus, "old", SHORT_TERM, "2020-01-01T00:00:00Z")
    rep = demote_expired(bus, dry_run=False)
    assert rep["tombstoned"] == 1
    alive = bus.conn.execute(
        "SELECT COUNT(*) FROM memory_canonical WHERE tombstoned=0").fetchone()[0]
    assert alive == 0


def test_demote_leaves_long_term_alone():
    from astor_memory.nest.scope_lifecycle import demote_expired
    bus = _bus()
    _insert(bus, "perm", LONG_TERM, None)
    rep = demote_expired(bus, dry_run=True)
    assert rep["candidates"] == 0


def test_demote_ignores_not_yet_expired():
    from astor_memory.nest.scope_lifecycle import demote_expired
    bus = _bus()
    _insert(bus, "fresh", SHORT_TERM, "2099-01-01T00:00:00Z")
    assert demote_expired(bus, dry_run=True)["candidates"] == 0


# --------------------------------------------------------------- 读路径
def test_read_path_excludes_expired():
    """读路径的 SQL 必须带 expires_at 过滤 —— 降级是批量任务, 读路径
    不能等它跑。"""
    import re
    src = open(
        os.path.join(os.path.dirname(__file__), '..', 'astor_memory', 'server.py'),
        encoding='utf-8').read()
    assert "expires_at IS NULL OR expires_at > strftime" in src, \
        "server 读路径必须排除已过期的临时层事实"


# ------------------------------------------------- outcome 检测器的词边界
def test_success_pattern_does_not_match_unverified():
    """回归: v1.16.92 之前 'verified' 没有词边界, 于是 "unverified"
    (以及 "unworking" / "book了") 被判成 success。

    后果不是小事: outcome 决定 fact 的 kind (success_pattern) 和 tier
    (public), 也决定 scope 分级 —— 一句"这条还没验证"会被当成"已验证的
    做法", 既进共享层又拿 long_term。
    """
    from astor_memory.forge.extractor import astor_classify_outcome
    assert astor_classify_outcome("scope_probe_unverified_9f3a") == 'neutral'
    assert astor_classify_outcome("unworking build") == 'neutral'
    assert astor_classify_outcome("这条还没验证") == 'neutral'


def test_success_pattern_still_matches_real_successes():
    from astor_memory.forge.extractor import astor_classify_outcome
    for t in ("搞定了", "verified", "跑通了", "ship 了", "this works"):
        assert astor_classify_outcome(t) == 'success', t


def test_caller_importance_is_not_dropped():
    """回归: v1.16.92 之前调用方显式给的 importance 被丢在地板上,
    astor_extract_facts 没有 importance 参数, regex 模式写死 0.5。

    后果: client.write() 和 mcp_auto_observe 发的 importance 全部无效,
    而 mcp_auto_observe 发的正是 _score_importance() 算出来的真分数 ——
    抽取器的启发式分从来没生效过。
    """
    src = open(
        os.path.join(os.path.dirname(__file__), '..', 'astor_memory', 'server.py'),
        encoding='utf-8').read()
    assert "_explicit_importance" in src, "server 必须接受调用方的 importance"
    assert "_f.importance = _ei" in src, "调用方的 importance 必须落到 fact 上"
