"""eval_runner.py — Astor recall evaluation harness

v1.0.0 (2026-09-08, ship)

Usage:
    python eval_runner.py                          # baseline (current production config)
    python eval_runner.py --variant vector_only    # pure vector (no hybrid)
    python eval_runner.py --variant rerank_off    # hybrid but no LLM rerank
    python eval_runner.py --variant stage_off     # no stage_recall entity boost
    python eval_runner.py --all                   # run all variants + compare

Metrics:
- hit_rate@k: fraction of queries where at least min_hits expected_keywords
  appear in top-k results.
- mrr: mean reciprocal rank of first matching result (1/rank; 0 if miss).
- avg_latency_ms: per-query mean.

Outputs:
- astor/metrics/eval_<variant>_<ts>.json      (full per-query detail)
- astor/metrics/eval_<variant>_<ts>_summary.json  (aggregate metrics)
- astor/metrics/eval_history.jsonl           (append summary; trend track)
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(r"D:\AI\astor-memory")
TESTS_DIR = ROOT / "tests"
METRICS_DIR = ROOT / "astor" / "metrics"
METRICS_DIR.mkdir(parents=True, exist_ok=True)

SERVER = os.environ.get("ASTOR_SERVER", "http://127.0.0.1:7803")
EVAL_SET = TESTS_DIR / "eval_set.jsonl"


def load_eval_set() -> list[dict]:
    with open(EVAL_SET, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def recall(query: str, tier: str, user: str, top_k: int, **kwargs) -> tuple[list[dict], float]:
    body = {"query": query, "tier": tier, "user": user, "top_k": top_k, **kwargs}
    import time as _t
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                f"{SERVER}/v1/read",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
            )
            t0 = time.perf_counter()
            with urllib.request.urlopen(req, timeout=30) as r:
                payload = json.loads(r.read())
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return payload.get("results", []) or [], elapsed_ms
        except (urllib.error.URLError, ConnectionRefusedError, TimeoutError) as e:
            last_err = e
            _t.sleep(5 * (attempt + 1))
            print(f"  [retry {attempt+1}/3] {type(e).__name__}: {e}", file=__import__('sys').stderr)
    raise RuntimeError(f"recall failed after 3 retries: {last_err}")


def score_results(results: list[dict], expected_keywords: list[str], min_hits: int) -> dict:
    """Compute hit/mrr for one query.

    A result "matches" if its content contains ANY of the expected_keywords
    (case-insensitive substring match). mrr = 1/(first match rank); 0 if no match.
    """
    if not results:
        return {"matched": False, "mrr": 0.0, "n_matches": 0, "first_rank": None}
    expected = [kw.lower() for kw in expected_keywords]
    first_rank = None
    n_matches = 0
    for rank, r in enumerate(results, start=1):
        content = (r.get("content") or r.get("text") or "").lower()
        if any(kw in content for kw in expected):
            if first_rank is None:
                first_rank = rank
            n_matches += 1
    return {
        "matched": n_matches >= min_hits,
        "mrr": (1.0 / first_rank) if first_rank else 0.0,
        "n_matches": n_matches,
        "first_rank": first_rank,
    }


def run_variant(variant: str, eval_set: list[dict]) -> dict:
    """Run one variant (a set of recall kwargs overrides)."""
    # Variant -> kwargs to merge into /v1/read body
    variants = {
        "baseline":   {"hybrid": True,  "rerank": "on"},   # production config
        "vector_only": {"hybrid": False, "rerank": "off"},
        "rerank_off":  {"hybrid": True,  "rerank": "off"},
        "stage_off":   {"hybrid": True,  "rerank": "on"},   # not yet togglable via body
    }
    kwargs = variants.get(variant, {})

    detail = []
    latencies = []
    for q in eval_set:
        try:
            results, lat_ms = recall(
                query=q["query"], tier=q.get("tier", "private"),
                user=q.get("user", "admin"), top_k=q.get("top_k", 10),
                **kwargs,
            )
        except Exception as e:
            detail.append({"qid": q["qid"], "error": str(e), "matched": False, "mrr": 0.0})
            continue
        s = score_results(results, q["expected_keywords"], q.get("min_hits", 1))
        detail.append({
            "qid": q["qid"],
            "query": q["query"],
            "category": q.get("category"),
            "latency_ms": round(lat_ms, 2),
            **{k: v for k, v in s.items()},
            "n_results": len(results),
            "top3_fids": [r.get("fact_id") for r in results[:3]],
        })
        latencies.append(lat_ms)

    n = len(detail)
    hit_rate = sum(1 for d in detail if d.get("matched")) / n if n else 0
    mrr = statistics.mean(d.get("mrr", 0) for d in detail) if n else 0
    avg_lat = statistics.mean(latencies) if latencies else 0
    p50_lat = statistics.median(latencies) if latencies else 0
    p95_lat = (statistics.quantiles(latencies, n=20)[18] if len(latencies) >= 20
               else max(latencies, default=0))

    summary = {
        "variant": variant,
        "kwargs": kwargs,
        "ts": datetime.now(timezone.utc).isoformat(),
        "n_queries": n,
        "hit_rate_at_k": round(hit_rate, 4),
        "mrr": round(mrr, 4),
        "avg_latency_ms": round(avg_lat, 2),
        "p50_latency_ms": round(p50_lat, 2),
        "p95_latency_ms": round(p95_lat, 2),
        "by_category": {},
    }
    by_cat: dict[str, list[float]] = {}
    for d in detail:
        cat = d.get("category") or "unknown"
        by_cat.setdefault(cat, []).append(d.get("mrr", 0))
    for cat, mrrs in by_cat.items():
        summary["by_category"][cat] = {
            "n": len(mrrs),
            "mrr": round(statistics.mean(mrrs), 4),
            "hit_rate": round(sum(1 for m in mrrs if m > 0) / len(mrrs), 4),
        }

    return {"summary": summary, "detail": detail}


def save_run(run: dict) -> tuple[Path, Path]:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    variant = run["summary"]["variant"]
    detail_path = METRICS_DIR / f"eval_{variant}_{ts}.json"
    summary_path = METRICS_DIR / f"eval_{variant}_{ts}_summary.json"
    detail_path.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_path.write_text(json.dumps(run["summary"], ensure_ascii=False, indent=2), encoding="utf-8")
    # Append to history (one-line per run)
    history_path = METRICS_DIR / "eval_history.jsonl"
    with open(history_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(run["summary"], ensure_ascii=False) + "\n")
    return detail_path, summary_path


def print_summary(run: dict) -> None:
    s = run["summary"]
    print(f"\n=== {s['variant']} ===")
    print(f"  hit_rate@{10}     = {s['hit_rate_at_k']:.3f}")
    print(f"  mrr               = {s['mrr']:.3f}")
    print(f"  avg / p50 / p95 ms= {s['avg_latency_ms']:.0f} / {s['p50_latency_ms']:.0f} / {s['p95_latency_ms']:.0f}")
    if s["by_category"]:
        print(f"  by category:")
        for cat, m in sorted(s["by_category"].items()):
            print(f"    {cat:12} n={m['n']:>2}  hit={m['hit_rate']:.2f}  mrr={m['mrr']:.3f}")
    # misses
    misses = [d for d in run["detail"] if not d.get("matched")]
    if misses:
        print(f"  misses ({len(misses)}):")
        for m in misses[:5]:
            print(f"    {m['qid']}: {m.get('query','?')[:50]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="baseline",
                    choices=["baseline", "vector_only", "rerank_off", "stage_off"])
    ap.add_argument("--all", action="store_true", help="Run all variants")
    ap.add_argument("--set", default=str(EVAL_SET), help="Path to eval set jsonl")
    args = ap.parse_args()

    eval_set_path = Path(args.set)
    if not eval_set_path.exists():
        print(f"ERROR: eval set not found at {eval_set_path}", file=sys.stderr)
        return 2
    eval_set = load_eval_set()
    print(f"Loaded {len(eval_set)} queries from {eval_set_path}")
    print(f"Server: {SERVER}\n")

    variants = ["baseline", "vector_only", "rerank_off"] if args.all else [args.variant]
    runs = []
    for v in variants:
        run = run_variant(v, eval_set)
        print_summary(run)
        detail_p, summary_p = save_run(run)
        print(f"  saved: {summary_p.name}")
        runs.append(run)

    if args.all and len(runs) >= 2:
        print(f"\n=== VARIANT COMPARISON (mrr / hit_rate) ===")
        print(f"  {'variant':<14}  mrr    hit@10  avg_ms")
        for r in runs:
            s = r["summary"]
            print(f"  {s['variant']:<14}  {s['mrr']:.3f}  {s['hit_rate_at_k']:.3f}    {s['avg_latency_ms']:.0f}")
        # Suggest winner
        best = max(runs, key=lambda r: r["summary"]["mrr"])
        print(f"\n  >>> best mrr: {best['summary']['variant']} ({best['summary']['mrr']:.3f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
