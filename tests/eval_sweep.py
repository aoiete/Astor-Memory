"""eval_sweep.py — multi-config recall evaluation

v1.0.0 (2026-09-08, ship)

Sweeps multiple knobs (bm25_weight, rerank top_k) to find best config.
Use after eval_runner.py ships the baseline.

Usage:
    python eval_sweep.py                          # all configs
    python eval_sweep.py --quick                  # 4 quick configs only
    python eval_sweep.py --set tests/eval_set.jsonl
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


def recall(query: str, top_k: int = 10, **kwargs) -> tuple[list[dict], float]:
    body = {"query": query, "tier": "private", "user": "admin", "top_k": top_k, **kwargs}
    req = urllib.request.Request(
        f"{SERVER}/v1/read",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.loads(r.read())
    return payload.get("results", []) or [], (time.perf_counter() - t0) * 1000.0


def score(results, kws, min_hits=1):
    if not results:
        return {"matched": False, "mrr": 0.0, "n": 0, "first_rank": None}
    kws_l = [k.lower() for k in kws]
    first_rank = None
    n = 0
    for r, res in enumerate(results, 1):
        c = (res.get("content") or "").lower()
        if any(k in c for k in kws_l):
            if first_rank is None:
                first_rank = r
            n += 1
    return {"matched": n >= min_hits, "mrr": (1.0 / first_rank) if first_rank else 0.0,
            "n": n, "first_rank": first_rank}


CONFIGS = [
    # name, kwargs
    ("baseline_bm25.4",      {"hybrid": True, "rerank": "on",  "bm25_weight": 0.4}),
    ("high_bm25_bm25.6",     {"hybrid": True, "rerank": "on",  "bm25_weight": 0.6}),
    ("low_bm25_bm25.2",      {"hybrid": True, "rerank": "on",  "bm25_weight": 0.2}),
    ("high_vec_bm25.2",      {"hybrid": True, "rerank": "on",  "bm25_weight": 0.2}),
    ("pure_vec",             {"hybrid": False, "rerank": "off"}),
    ("hybrid_no_rerank",     {"hybrid": True,  "rerank": "off"}),
    ("hybrid_rerank",        {"hybrid": True,  "rerank": "on"}),
    # v1.14.5: dual-model merge (server-side ASTOR_DUAL_MODEL=1) — only effective
    # when server is running with the env var. Tested via env-controlled reruns.
    ("dual_model_off",       {"hybrid": True,  "rerank": "on"}),  # baseline for comparison
    ("dual_model_on",        {"hybrid": True,  "rerank": "on"}),  # baseline + body hint; server env decides
]


def run_config(name: str, kwargs: dict, eval_set: list[dict]) -> dict:
    detail = []
    lats = []
    for q in eval_set:
        try:
            results, lat = recall(q["query"], q.get("top_k", 10), **kwargs)
        except Exception as e:
            detail.append({"qid": q["qid"], "error": str(e), "matched": False, "mrr": 0.0})
            continue
        s = score(results, q["expected_keywords"], q.get("min_hits", 1))
        detail.append({"qid": q["qid"], "category": q.get("category"), **s,
                       "latency_ms": round(lat, 2)})
        lats.append(lat)
    n = len(detail)
    return {
        "name": name, "kwargs": kwargs,
        "ts": datetime.now(timezone.utc).isoformat(),
        "n_queries": n,
        "hit_rate": round(sum(1 for d in detail if d["matched"]) / n, 4) if n else 0,
        "mrr": round(statistics.mean(d["mrr"] for d in detail), 4) if n else 0,
        "avg_latency_ms": round(statistics.mean(lats), 2) if lats else 0,
        "p95_latency_ms": round(statistics.quantiles(lats, n=20)[18], 2) if len(lats) >= 20 else (max(lats, default=0)),
        "misses": [d["qid"] for d in detail if not d["matched"]],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--set", default=str(EVAL_SET))
    args = ap.parse_args()

    eval_set = load_eval_set()
    print(f"Loaded {len(eval_set)} queries. Sweeping {len(CONFIGS) if not args.quick else 4} configs...")

    configs = CONFIGS[:4] if args.quick else CONFIGS

    rows = []
    for name, kw in configs:
        r = run_config(name, kw, eval_set)
        rows.append(r)
        print(f"  {name:24}  hit={r['hit_rate']:.3f}  mrr={r['mrr']:.3f}  "
              f"avg_ms={r['avg_latency_ms']:.0f}  p95_ms={r['p95_latency_ms']:.0f}  "
              f"misses={r['misses']}")

    # Save sweep
    sweep_path = METRICS_DIR / f"eval_sweep_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    sweep_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n=== COMPARISON (sorted by mrr desc) ===")
    sorted_rows = sorted(rows, key=lambda r: r["mrr"], reverse=True)
    print(f"  {'config':<24}  mrr    hit    avg_ms  p95_ms")
    for r in sorted_rows:
        print(f"  {r['name']:<24}  {r['mrr']:.3f}  {r['hit_rate']:.3f}  "
              f"{r['avg_latency_ms']:.0f}      {r['p95_latency_ms']:.0f}")
    best = sorted_rows[0]
    print(f"\n  >>> best: {best['name']}  mrr={best['mrr']:.3f}  kwargs={best['kwargs']}")

    # Append sweep summary to history
    with open(METRICS_DIR / "eval_sweep_history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "sweep": rows,
                            "best": best["name"]}, ensure_ascii=False) + "\n")

    print(f"\nSaved: {sweep_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
