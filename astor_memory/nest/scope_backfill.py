"""Retroactive scope assignment (v1.16.94, 2026-10-23).

Why
---
scope_policy.py decides a fact's tier at write time. Every fact written
before that existed was promoted with scope_type='long_term' and no
expiry, so the whole corpus sat in one permanent bucket while the
mechanism only governed new writes. The evidence the rule needs is
still in the row -- kind, importance, provenance_kind, and the outcome
the extractor recorded -- so the same decision can be made again for
existing facts.

This is not a bulk demotion. A fact that has been recalled keeps being
recalled, and recall renews its expiry (scope_lifecycle.renew_on_confirm),
so the population that actually falls due is the one that has never
been used. Measured on the live corpus before any write: 2342 of the
2343 admin-tier facts that the rule would make temporary have been
recalled at least once, so the only ones at risk are the 1 in admin and
30 in public that have never surfaced.

The rule is applied to the stored evidence, not to a guess. outcome is
recovered from the fact's metadata when the extractor recorded it.

Usage:
    from astor_memory.nest.scope_backfill import backfill_tier
    backfill_tier(bus, dry_run=True)   # report only
    backfill_tier(bus, dry_run=False)  # write
"""
from __future__ import annotations

import datetime as _dt
import json

from .scope_policy import (
    LONG_TERM, SHORT_TERM, DEFAULT_TTL_DAYS, classify_scope, expiry_from_now,
)

# The columns the rule reads. Kept in one place so a schema change shows
# up as a single edit rather than a scattered set of query strings.
_EVIDENCE_SELECT = (
    "SELECT id, content, kind, importance, provenance_kind, metadata, "
    "       scope_type, expires_at, access_count, created_at "
    "FROM memory_canonical WHERE tombstoned = 0"
)


# 内容里出现这些标记, 说明作者自己把它写成了长期有效的规则/偏好,
# 而抽取器没能把它归到 user_preference / rule (抽取器的 kind 分类会漏)。
# 回填时保守处理: 命中就保持 long_term —— 宁可让噪声多留一会儿, 也不能
# 把用户明确说过的偏好降级掉。
#
# 漏判是实测的: "用户偏好:使用astor记忆系统存储数据" 这种内容,
# 抽取器给的是 kind='fact' + importance=0.5, 于是按 kind 判定会被算成
# "未验证的一次性观察"。
_DURABLE_MARKERS = (
    # 中文
    '用户偏好', '用户要求', '用户纠正', '用户锁定', '用户习惯', '用户风格',
    '用户工作', '铁律', '规则', '协议', '口径', '标准', '约定', '禁止',
    '必须', '不要', '永远', '不能', '锁定',
    # 英文 / 混排
    'RULE ', 'R-class', 'locked', 'MUST', 'NEVER', 'ALWAYS',
    'iron rule', 'policy', 'convention', 'standard', 'SSoT',
)


def _looks_durable(content: str) -> bool:
    """内容是否自带"这是长期规则"的标记。"""
    if not content:
        return False
    text = content[:400]
    return any(m in text for m in _DURABLE_MARKERS)


