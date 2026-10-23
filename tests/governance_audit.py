"""governance_audit.py — 记忆治理体检 (Self-Evolving 四 的 8 问清单可执行版)

来源: 《Agent开发·Self-Evolving (四): 记忆系统的核心不是存储, 而是治理》
      https://mp.weixin.qq.com/s/wGKX4UdTc_Xb2Z_laHqK4g

文章的核心主张: **Agent Memory 的难点不是把信息存下来, 而是决定什么该写、
如何治理、何时召回。没有治理的记忆, 存得越多, 污染未来任务的机会越大。**

文章给了 8 条自检清单。这个脚本把它变成**可运行的诊断**, 每一条都给出
真实数字而不是"应该有":

  Q1 是否把所有过程数据都直接写进长期记忆?   -> 量化执行日志型 fact 占比
  Q2 每条记忆是否有明确的来源/触发原因/适用范围? -> 字段覆盖率
  Q3 是否区分 临时/事实/情景/程序性?          -> kind / memory_class 分布
  Q4 是否存在 TTL/使用频率衰减/负反馈淘汰?    -> 淘汰通道的活 fact 占比
  Q5 多 Agent 并发写入: 版本保护/冲突处理?    -> revision / superseded_by
  Q6 读取是否渐进披露 + 上下文预算?            -> top_k 分布
  Q7 检索结果是否回到原始证据核验?             -> event_id 可回溯率
  Q8 评测失败后能否定位并回滚?                 -> versioned / invalidated 通道

用法:
    python governance_audit.py                 # 审计 admin private tier
    python governance_audit.py --tier public
    python governance_audit.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

os.environ.setdefault("ASTOR_DIR", r"astor-runtime-dir")

# 执行日志的典型特征 (文章说的"把执行日志直接当成知识库")
NOISE_PATTERNS = [
    (re.compile(r"powershell\s+-NoProfile", re.I), "powershell 命令"),
    (re.compile(r"hermes-agent/\.venv/Scripts/python", re.I), "venv python 路径"),
    (re.compile(r"^\s*\[?[A-Z_ ]{0,12}LESSON\]?\s*.*?[a-zA-Z]:[\\/]", re.M), "命令路径开头"),
    (re.compile(r"^\s*\[20\d\d-\d\d-\d\d", re.M), "日志时间戳行"),
    (re.compile(r"Wrote \d+ bytes"), "写入回执"),
]


def _bus_db(tier: str, user_id: str) -> Path | None:
    """找 bus db。布局是 users/<user>/memory/astor_bus_<tier>_<user>.db,
    但历史上有 astor_bus_<user>.db / astor_bus_<tier>.db 等变体,
    所以按优先级逐个 glob, 挑**活 fact 最多**的那个。"""
    root = Path(os.environ["ASTOR_DIR"])
    patterns = [
        f"astor_bus_{tier}_{user_id}.db",
        f"astor_bus_{user_id}.db",
        f"astor_bus_{tier}.db",
    ]
    cands: list[Path] = []
    for pat in patterns:
        cands.extend(root.rglob(pat))
    # 去重, 保留顺序
    seen, uniq = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    if not uniq:
        return None
    best, best_n = None, -1
    for c in uniq:
        try:
            cc = sqlite3.connect(str(c))
            k = cc.execute(
                "SELECT COUNT(*) FROM memory_canonical WHERE tombstoned=0"
            ).fetchone()[0]
            cc.close()
        except Exception:
            continue
        if k > best_n:
            best, best_n = c, k
    return best or uniq[0]


def audit(tier: str = "private", user_id: str = "admin") -> dict:
    out: dict = {"tier": tier, "user_id": user_id, "checks": []}

    def check(qid: str, question: str, verdict: str, detail: str, metric=None):
        out["checks"].append({
            "id": qid, "question": question,
            "verdict": verdict, "detail": detail, "metric": metric,
        })

    db = _bus_db(tier, user_id)
    if not db or not db.exists():
        out["error"] = f"bus db not found for {tier}/{user_id}"
        return out
    out["db"] = str(db)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    cols = [r[1] for r in conn.execute("PRAGMA table_info(memory_canonical)")]
    rows = conn.execute(
        "SELECT * FROM memory_canonical WHERE tombstoned=0").fetchall()
    n = len(rows) or 1
    out["n_facts"] = len(rows)

    # ---- Q1 执行日志污染 ----
    noise_ids = set()
    for r in rows:
        txt = r["content"] or ""
        for pat, _label in NOISE_PATTERNS:
            if pat.search(txt):
                noise_ids.add(r["id"])
                break
    noise_pct = len(noise_ids) / n * 100
    check("Q1", "是否把所有过程数据都直接写进长期记忆?",
          "WARN" if noise_pct > 1 else "OK",
          f"{len(noise_ids)}/{n} ({noise_pct:.1f}%) 是执行日志型 fact",
          {"noise": len(noise_ids), "total": n, "pct": round(noise_pct, 2)})

    # ---- Q2 来源/触发原因/适用范围 ----
    have = {}
    for col in ("event_id", "origin_session_id", "scope_type", "namespace",
                "provenance_kind", "source_ref", "evidence_quote"):
        if col in cols:
            k = conn.execute(
                f"SELECT COUNT(*) FROM memory_canonical "
                f"WHERE tombstoned=0 AND {col} IS NOT NULL").fetchone()[0]
            have[col] = round(k / n * 100, 1)
    # 任一溯源字段有值的比例
    src_cols = [c for c in ("event_id", "source_ref", "provenance_kind") if c in cols]
    traced = 0
    if src_cols:
        traced = conn.execute(
            "SELECT COUNT(*) FROM memory_canonical WHERE tombstoned=0 AND ("
            + " OR ".join(f"{c} IS NOT NULL" for c in src_cols) + ")").fetchone()[0]
    traced_pct = traced / n * 100
    check("Q2", "每条记忆是否有明确的来源/触发原因/适用范围?",
          "OK" if traced_pct >= 95 else "WARN",
          f"{traced_pct:.1f}% 的 fact 有可回溯来源; 字段覆盖率 {have}",
          {"traced_pct": round(traced_pct, 1), "coverage": have})

    # ---- Q3 记忆类型分层 ----
    kind_dist = {}
    for col in ("kind", "memory_class", "scope_type"):
        if col in cols:
            kind_dist[col] = [
                (str(r[0]), r[1]) for r in conn.execute(
                    f"SELECT {col}, COUNT(*) FROM memory_canonical "
                    f"WHERE tombstoned=0 GROUP BY {col} ORDER BY 2 DESC").fetchall()]
    n_kinds = len(kind_dist.get("kind", []))
    n_classes = len(kind_dist.get("memory_class", []))
    n_scopes = len(kind_dist.get("scope_type", []))
    # v1.16.92: 临时层是否真的在用。scope_type 只有一个取值 (long_term)
    # 意味着记忆只有"永久"一种状态 —— 一次性观察和用户明确要求记住的
    # 规则享受同样的地位, 那就是治理缺失。
    # 文章说"不要只写一个类型, 也不要全都塞进同一个向量库"
    scope_detail = {}
    if "scope_type" in cols:
        scope_detail = {
            "dist": kind_dist.get("scope_type", []),
            "short_term": sum(
                1 for s_, n_ in kind_dist.get("scope_type", []) if s_ == "short_term"),
            "with_expiry": 0,
            "expired_pending": 0,
        }
        if "expires_at" in cols:
            scope_detail["with_expiry"] = conn.execute(
                "SELECT COUNT(*) FROM memory_canonical "
                "WHERE tombstoned=0 AND expires_at IS NOT NULL").fetchone()[0]
            scope_detail["expired_pending"] = conn.execute(
                "SELECT COUNT(*) FROM memory_canonical "
                "WHERE tombstoned=0 AND scope_type='short_term' "
                "  AND expires_at IS NOT NULL "
                "  AND expires_at <= strftime('%Y-%m-%dT%H:%M:%fZ','now')"
            ).fetchone()[0]
    scope_ok = (n_scopes >= 2) or (scope_detail.get("with_expiry", 0) == 0
                                   and n == 0)
    check("Q3", "是否区分 临时/事实/情景/程序性?",
          "OK" if n_kinds >= 3 and n_classes >= 2 and scope_ok else "WARN",
          f"kind 种类 {n_kinds}, memory_class 种类 {n_classes}, "
          f"scope 种类 {n_scopes} "
          f"(临时层 {scope_detail.get('short_term', 0)} 条, "
          f"带到期时间 {scope_detail.get('with_expiry', 0)} 条)",
          {"kinds": kind_dist, "scope": scope_detail})

    # ---- Q4 主动淘汰通道 ----
    ttl_cols = [c for c in ("valid_until", "expires_at", "invalidated_at",
                            "last_confirmed_at") if c in cols]
    decayed = 0
    if "last_confirmed_at" in cols:
        decayed = conn.execute(
            "SELECT COUNT(*) FROM memory_canonical "
            "WHERE tombstoned=0 AND last_confirmed_at IS NOT NULL").fetchone()[0]
    tomb_total = conn.execute(
        "SELECT COUNT(*) FROM memory_canonical").fetchone()[0]
    tomb_alive = n
    check("Q4", "是否存在 TTL/使用频率衰减/负反馈淘汰?",
          "OK" if ttl_cols else "WARN",
          f"淘汰字段 {ttl_cols or '无'}; 已淘汰 {tomb_total - tomb_alive} 条; "
          f"有确认时间的 {decayed}",
          {"ttl_cols": ttl_cols, "tombstoned": tomb_total - tomb_alive,
           "confirmed": decayed})

    # ---- Q5 版本保护 / 冲突处理 ----
    rev = 0
    sup = 0
    if "revision" in cols:
        rev = conn.execute(
            "SELECT COUNT(*) FROM memory_canonical "
            "WHERE tombstoned=0 AND revision IS NOT NULL").fetchone()[0]
    if "superseded_by" in cols:
        sup = conn.execute(
            "SELECT COUNT(*) FROM memory_canonical "
            "WHERE tombstoned=0 AND superseded_by IS NOT NULL").fetchone()[0]
    check("Q5", "多 Agent 并发写入: 版本保护/冲突处理/乐观锁?",
          "OK" if ("revision" in cols or "superseded_by" in cols) else "GAP",
          f"有 revision 的 {rev} 条, 有 superseded_by 的 {sup} 条",
          {"revision": rev, "superseded_by": sup})

    # ---- Q6 渐进披露 ----
    disp = {}
    for col in ("access_count", "distinct_queries_hit", "promoted_at",
                "visibility", "status"):
        if col in cols:
            disp[col] = conn.execute(
                f"SELECT COUNT(*) FROM memory_canonical "
                f"WHERE tombstoned=0 AND {col} IS NOT NULL").fetchone()[0]
    check("Q6", "读取是否采用渐进披露 + 严格上下文预算?",
          "OK" if disp else "GAP",
          f"渐进披露相关字段: {disp}",
          {"fields": disp})

    # ---- Q7 证据核验 ----
    ev = 0
    if "event_id" in cols:
        ev = conn.execute(
            "SELECT COUNT(*) FROM memory_canonical "
            "WHERE tombstoned=0 AND event_id IS NOT NULL").fetchone()[0]
    ev_pct = ev / n * 100
    check("Q7", "检索结果是否会回到原始证据进行核验?",
          "OK" if ev_pct >= 90 else "WARN",
          f"{ev_pct:.1f}% 的 fact 能回溯到原始 event",
          {"traced_pct": round(ev_pct, 1)})

    # ---- Q8 可回滚 ----
    rb = []
    for col in ("valid_until", "invalidated_by", "invalidated_reason",
                "revision", "parent_revision_id", "superseded_by"):
        if col in cols:
            rb.append(col)
    check("Q8", "评测失败后, 能否定位并回滚造成问题的记忆更新?",
          "OK" if rb else "GAP",
          f"可回滚通道字段: {rb or '无'}",
          {"fields": rb})

    # ---- 附加: 重复度 (记忆腐烂) ----
    exact = Counter((r["content"] or "") for r in rows)
    dup_extra = sum(v - 1 for v in exact.values() if v > 1)
    pref = Counter((r["content"] or "")[:60] for r in rows)
    near = sum(v - 1 for v in pref.values() if v > 1)
    out["duplicates"] = {
        "exact_extra": dup_extra,
        "exact_pct": round(dup_extra / n * 100, 2),
        "near60_extra": near,
        "near60_pct": round(near / n * 100, 2),
    }

    conn.close()
    return out


VERDICT_ICON = {"OK": "OK  ", "WARN": "WARN", "GAP": "GAP "}


def print_report(a: dict) -> None:
    if "error" in a:
        print(f"ERROR: {a['error']}")
        return
    print(f"=== 记忆治理体检 ({a['tier']}/{a['user_id']}) ===")
    print(f"  db: {a.get('db')}")
    print(f"  活 fact: {a['n_facts']}")
    print("")
    for c in a["checks"]:
        print(f"  [{VERDICT_ICON.get(c['verdict'], c['verdict'])}] {c['id']} {c['question']}")
        print(f"         {c['detail']}")
    d = a.get("duplicates", {})
    if d:
        print("")
        print(f"  重复: 完全相同 {d['exact_extra']} ({d['exact_pct']}%), "
              f"前60字符相同 {d['near60_extra']} ({d['near60_pct']}%)")
    verdicts = Counter(c["verdict"] for c in a["checks"])
    print("")
    print("  汇总: " + "  ".join(f"{k}={v}" for k, v in sorted(verdicts.items())))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="private")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    a = audit(args.tier, args.user)
    print_report(a)
    if args.json:
        Path(args.json).write_text(json.dumps(a, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"\n  saved: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())