"""scope_lifecycle 维护工具 (v1.16.92)。

《Self-Evolving 四》要求的三件事在这里落地:
  - 多次验证后谨慎晋升   promote_recurring
  - 过期快速降级         demote_expired
  - 临时层是工作集       renew_on_confirm (在召回确认路径上, 不在这里)

默认 dry-run: 只报告不动作。--apply 才真的降级。
晋升是自动的 (access_count >= 3), 因为它只是把"已经反复被验证"这件事
记到 scope_type 上, 不丢数据。

用法:
    python scope_lifecycle_run.py                # 三个 tier 全部报告
    python scope_lifecycle_run.py --tier public
    python scope_lifecycle_run.py --apply        # 真降级
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _init_acl(tier: str, user_id: str | None = None):
    """绑定 ACL —— AstorBus 打开前必须先有 actor 绑定, 否则报
    "astor_acl not initialized" (见 cli/main.py:1079 的 am init)。"""
    from astor_memory._internal.acl import astor_init_acl
    if tier == "private" and user_id:
        astor_init_acl(actor=f"user:{user_id}", role="user", tier="private",
                       user_id=user_id, subscription_plan="power")
    else:
        astor_init_acl(actor="admin:admin", role="admin", tier=tier)


def _bus_for(tier: str, user_id: str | None = None):
    from astor_memory.bus.store import astor_bus
    _init_acl(tier, user_id)
    return astor_bus(tier=tier, user_id=user_id)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="all",
                    help="public / source / private / all")
    ap.add_argument("--user", default="admin",
                    help="private 层用的 user_id")
    ap.add_argument("--apply", action="store_true",
                    help="真的降级 (默认只报告)")
    ap.add_argument("--min-confirmations", type=int, default=3)
    args = ap.parse_args()

    from astor_memory.nest.scope_lifecycle import (
        demote_expired, promote_recurring, scope_stats,
    )

    targets = []
    if args.tier == "all":
        targets = [("public", None), ("source", None),
                   ("private", args.user)]
    else:
        targets = [(args.tier, args.user if args.tier == "private" else None)]

    total_demoted = 0
    total_promoted = 0
    for tier, uid in targets:
        try:
            bus = _bus_for(tier, uid)
        except Exception as e:
            print(f"[{tier}] 无法打开 bus: {e}")
            continue
        st = scope_stats(bus)
        print(f"=== {tier}{'/' + uid if uid else ''} ===")
        for row in st.get("by_scope", []):
            print(f"   scope {row['scope_type']:12s} {row['count']:6d}")
        print(f"   7 天内临期: {st.get('expiring_soon', 0)}")
        print(f"   已到期    : {st.get('expired_now', 0)}")

        promoted = promote_recurring(bus, args.min_confirmations)
        total_promoted += promoted
        print(f"   晋升为 long_term: {promoted}")

        rep = demote_expired(bus, dry_run=not args.apply)
        total_demoted += rep.get("tombstoned", 0)
        print(f"   到期待降级: {rep.get('candidates', 0)}"
              f"  (实际降级 {rep.get('tombstoned', 0)})")
        for s in rep.get("sample", [])[:5]:
            print(f"      - #{s['id']} kind={s['kind']} "
                  f"hits={s['access_count']} {s['content'][:60]!r}")
        if not args.apply and rep.get("candidates"):
            print("   (加 --apply 才真的降级)")
        print()

    print(f"=== 合计: 晋升 {total_promoted}, 降级 {total_demoted} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
