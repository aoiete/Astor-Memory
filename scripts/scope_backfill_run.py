#!/usr/bin/env python
"""scope_backfill_run.py — 把 scope 规则回填到存量 fact (v1.16.94)。

默认 dry-run: 只报告。--apply 才写。

用法:
    python scripts/scope_backfill_run.py
    python scripts/scope_backfill_run.py --apply
    python scripts/scope_backfill_run.py --tier private
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _bus_for(tier: str, user_id: str | None = None):
    from astor_memory.bus.store import astor_bus
    from astor_memory._internal.acl import astor_init_acl
    if tier == "private" and user_id:
        astor_init_acl(actor=f"user:{user_id}", role="user", tier="private",
                       user_id=user_id, subscription_plan="power")
    else:
        astor_init_acl(actor="admin:admin", role="admin", tier=tier)
    return astor_bus(tier=tier, user_id=user_id)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="all",
                    help="public / source / private / all")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--apply", action="store_true",
                    help="真的回填 (默认只报告)")
    ap.add_argument("--ttl-days", type=int, default=30)
    args = ap.parse_args()

    from astor_memory.nest.scope_backfill import backfill_tier

    targets = ([("public", None), ("source", None), ("private", args.user)]
               if args.tier == "all"
               else [(args.tier, args.user if args.tier == "private" else None)])

    total_tmp = total_perm = total_never = total_applied = 0
    for tier, uid in targets:
        try:
            bus = _bus_for(tier, uid)
        except Exception as e:
            print(f"[{tier}] 打不开 bus: {e}")
            continue
        rep = backfill_tier(bus, ttl_days=args.ttl_days, dry_run=not args.apply)
        if "error" in rep:
            print(f"[{tier}] 错误: {rep['error']}")
            continue
        label = f"{tier}/{uid}" if uid else tier
        print(f"=== {label} ===")
        print(f"   活 fact        {rep['n_alive']}")
        print(f"   已是临时层      {rep['already_temporary']}")
        print(f"   应升为临时层    {rep['to_temporary']}")
        print(f"   保持永久       {rep['stay_permanent']}")
        print(f"   其中从没被召回  {rep['never_recalled']}  <- 只有这些可能到期降级")
        if rep.get("applied"):
            print(f"   已写入         {rep['applied']} "
                  f"(带到期 {rep.get('applied_with_expiry', 0)}, "
                  f"仅暂定 {rep.get('applied_provisional_only', 0)})")
        for s in rep.get("sample", [])[:6]:
            print(f"      - #{s['id']} kind={s['kind']} imp={s['importance']} "
                  f"hits={s['access_count']} {s['content_preview'][:56]!r}")
        print()
        total_tmp += rep["to_temporary"]
        total_perm += rep["stay_permanent"]
        total_never += rep["never_recalled"]
        total_applied += rep.get("applied", 0)

    print(f"=== 合计: 临时层 {total_tmp}, 永久层 {total_perm}, "
          f"从未被召回 {total_never}, 实际写入 {total_applied} ===")
    if not args.apply and total_tmp:
        print("(加 --apply 才真的回填)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
