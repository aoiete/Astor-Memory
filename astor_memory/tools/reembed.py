"""reembed.py — re-embed all facts under the new default model.

v1.14.5 (2026-09-08, ship).

Use after changing `astor_get_model_name_for_ram()` default — walks every
canonical fact in every tier (public/source/private) and calls
`nest.store(fact_id, content)` so each fact gets an embedding row under the
new model_name. Old embeddings are kept (model_name column) for audit /
fallback, but new recall() calls filter to the active model_name.

Usage:
    python -m astor_memory.tools.reembed                  # all tiers, all users
    python -m astor_memory.tools.reembed --tier private   # just one tier
    python -m astor_memory.tools.reembed --user admin     # just one user
    python -m astor_memory.tools.reembed --model 'intfloat/multilingual-e5-large'
    python -m astor_memory.tools.reembed --dry-run       # count only, no writes
    python -m astor_memory.tools.reembed --batch-size 50 # flush every N facts

Runtime: ~10-30 min for 9500 facts on CPU (multilingual-e5-large at ~50 facts/sec).
Progress log: stdout every batch + final summary to astor/metrics/reembed_<ts>.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(r"D:\AI\astor-memory")
METRICS = ROOT / "astor" / "metrics"
METRICS.mkdir(parents=True, exist_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=["public", "source", "private", "repo"], default=None,
                    help="Limit to one tier (default: all)")
    ap.add_argument("--user", default=None, help="Limit to one user_id (private/repo only)")
    ap.add_argument("--model", default=None,
                    help="Override embedding model name (default: astor_get_model_name_for_ram)")
    ap.add_argument("--dry-run", action="store_true", help="Count only, do not write")
    ap.add_argument("--batch-size", type=int, default=100, help="Flush every N facts (logging)")
    ap.add_argument("--limit", type=int, default=None, help="Process at most N facts (smoke test)")
    args = ap.parse_args()

    # Lazy imports — model load is heavy (~20s)
    from ..nest.embeddings import astor_get_model_name_for_ram, astor_get_embedding_model
    from ..bus.store import astor_bus
    from ..nest.vector_store import astor_nest

    model_name = args.model or astor_get_model_name_for_ram()
    print(f"[reembed] target model: {model_name}")
    if not args.dry_run:
        print(f"[reembed] preloading model (may take 10-20s on first run)...")
        t0 = time.time()
        astor_get_embedding_model(model_name)  # warmup
        print(f"[reembed] model loaded in {time.time()-t0:.1f}s")

    # Decide which (tier, user_id) combinations to walk
    from .._internal.acl import astor_init_acl
    astor_init_acl(actor='admin:admin', role='admin', tier='public', subscription_plan=None)
    tiers = [args.tier] if args.tier else ["public", "source", "private"]
    if args.tier in ("private", "repo") and args.user:
        tier_user_combos = [(args.tier, args.user)]
    elif args.user and not args.tier:
        tier_user_combos = [(t, args.user) for t in ("private", "repo")]
    elif args.tier == "private":
        # v1.14.5: walk every user's private DB (16+ users in prod)
        from pathlib import Path
        users_dir = Path(r"D:\AI\Astor-Memory-Runtime\users")
        tier_user_combos = [("private", d.name) for d in users_dir.iterdir() if d.is_dir()]
    elif args.tier is None:
        # default = all public + all source + all private (per-user)
        # v1.14.5: prioritize admin (largest tier, 9500+ facts) first so
        # admin recall path gets e5-large coverage ASAP, then alphabetical.
        from pathlib import Path
        users_dir = Path(r"D:\AI\Astor-Memory-Runtime\users")
        all_users = sorted(d.name for d in users_dir.iterdir() if d.is_dir())
        priority_users = [u for u in all_users if u == "admin"]
        other_users = [u for u in all_users if u != "admin"]
        tier_user_combos = [("public", None), ("source", None)]
        tier_user_combos += [("private", u) for u in priority_users + other_users]
    else:
        tier_user_combos = [(t, None) for t in tiers]

    # v1.14.5: bypass ACL for admin CLI — direct sqlite access (admin has
    # all grants in grants.db but private_<user> requires user-side grant
    # to read). This is admin/CLI tool, ACL not relevant.
    import sqlite3
    from .._internal.acl_layout import get_db_path as _gdp

    def _direct_walk(tier: str, user_id: str | None) -> list[tuple[int, str]]:
        """Direct sqlite walk — bypass ACL for admin CLI."""
        bus_path = _gdp(tier, "bus", user_id)
        conn = sqlite3.connect(str(bus_path))
        rows = conn.execute(
            "SELECT id, content FROM memory_canonical WHERE tombstoned = 0 ORDER BY id"
        ).fetchall()
        conn.close()
        return [(int(r[0]), r[1]) for r in rows]

    def _direct_store(fact_id: int, content: str, tier: str, user_id: str | None) -> None:
        """Direct nest DB write — bypass ACL."""
        from ..nest.embeddings import astor_get_embedding_model
        nest_path = _gdp(tier, "nest", user_id)
        conn = sqlite3.connect(str(nest_path))
        model = astor_get_embedding_model(model_name)
        emb = list(model.embed([content]))[0]
        import struct
        blob = struct.pack(f'{len(emb)}f', *emb)
        conn.execute(
            """INSERT OR REPLACE INTO embeddings
               (fact_id, embedding, model_name, dim, updated_at, user_id, tier, publishable)
               VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?, ?, 0)""",
            (fact_id, blob, model_name, len(emb), user_id or '_current', tier),
        )
        conn.commit()
        conn.close()

    summary = {
        "model": model_name,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dry_run": args.dry_run,
        "tiers_walked": [],
        "total_facts": 0,
        "total_embedded": 0,
        "total_skipped_no_change": 0,
        "errors": [],
    }

    for tier, user_id in tier_user_combos:
        print(f"\n[reembed] tier={tier} user={user_id or '*'}")
        # v1.14.5: bypass ACL via direct sqlite (admin CLI tool)
        try:
            rows = _direct_walk(tier, user_id)
        except Exception as e:
            print(f"  SKIP (no db / no access): {e}")
            continue
        if args.limit:
            rows = rows[: args.limit]
        print(f"  facts to process: {len(rows)}")
        summary["tiers_walked"].append({"tier": tier, "user": user_id, "n": len(rows)})
        summary["total_facts"] += len(rows)

        # Check existing embed count for this model
        import sqlite3
        from .._internal.acl_layout import get_db_path as _gdp
        try:
            nest_path = _gdp(tier, "nest", user_id)
            nest_conn = sqlite3.connect(str(nest_path))
            existing_count = nest_conn.execute(
                "SELECT COUNT(*) FROM embeddings WHERE model_name=?", (model_name,)
            ).fetchone()[0]
            print(f"  already embedded ({model_name}): {existing_count}")
            nest_conn.close()
        except Exception as e:
            existing_count = 0
            print(f"  no nest db yet: {e}")

        embedded = 0
        skipped = 0
        t_start = time.time()
        for i, (fid, content) in enumerate(rows, 1):
            if not content:
                skipped += 1
                continue
            try:
                if not args.dry_run:
                    _direct_store(int(fid), str(content), tier, user_id)
                embedded += 1
            except Exception as e:
                summary["errors"].append({"fid": int(fid), "tier": tier, "user": user_id, "err": str(e)})
                if len(summary["errors"]) <= 5:
                    print(f"    ERR fid={fid}: {e}")
            if i % args.batch_size == 0:
                rate = i / max(1, time.time() - t_start)
                eta = (len(rows) - i) / max(0.1, rate)
                print(f"    [{i}/{len(rows)}] embedded={embedded} skipped={skipped} rate={rate:.1f}/s eta={eta:.0f}s")

        elapsed = time.time() - t_start
        rate = embedded / max(0.1, elapsed)
        print(f"  tier done: embedded={embedded} skipped={skipped} elapsed={elapsed:.0f}s rate={rate:.1f}/s")
        summary["total_embedded"] += embedded
        summary["total_skipped_no_change"] += skipped

    summary["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out = METRICS / f"reembed_{int(time.time())}.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[reembed] DONE. summary -> {out.name}")
    print(f"  total embedded: {summary['total_embedded']}  skipped: {summary['total_skipped_no_change']}  errors: {len(summary['errors'])}")
    return 0 if not summary["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
