"""Scope lifecycle: renew, promote, demote (v1.16.92, 2026-10-22).

《Self-Evolving 四》: "错误经验一旦被证伪, 要快速降级或删除; 正确经验
则应该经过多次验证后谨慎晋升。"

scope_policy 决定一条 fact 写进来时落在哪一层。这个模块管的是**之后**
会发生什么:

  renew_on_confirm()    每次被召回确认 -> 续期 (expires_at 往后推)
  promote_recurring()   累计确认够多次 -> 升为 long_term
  demote_expired()      过期 -> 降级 (tombstone, 可逆)

为什么续期是核心
----------------
如果 TTL 到期就降级, 那"用户时区是 MDT"这种**说一次就再也不提**的
稳定事实会在 30 天后被降级 —— 那是错的。续期机制把这个风险消掉了:
只有**从来没被召回用过**的事实才会过期。实测三个真实库, 符合"没被召回
过"这个条件的事实只有 admin 1 条 / public 30 条 / source 0 条。

所以这条规则的实际效果不是"砍掉一半记忆", 而是"从永久桶里把从没用过
的那些挑出来, 给它们一个 30 天的观察期"。这正是文章说的"门槛最低的
临时经验层 + 多次验证后谨慎晋升"。

降级用 tombstone 而不是物理删除: 可逆, 留审计行, 可以找回。
"""
from __future__ import annotations

import datetime as _dt

from .scope_policy import LONG_TERM, SHORT_TERM, DEFAULT_TTL_DAYS

# 累计被召回确认多少次后, 临时层升为长期层
PROMOTE_AFTER_CONFIRMATIONS = 3


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def renew_on_confirm(bus, fact_ids, ttl_days: int = DEFAULT_TTL_DAYS) -> int:
    """被召回确认的临时层 fact 续期。

    只动 scope_type='short_term' 且有 expires_at 的行 —— 已经升为
    long_term 的不碰 (它们的 expires_at 是 NULL, 续期没有意义)。

    Returns: 续期条数。
    """
    ids = [int(f) for f in (fact_ids or []) if f is not None]
    if not ids:
        return 0
    now = _dt.datetime.now(_dt.timezone.utc)
    new_expiry = (now + _dt.timedelta(days=int(ttl_days))).isoformat().replace(
        "+00:00", "Z")
    ph = ",".join("?" * len(ids))
    try:
        cur = bus.conn.execute(
            f"UPDATE memory_canonical SET expires_at = ? "
            f"WHERE id IN ({ph}) AND tombstoned = 0 "
            f"  AND scope_type = ? AND expires_at IS NOT NULL",
            (new_expiry, *ids, SHORT_TERM),
        )
        return cur.rowcount or 0
    except Exception:
        return 0


def promote_recurring(bus, min_confirmations: int = PROMOTE_AFTER_CONFIRMATIONS) -> int:
    """确认次数够了的临时层 fact 升为 long_term。

    "经过多次验证后谨慎晋升" —— access_count 就是验证次数的度量: 每次
    被召回命中就 +1。

    Returns: 晋升条数。
    """
    try:
        cur = bus.conn.execute(
            "UPDATE memory_canonical "
            "SET scope_type = ?, expires_at = NULL "
            "WHERE tombstoned = 0 AND scope_type = ? "
            "  AND access_count >= ?",
            (LONG_TERM, SHORT_TERM, int(min_confirmations)),
        )
        return cur.rowcount or 0
    except Exception:
        return 0


def demote_expired(bus, now_iso: str | None = None, dry_run: bool = True) -> dict:
    """过期且从未被确认过的临时层 fact -> 降级 (tombstone)。

    已经被召回确认过的 fact 会被 renew_on_confirm 续期, 所以能落到这里
    的基本是"写进来之后从来没被用到过"的那些。

    默认 dry_run=True, 只报告不动作 —— 降级是状态变更, 应该先看数。

    Returns: {'candidates': int, 'tombstoned': int, 'sample': [...]}
    """
    now = now_iso or _now_iso()
    try:
        rows = bus.conn.execute(
            "SELECT id, content, kind, access_count, expires_at "
            "FROM memory_canonical "
            "WHERE tombstoned = 0 AND scope_type = ? "
            "  AND expires_at IS NOT NULL AND expires_at <= ? "
            "ORDER BY expires_at",
            (SHORT_TERM, now),
        ).fetchall()
    except Exception as e:
        return {'candidates': 0, 'tombstoned': 0, 'sample': [], 'error': str(e)}

    sample = [
        {'id': r[0], 'content': (r[1] or '')[:120], 'kind': r[2],
         'access_count': r[3], 'expires_at': r[4]}
        for r in rows[:20]
    ]
    if dry_run or not rows:
        return {'candidates': len(rows), 'tombstoned': 0, 'sample': sample}

    ids = [int(r[0]) for r in rows]
    ph = ",".join("?" * len(ids))
    try:
        cur = bus.conn.execute(
            f"UPDATE memory_canonical "
            f"SET tombstoned = 1, tombstoned_at = ?, "
            f"    invalidated_reason = ? "
            f"WHERE id IN ({ph}) AND tombstoned = 0",
            (now, "scope expired (unverified working set)", *ids),
        )
        return {'candidates': len(rows), 'tombstoned': cur.rowcount or 0,
                'sample': sample}
    except Exception as e:
        return {'candidates': len(rows), 'tombstoned': 0, 'sample': sample,
                'error': str(e)}


def scope_stats(bus) -> dict:
    """当前 scope 分布 + 临期情况。给体检和 CLI 用。"""
    out = {'by_scope': [], 'expiring_soon': 0, 'expired_now': 0}
    try:
        out['by_scope'] = [
            {'scope_type': r[0], 'count': r[1]}
            for r in bus.conn.execute(
                "SELECT scope_type, COUNT(*) FROM memory_canonical "
                "WHERE tombstoned = 0 GROUP BY scope_type ORDER BY 2 DESC"
            ).fetchall()
        ]
        now = _dt.datetime.now(_dt.timezone.utc)
        soon = (now + _dt.timedelta(days=7)).isoformat().replace("+00:00", "Z")
        out['expiring_soon'] = bus.conn.execute(
            "SELECT COUNT(*) FROM memory_canonical "
            "WHERE tombstoned = 0 AND scope_type = ? "
            "  AND expires_at IS NOT NULL AND expires_at <= ?",
            (SHORT_TERM, soon),
        ).fetchone()[0]
        out['expired_now'] = bus.conn.execute(
            "SELECT COUNT(*) FROM memory_canonical "
            "WHERE tombstoned = 0 AND scope_type = ? "
            "  AND expires_at IS NOT NULL AND expires_at <= ?",
            (SHORT_TERM, _now_iso()),
        ).fetchone()[0]
    except Exception as e:
        out['error'] = str(e)
    return out