def _outcome_from_metadata(meta_raw) -> str | None:
    """The extractor recorded the outcome in the fact's metadata JSON."""
    if not meta_raw:
        return None
    try:
        meta = json.loads(meta_raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(meta, dict):
        return None
    out = meta.get('outcome')
    return out if isinstance(out, str) and out else None


def plan_backfill(bus, ttl_days: int = DEFAULT_TTL_DAYS) -> dict:
    """Decide what would change, without writing anything."""
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat().replace(
        "+00:00", "Z")
    try:
        rows = bus.conn.execute(_EVIDENCE_SELECT).fetchall()
    except Exception as e:
        return {'error': str(e)}

    to_temporary: list[dict] = []
    stay_permanent = 0
    already_temporary = 0
    never_recalled = 0

    for r in rows:
        (fid, content, kind, importance, prov, meta_raw,
         scope_type, expires_at, access_count, created_at) = r[:10]
        if scope_type == SHORT_TERM:
            already_temporary += 1
            continue
        outcome = _outcome_from_metadata(meta_raw)
        if _looks_durable(content):
            stay_permanent += 1
            continue
        dec = classify_scope(
            kind=kind,
            outcome=outcome,
            capture_intent=False,
            provenance_kind=prov,
            importance=importance,
            # v1.16.94: 确认次数就是验证证据 (see scope_policy).
            confirmations=access_count or 0,
            ttl_days=ttl_days,
        )
        if dec.scope_type != SHORT_TERM:
            stay_permanent += 1
            continue
        if (access_count or 0) <= 0:
            never_recalled += 1
        to_temporary.append({
            'id': fid,
            'kind': kind,
            'importance': importance,
            'provenance_kind': prov,
            'outcome': outcome,
            'access_count': access_count or 0,
            # 从没被召回过 -> 给到期时间, 到期真的会被降级。
            # 被召回过 -> 只标记为"暂定", 不设到期时间: 一次召回是弱
            # 验证, 但 30 天没用上就降级仍然太激进 (实测里有"用户对话
            # 混合使用中文陈述和英文技术术语"这种长期有效的偏好, 只是
            # 恰好被召回过 1 次)。它们靠被反复召回来晋升 (3 次即升
            # long_term), 这就是"多次验证后谨慎晋升"。
            'expiring': (access_count or 0) <= 0,
            'reason': dec.reason,
            'content_preview': (content or '')[:100],
        })

    return {
        'n_alive': len(rows),
        'to_temporary': len(to_temporary),
        'stay_permanent': stay_permanent,
        'already_temporary': already_temporary,
        'never_recraised': never_recalled,
        'never_recalled': never_recalled,
        # The full item list, not just a sample -- the apply step needs
        # every id and every expiring flag. ('to_temporary' is the count.)
        'items': to_temporary,
        'sample': to_temporary[:15],
        'now_iso': now_iso,
    }


def backfill_tier(bus, ttl_days: int = DEFAULT_TTL_DAYS,
                  dry_run: bool = True) -> dict:
    """Apply the scope rule to the existing corpus.

    dry_run=True (default) reports only.
    """
    plan = plan_backfill(bus, ttl_days)
    if 'error' in plan:
        return plan
    if dry_run or not plan['to_temporary']:
        plan['applied'] = 0
        return plan

    now_iso = plan['now_iso']
    expiry = expiry_from_now(ttl_days, now_iso)
    applied = 0
    try:
        # 两组分开写: 有到期时间的 (从未被召回) 和没有的 (只标记暂定)
        _items = plan.get('items') or []
        exp_ids = [t['id'] for t in _items if t.get('expiring')]
        soft_ids = [t['id'] for t in _items if not t.get('expiring')]
        if exp_ids:
            ph = ','.join('?' * len(exp_ids))
            cur = bus.conn.execute(
                f"UPDATE memory_canonical "
                f"SET scope_type = ?, expires_at = ? "
                f"WHERE id IN ({ph}) AND tombstoned = 0 AND scope_type != ?",
                (SHORT_TERM, expiry, *exp_ids, SHORT_TERM),
            )
            applied += cur.rowcount or 0
        if soft_ids:
            ph = ','.join('?' * len(soft_ids))
            cur = bus.conn.execute(
                f"UPDATE memory_canonical "
                f"SET scope_type = ?, expires_at = NULL "
                f"WHERE id IN ({ph}) AND tombstoned = 0 AND scope_type != ?",
                (SHORT_TERM, *soft_ids, SHORT_TERM),
            )
            applied += cur.rowcount or 0
        plan['applied'] = applied
        plan['applied_with_expiry'] = len(exp_ids)
        plan['applied_provisional_only'] = len(soft_ids)
    except Exception as e:
        plan['error'] = str(e)
        plan['applied'] = 0
    return plan
