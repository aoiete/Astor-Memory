"""memory_hygiene.py — 记忆库清理: 隔离执行日志 / 测试残渣 / 近似重复

为什么需要 (2026-10-22, 治理文章《Self-Evolving(四)》)
------------------------------------------------------
文章的核心主张: "没有治理的记忆, 存得越多, 污染未来任务的机会越大。"
治理体检 (tests/governance_audit.py) 在真实语料上验证了这一点:

  · public 层 1396 条活 fact 里, **236 条是同一个模板**
    ("astor write path instrumentation probe 1...") = 共享层的 17%
  · admin 层 5014 条里 **240 条是同一个模板**
    ("maker_pocket V085_TP_CAP: {...}") = 4.8%
  · 另有 75 条正文就是一条命令 (python.exe -c " / powershell -NoProfile ...)

这些不是"少量噪声", 而是**同一个模板成百上千次地占着召回位**。
BM25 算的是 tf, 一个模板重复 236 次会在打分里赢过真正相关但只出现一次的
fact, 于是 top-k 被测试残渣填满。

工具行为
--------
* **默认 dry-run**, 只报告不改动。`--apply` 才动手。
* 隔离 = tombstone (走 /v1/forget 的 tombstone_only), **不删数据**, 可回滚。
* 每条都记原因, 便于事后审计与恢复。
* 只处理**明确判定**为垃圾的: 执行日志、已知测试探针前缀、同一前缀的
  近似重复 (保留最早的一条)。

用法:
    python memory_hygiene.py                      # 报告所有 tier
    python memory_hygiene.py --tier public
    python memory_hygiene.py --apply --tier public
    python memory_hygiene.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("ASTOR_DIR", r"<ASTOR_RUNTIME_DIR>")

SERVER = os.environ.get("ASTOR_SERVER", "http://127.0.0.1:7803")
RUNTIME = Path(os.environ.get("ASTOR_DIR"))

# 已知测试探针 / 调试残渣的前缀。这些是 astor 自己的测试和调试写进
# 知识库的, 永远不该出现在任何 tier 里。
PROBE_PREFIXES = (
    "astor write path instrumentation probe",
    "e2e_test_marker",
    "e2e_correction",
    "auto fork-",
    "middleware-warn-test",
    "LEAKPROBE",
    "VERIFY1",
    "demo probe from AstorClient",
    "profile debug print",
    "profile loop sub-stages",
    "post-restart tunnel test",
    "apple banana cherry",
)

# 近似重复: 前 N 字符相同即视为同一模板
DUP_PREFIX_LEN = 40
# 一个模板出现多少次以上才值得清理 (保留最早一条)
DUP_MIN_COUNT = 3


def _exec_log_match(text: str) -> bool:
    try:
        from astor_memory.forge.extractor import _EXEC_LOG_START
        return bool(_EXEC_LOG_START.match((text or "").lstrip()))
    except Exception:
        return False


def _probe_match(text: str) -> bool:
    t = (text or "").lstrip()
    return any(t.startswith(p) for p in PROBE_PREFIXES)


def _bus_path(tier: str, user_id: str | None) -> Path | None:
    if tier == "public":
        cands = list(RUNTIME.rglob("astor_bus_public.db"))
    elif tier == "source":
        cands = list(RUNTIME.rglob("astor_bus_source.db"))
    else:
        cands = [RUNTIME / "users" / (user_id or "admin") / "memory" /
                 f"astor_bus_{user_id or 'admin'}.db"]
    cands = [c for c in cands if c.exists()]
    if not cands:
        return None
    # 选活 fact 最多的那个
    best, best_n = None, -1
    for c in cands:
        try:
            cc = sqlite3.connect(str(c))
            n = cc.execute(
                "SELECT COUNT(*) FROM memory_canonical WHERE tombstoned=0"
            ).fetchone()[0]
            cc.close()
        except Exception:
            continue
        if n > best_n:
            best, best_n = c, n
    return best


def _forget(fact_id: int, tier: str, user_id: str | None,
            dry_run: bool) -> tuple[bool, str]:
    body = json.dumps({
        "fact_id": fact_id, "tier": tier, "user_id": user_id,
        "tombstone_only": True, "dry_run": dry_run,
    }).encode()
    req = urllib.request.Request(
        f"{SERVER}/v1/forget", data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
        return True, str(d.get("status") or d.get("ok") or "done")
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read()[:120].decode(errors='ignore')}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def scan(tier: str, user_id: str | None) -> dict:
    bus = _bus_path(tier, user_id)
    if not bus:
        return {"tier": tier, "user_id": user_id, "error": "bus db not found"}
    c = sqlite3.connect(str(bus))
    rows = c.execute(
        "SELECT id, content FROM memory_canonical WHERE tombstoned=0 "
        "ORDER BY id").fetchall()
    c.close()

    groups: dict[str, list[int]] = {}
    prefix_groups: dict[str, list[int]] = {}
    already: set[int] = set()
    for fid, content in rows:
        text = content or ""
        prefix_groups.setdefault(text[:DUP_PREFIX_LEN], []).append(fid)
        # One reason per fact: a fact that is already flagged as debris or
        # as a transcript must not also be counted as a duplicate, or the
        # totals double-count and the apply step forgets it twice.
        if fid in already:
            continue
        if _probe_match(text):
            groups.setdefault("probe_debris", []).append(fid)
            already.add(fid)
        elif _exec_log_match(text):
            groups.setdefault("execution_log", []).append(fid)
            already.add(fid)

    dup_ids: list[int] = []
    for prefix, ids in prefix_groups.items():
        if len(ids) >= DUP_MIN_COUNT:
            # keep the oldest of the group, quarantine the rest
            dup_ids.extend(i for i in ids[1:] if i not in already)
    if dup_ids:
        groups.setdefault("near_duplicate", []).extend(dup_ids)
        already.update(dup_ids)

    return {
        "tier": tier,
        "user_id": user_id,
        "bus": str(bus),
        "n_alive": len(rows),
        "groups": {k: v for k, v in groups.items()},
        "n_flagged": sum(len(v) for v in groups.values()),
        "n_alive_after": len(rows) - sum(len(v) for v in groups.values()),
    }


def apply(tier: str, user_id: str | None, plan: dict, dry_run: bool) -> dict:
    done, failed = 0, []
    for reason, ids in plan.get("groups", {}).items():
        for fid in ids:
            ok, msg = _forget(fid, tier, user_id, dry_run)
            if ok:
                done += 1
            else:
                failed.append({"fact_id": fid, "reason": reason, "error": msg})
    return {"quarantined": done, "failed": failed}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="all",
                    choices=["all", "public", "source", "private"])
    ap.add_argument("--user", default="admin")
    ap.add_argument("--apply", action="store_true", help="真的隔离 (默认只报告)")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    targets = []
    if args.tier in ("all", "public"):
        targets.append(("public", None))
    if args.tier in ("all", "source"):
        targets.append(("source", None))
    if args.tier in ("all", "private"):
        targets.append(("private", args.user))

    out = {"dry_run": not args.apply, "tiers": []}
    for tier, uid in targets:
        plan = scan(tier, uid)
        if plan.get("error"):
            out["tiers"].append(plan)
            continue
        result = apply(tier, uid, plan, dry_run=not args.apply)
        plan["result"] = result
        out["tiers"].append(plan)

    print(f"=== 记忆库清理{'（DRY-RUN，不改动）' if not args.apply else ''} ===")
    for p in out["tiers"]:
        if p.get("error"):
            print(f"  {p['tier']}: {p['error']}")
            continue
        print(f"\n  [{p['tier']}] 活 fact {p['n_alive']}  标记 {p['n_flagged']} "
              f"({p['n_flagged']/max(p['n_alive'],1)*100:.1f}%)")
        for reason, ids in p["groups"].items():
            print(f"     {reason:16s} {len(ids):5d}")
        r = p["result"]
        verb = "将隔离" if not args.apply else "已隔离"
        print(f"     -> {verb} {r['quarantined']} 条, 失败 {len(r['failed'])}")

    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"\n  saved: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())