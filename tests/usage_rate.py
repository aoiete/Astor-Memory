"""usage_rate.py — WorldMemArena 第四段指标: **使用率 (use)**

为什么需要这个指标 (2026-10-22, 读 arxiv 2605.29341 / WorldMemArena)
------------------------------------------------------------------------
论文的核心发现: **召回率高 ≠ 答得对**。
  M2A      recall 86.83%  ->  QA 50.14%
  MemGPT   recall 85.20%  ->  QA 57.81%
"写入了多少" 和 "回答时能否调用正确证据" 不是一回事。

astor 现有的 eval 只测了前三段 (write / maintain / retrieve):
  hit_rate@k  = 期望关键词有没有出现在 top-k 结果里      (retrieve)
  mrr         = 第一个匹配结果的倒数排名                  (retrieve)
**第四段 "use" (证据有没有真的被用进最终输出) 我们从来没测过** ——
那 36 个百分点对我们完全是黑盒。

这个模块补的就是这一段, 但刻意做成**离线、无 LLM** 的确定性指标,
理由和论文的 Track A 一致 (可复现、可回归、不烧 token):

  evidence_found_rate  — 期望证据在 top-k 里的比例 (= 我们已有的召回)
  evidence_cited_rate  — 期望证据在 top-k 里的比例, 但**排位可被下游读到**
                         (即: 排在会被注入上下文的前 N 条内)
  usable_evidence_rate — 证据"可用"的占比: 证据存在 + 有 provenance +
                         没被 valid_until 作废 + 有内容(不是空壳)
  actionable_rate      — usable 证据里, 内容长度足够支撑一次决策的比例

关键区别: hit_rate 只问"在不在", 这里问"**能不能被用**"。
一个 fact 可能在 top-10 里, 但排在第 9 位、下游只取 top-5、或者它已经
被 valid_until 作废 —— 它"召回到了"却"用不上"。这正是论文说的缺口。

用法:
    python usage_rate.py                       # 跑默认 eval_set
    python usage_rate.py --top-k 10 --cite-k 5
    python usage_rate.py --json out.json
    python usage_rate.py --set tests/eval_set.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get(
    "ASTOR_PROJECT_ROOT",
    str(Path(__file__).resolve().parent.parent)))
TESTS_DIR = ROOT / "tests"
METRICS_DIR = ROOT / "astor" / "metrics"
METRICS_DIR.mkdir(parents=True, exist_ok=True)

SERVER = os.environ.get("ASTOR_SERVER", "http://127.0.0.1:7803")
EVAL_SET = TESTS_DIR / "eval_set.jsonl"

# 一条 fact 至少要有这么多字符, 才被认为"内容足以支撑一次决策"。
# 低于此长度的多半是残缺抽取 / 占位符, 召回到了也用不上。
MIN_USABLE_CHARS = 24


def _read_api(query: str, tier: str, user: str, top_k: int) -> list[dict]:
    body = json.dumps({"query": query, "tier": tier, "user": user,
                       "top_k": top_k}).encode()
    req = urllib.request.Request(
        f"{SERVER}/v1/read", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read()).get("results", []) or []


def _load_eval_set(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def score_one(query: str, results: list[dict], expected_keywords: list[str],
              top_k: int, cite_k: int) -> dict:
    """把 WorldMemArena 的 "use" 阶段拆成 4 个可测子指标。

    - evidence_found_rate : 期望证据是否在 top-k 内 (retrieve 段, 我们已有)
    - evidence_cited_rate : 期望证据是否在前 cite_k 条 (下游真正会看到的)
    - usable_evidence_rate: 证据是否"可用" (有内容 + 未被 valid_until 作废)
    - actionable_rate     : 可用证据里内容长度是否够支撑一次决策
    """
    expected = [kw.lower() for kw in expected_keywords]
    found_rank = None
    cited_rank = None
    for rank, r in enumerate(results, start=1):
        content = (r.get("content") or r.get("text") or "")
        if not expected:
            continue
        if any(kw in content.lower() for kw in expected):
            if found_rank is None:
                found_rank = rank
            if cited_rank is None and rank <= cite_k:
                cited_rank = rank

    found = found_rank is not None
    cited = cited_rank is not None

    # "可用性" 检查: 取 top-k 内第一条匹配证据, 看它是否可用
    usable = False
    actionable = False
    usable_reason = "not_found"
    if found:
        hit_fact = results[found_rank - 1]
        content = (hit_fact.get("content") or hit_fact.get("text") or "")
        has_body = len(content.strip()) >= MIN_USABLE_CHARS
        # valid_until 存在且已过期 -> 召回到了但作废了, 用不上
        invalidated = False
        vu = hit_fact.get("valid_until") or hit_fact.get("invalidated_by")
        if vu:
            try:
                expired = datetime.fromisoformat(str(vu).replace("Z", "+00:00"))
                if expired.tzinfo is None:
                    expired = expired.replace(tzinfo=timezone.utc)
                invalidated = expired <= datetime.now(timezone.utc)
            except Exception:
                invalidated = False
        if invalidated:
            usable_reason = "invalidated"
        elif hit_fact.get("tombstoned"):
            usable_reason = "tombstoned"
        elif not has_body:
            usable_reason = "too_short"
        else:
            usable = True
            usable_reason = "ok"
            actionable = True

    return {
        "found": found,
        "found_rank": found_rank,
        "cited": cited,
        "cited_rank": cited_rank,
        "usable": usable,
        "actionable": actionable,
        "usable_reason": usable_reason,
    }


def run(eval_set: list[dict], top_k: int = 10, cite_k: int = 5) -> dict:
    detail = []
    for q in eval_set:
        query = q.get("query", "")
        kws = q.get("expected_keywords", []) or []
        tier = q.get("tier", "private")
        user = q.get("user", "admin")
        try:
            results = _read_api(query, tier, user, top_k)
            err = None
        except (urllib.error.URLError, ConnectionRefusedError,
                TimeoutError) as e:
            results = []
            err = f"{type(e).__name__}: {e}"
        s = score_one(query, results, kws, top_k, cite_k)
        s.update({
            "qid": q.get("qid"),
            "query": query,
            "category": q.get("category", "unknown"),
            "error": err,
        })
        detail.append(s)

    n = len(detail) or 1
    ok = [d for d in detail if not d.get("error")]

    def rate(key: str) -> float:
        return round(sum(1 for d in ok if d.get(key)) / len(ok), 4) if ok else 0.0

    reasons: dict[str, int] = {}
    for d in ok:
        r = d.get("usable_reason", "unknown")
        reasons[r] = reasons.get(r, 0) + 1

    summary = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "top_k": top_k,
        "cite_k": cite_k,
        "n_queries": len(ok),
        "n_errors": len(detail) - len(ok),
        # WorldMemArena 的核心对照: 召回到 vs 真的能被用上
        "evidence_found_rate": rate("found"),
        "evidence_cited_rate": rate("cited"),
        "usable_evidence_rate": rate("usable"),
        "actionable_rate": rate("actionable"),
        "found_but_not_cited": sum(1 for d in ok if d.get("found") and not d.get("cited")),
        "found_but_not_usable": sum(1 for d in ok if d.get("found") and not d.get("usable")),
        "unusable_reasons": reasons,
    }
    # usage_gap = 召回到但用不上 —— 这就是论文里那 36 个百分点在我们身上的形状
    if summary["evidence_found_rate"] > 0:
        summary["usage_gap_pct"] = round(
            (1 - summary["usable_evidence_rate"] / summary["evidence_found_rate"]) * 100, 2)
    else:
        summary["usage_gap_pct"] = None

    by_cat: dict[str, dict] = {}
    for d in ok:
        c = by_cat.setdefault(d["category"], {"n": 0, "found": 0, "cited": 0, "usable": 0})
        c["n"] += 1
        c["found"] += 1 if d.get("found") else 0
        c["cited"] += 1 if d.get("cited") else 0
        c["usable"] += 1 if d.get("usable") else 0
    for c in by_cat.values():
        c["found_rate"] = round(c["found"] / c["n"], 4)
        c["cited_rate"] = round(c["cited"] / c["n"], 4)
        c["usable_rate"] = round(c["usable"] / c["n"], 4)
    summary["by_category"] = by_cat
    return {"summary": summary, "detail": detail}


def print_report(res: dict) -> None:
    s = res["summary"]
    print("\n=== usage_rate (WorldMemArena 第四段: use) ===")
    print(f"  queries            = {s['n_queries']} (errors {s['n_errors']})")
    print(f"  top_k / cite_k     = {s['top_k']} / {s['cite_k']}")
    print("")
    print(f"  evidence_found_rate  = {s['evidence_found_rate']:.3f}   <- retrieve 段 (我们已有)")
    print(f"  evidence_cited_rate  = {s['evidence_cited_rate']:.3f}   <- 排位够前, 下游看得见")
    print(f"  usable_evidence_rate = {s['usable_evidence_rate']:.3f}   <- 有内容 + 未被作废")
    print(f"  actionable_rate      = {s['actionable_rate']:.3f}   <- 内容够支撑决策")
    print("")
    print(f"  found but NOT cited  = {s['found_but_not_cited']}")
    print(f"  found but NOT usable = {s['found_but_not_usable']}")
    print(f"  usage_gap_pct        = {s['usage_gap_pct']}%   <- 召回到却用不上的比例")
    print(f"  unusable reasons     = {s['unusable_reasons']}")
    if s["by_category"]:
        print("\n  by category:")
        print(f"    {'category':<12} {'n':>3}  found  cited  usable")
        for cat, m in sorted(s["by_category"].items()):
            print(f"    {cat:<12} {m['n']:>3}  {m['found_rate']:.2f}   "
                  f"{m['cited_rate']:.2f}   {m['usable_rate']:.2f}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default=str(EVAL_SET))
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--cite-k", type=int, default=5)
    ap.add_argument("--json", default="", help="写完整结果到这个路径")
    args = ap.parse_args()

    set_path = Path(args.set)
    if not set_path.exists():
        print(f"ERROR: eval set not found: {set_path}", file=sys.stderr)
        return 2
    eval_set = _load_eval_set(set_path)
    print(f"Loaded {len(eval_set)} queries from {set_path}")
    print(f"Server: {SERVER}")

    res = run(eval_set, args.top_k, args.cite_k)
    print_report(res)

    out = Path(args.json) if args.json else METRICS_DIR / (
        f"usage_rate_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json")
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  saved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())