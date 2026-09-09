"""eval_trend.py — S19 (2026-09-08): trend analysis of eval_history.jsonl.

Reads last N baseline runs, computes min/max/mean of hit_rate/mrr/p95,
and detects improving/stable/regressing trend by comparing current vs earliest.
Saves summary to astor/metrics/eval_trend.json for downstream dashboards.

Usage:
    python eval_trend.py                  # default window=30
    python eval_trend.py --window=10      # last 10 baseline runs
    python eval_trend.py --quiet         # only print on regression
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("ASTOR_PROJECT_ROOT", "."))
HISTORY = Path(os.environ.get("ASTOR_METRICS_DIR", str(ROOT / "astor" / "metrics"))) / "eval_history.jsonl"
TREND_OUT = Path(os.environ.get("ASTOR_METRICS_DIR", str(ROOT / "astor" / "metrics"))) / "eval_trend.json"


def main() -> int:
    ap = argparse.ArgumentParser(description="eval_trend — summarize last N baseline runs from eval_history.jsonl")
    ap.add_argument("--window", type=int, default=30, help="number of recent baseline runs to summarize (default 30)")
    ap.add_argument("--quiet", action="store_true", help="only print on regression")
    args = ap.parse_args()

    if not HISTORY.exists():
        print(f"[skip] {HISTORY} missing")
        return 0

    history = []
    for line in HISTORY.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                history.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    baselines = [h for h in history if h.get("variant") == "baseline"]
    last_n = baselines[-args.window:]

    if not last_n:
        print("[skip] no baseline runs in history")
        return 0

    trend = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_size": len(last_n),
        "n_baseline_runs_total": len(baselines),
        "window_first_ts": last_n[0]["ts"],
        "window_last_ts": last_n[-1]["ts"],
        "hit_rate": _stats(b.get("hit_rate_at_k", 0) for b in last_n),
        "mrr": _stats(b.get("mrr", 0) for b in last_n),
        "p95_latency_ms": _stats(b.get("p95_latency_ms", 0) for b in last_n),
    }

    if len(last_n) >= 2:
        cur = last_n[-1]
        earliest = last_n[0]
        trend["delta"] = {
            "mrr": round(cur["mrr"] - earliest["mrr"], 4),
            "hit_rate": round(cur["hit_rate_at_k"] - earliest["hit_rate_at_k"], 4),
            "p95_latency_ms": cur["p95_latency_ms"] - earliest["p95_latency_ms"],
        }
        mrr_delta = trend["delta"]["mrr"]
        if mrr_delta > 0.02:
            trend["trend_status"] = "improving"
        elif abs(mrr_delta) <= 0.02:
            trend["trend_status"] = "stable"
        else:
            trend["trend_status"] = "regressing"

    TREND_OUT.write_text(json.dumps(trend, indent=2, ensure_ascii=False), encoding="utf-8")

    if not args.quiet or trend.get("trend_status") == "regressing":
        print(json.dumps(trend, indent=2, ensure_ascii=False))
    return 0 if trend.get("trend_status") != "regressing" else 1


def _stats(values):
    values = list(values)
    if not values:
        return {"min": None, "max": None, "mean": None}
    return {
        "min": min(values),
        "max": max(values),
        "mean": round(sum(values) / len(values), 4),
    }


if __name__ == "__main__":
    raise SystemExit(main())
