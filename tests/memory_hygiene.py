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

os.environ.setdefault("ASTOR_DIR", r"astor-runtime-dir")

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


# 2026-10-23 (HaluMem 解读): public 层 kind='fact' 719 条里 409 条 (56.9%)
# 是测试残留 / 模板噪音, 而且 access_count 高达 300-500 —— 正在污染召回。
# 其中绝大多数**不以**已知探针前缀开头 ("Maria 启动了 e2e_correction_..."),
# 所以 PROBE_PREFIXES 的 startswith 抓不到。要按**内容模式**抓。
#
# 这些不是"用户的事实", 是 astor 自己的测试和 e2e 写进知识库的。
# 论文依据: HaluMem 的 FMR (抗虚构记忆率) 指标专门测"不该存的被存成事实"。
TEST_RESIDUE_PATTERNS = (
    # e2e 测试轮次: "Maria 启动了 e2e_correction_<id>_<ts> 项目"
    re.compile(r"e2e_correction_\w+_\d+"),
    re.compile(r"e2e_test_marker_\w+"),
    # 通用测试/调试 marker
    re.compile(r"\bmarker[-\s]?(test|fact|distill|publish|visibility)", re.I),
    # "marker-<任意串>" —— 覆盖 marker-final / marker-q1w2e3 / marker-methodtest
    re.compile(r"\bmarker[-\s][a-z0-9]", re.I),
    # astor e2e 场景里虚构的人名 "Maria" —— 只匹配 e2e 叙述, 不能匹配
    # 真实提到 Maria 的条目 (例如 "Maria 是用户的同事")。
    re.compile(r"^Maria\b.*(测试|test|项目|启动|完成|marker|e2e|新发布|发布)", re.I),
    re.compile(r"\bmethod curl (test|UA)", re.I),
    re.compile(r"^(async test|profile test|poc|curl test|debug_timing)\b", re.I),
    re.compile(r"^profile (timing|debug)\b", re.I),
    re.compile(r"^async (final )?verify\b", re.I),
    re.compile(r"\bhook_audit_test_", re.I),
    # "她/他 启动了 ... 项目" —— e2e 场景脚本的叙述残留
    re.compile(r"^她?他?们?实际上?没有?启动[了]?\s*e2e_", re.I),
    re.compile(r"^她?他?们?完成了\s+e2e_", re.I),
    # 时间戳型一次性事实: "..._<10位时间戳>" 结尾且带测试动词
    re.compile(r"\b(test|probe|debug|mock)\b.*\b\d{10}\b", re.I),
)


def _test_residue_match(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    return any(p.search(t) for p in TEST_RESIDUE_PATTERNS)


def _bus_path(tier: str, user_id: str | None) -> Path | None:
    if tier == "public":
        cands = list(RUNTIME.rglob("astor_bus_public.db"))
    elif tier == "source":
        cands = list(RUNTIME.rglob("astor_bus_source.db"))
    else:
        cands = [RUNTIME / "users" / (user_id or "admin") / "memory" /
                 f"astor_bus_{user_id or 'admin'}.db"]
    # 2026-10-23 bug: rglob 会把 runtime 里的**备份副本**一起匹配到。
    # .bak_bus_pre_* / .bak* 目录里的库是 apply 之前的快照, 活 fact 往往
    # 比生产库多 —— "选活 fact 最多的那个" 于是选中了备份, 于是 scan 读
    # 备份而 apply 通过 REST 打生产库, 两边 id 可能对不上 (这次侥幸一致,
    # 因为备份就是 apply 前的精确副本; 一旦备份来自更早的时间点就会
    # 隔离错 fact)。备份目录必须排除。
    cands = [c for c in cands
             if c.exists()
             and not any(part.startswith(".bak")
                         or part.endswith(".bak")
                         or "_bak_" in part
                         or "backup" in part.lower()
                         for part in c.parts)]
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


# 结构性头部: 连续的 [key: value] 行。知识分页的 fact 都是这个形状 ——
#   [source: 12-why-not-prediction.md]
#   [section: 證據三：卦德 > 五行]
#   [tag: 命理反思]
#   [topic: 命理边界]
#   <真正的正文>
_STRUCT_HEADER = re.compile(r"^(?:\[.*\]\s*$\n?)+")
# 数字 (含小数) 统一掩码: 让"只差一个时间戳/计数"的模板落进同一组
_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _shape_key(text: str) -> str:
    """近似重复的形状键。

    2026-10-23 修: 原来只按前 40 个字符分组, 于是**同一源文件的不同章节**
    被判成重复 —— 它们的 [source:]/[section:]/[tag:]/[topic:] 头部完全相同,
    前 40 字一模一样, 但正文讲的是不同的东西。实测误删了三个章节
    (12-why-not-prediction.md 的 證據二 / 證據三 / 所以該怎麼用), 其中
    一个正是"卦德 > 五行, 解释力约 5 倍"。

    修法: 先剥掉结构性头部, 再把数字掩码, 然后比较。同一模板只差时间戳的
    (maker_pocket V085_TP_CAP 那 240 条) 掩码后形状一致, 照样被抓到;
    不同章节剥掉头部后正文不同, 不会再被误判。
    """
    body = _STRUCT_HEADER.sub("", text or "")
    return _NUM.sub("#", body)[:600]


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
    shape_groups: dict[str, list[int]] = {}
    already: set[int] = set()
    for fid, content in rows:
        text = content or ""
        shape_groups.setdefault(_shape_key(text), []).append(fid)
        # One reason per fact: a fact that is already flagged as debris or
        # as a transcript must not also be counted as a duplicate, or the
        # totals double-count and the apply step forgets it twice.
        if fid in already:
            continue
        if _probe_match(text):
            groups.setdefault("probe_debris", []).append(fid)
            already.add(fid)
        elif _test_residue_match(text):
            groups.setdefault("test_residue", []).append(fid)
            already.add(fid)
        elif _exec_log_match(text):
            groups.setdefault("execution_log", []).append(fid)
            already.add(fid)

    dup_ids: list[int] = []
    for _shape, ids in shape_groups.items():
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