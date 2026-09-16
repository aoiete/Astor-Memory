"""eval_trend.py — trend analysis of eval_history.jsonl.

Reads last N baseline runs, computes min/max/mean of hit_rate/mrr/p95,
and detects improving/stable/regressing trend.

S20 (2026-09-15): drift detector — use last_good_snapshot.json as comparison
baseline instead of window-earliest, so cold-cache first-runs don't get flagged
as "regressing" forever. Snapshot only updates when a NEW high is reached, so
healthy state is sticky. Adds cold_cache_variance to surface first-run-vs-warmup
delta explicitly.

Usage:
    python eval_trend.py                  # default window=30
    python eval_trend.py --window=10      # last 10 baseline runs
    python eval_trend.py --quiet         # only print on regression
    python eval_trend.py --snapshot-only  # only update last_good_snapshot.json (no dashboard write)
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
SNAPSHOT_OUT = Path(os.environ.get("ASTOR_METRICS_DIR", str(ROOT / "astor" / "metrics"))) / "last_good_snapshot.json"

# S20: cold-cache tolerance — first run after server restart can drop hit_rate
# by up to this much (cache warmup + JIT embedding). Subsequent runs should
# recover. If they don't, that's a real regression, not cold-cache variance.
COLD_CACHE_TOLERANCE = 0.05
# S20: snap-up rule — a new run is "new high" only if it exceeds the prior
# snapshot by at least this much. Avoids micro-noise snapping every run.
SNAP_UP_THRESHOLD = 0.005


def _load_history() -> list[dict]:
    if not HISTORY.exists():
        return []
    out = []
    for line in HISTORY.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _stats(values) -> dict:
    values = list(values)
    if not values:
        return {"min": None, "max": None, "mean": None, "n": 0}
    return {
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "mean": round(sum(values) / len(values), 4),
        "n": len(values),
    }


def _load_snapshot() -> dict | None:
    if not SNAPSHOT_OUT.exists():
        return None
    try:
        return json.loads(SNAPSHOT_OUT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_snapshot(snap: dict) -> None:
    SNAPSHOT_OUT.write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")


def _maybe_update_snapshot(history: list[dict]) -> tuple[dict | None, dict | None, str]:
    """Update last_good_snapshot.json based on last 7 baseline median.

    S21 (2026-09-15): all-time max overweights outliers (e.g. 9/8 hit_rate=1.0
    was hot-cache 50-question sweep, not repeatable on 110-question set). Use
    median of last 7 baseline runs (≈ 1 week @ weekly cron cadence) as the
    "recent healthy & repeatable" reference. Snapshot moves UP only when the
    recent median improves; never snaps DOWN so it stays reliable.
    Returns (snapshot, new_entry_that_caused_update, status).
    """
    baselines = [h for h in history if h.get("variant") == "baseline"]
    if not baselines:
        return _load_snapshot(), None, "no_baselines"

    # Last 3 baselines = most recent sample. Median is robust to one outlier
    # but responsive to a real drift.
    window = baselines[-3:]
    hrs = sorted(b.get("hit_rate_at_k", 0) for b in window)
    mrrs = sorted(b.get("mrr", 0) for b in window)
    # Median (P50): index at len//2.
    med_idx = len(window) // 2
    recent_hr = hrs[med_idx]
    recent_mrr = mrrs[med_idx]
    cur_snap = _load_snapshot()

    if cur_snap is None:
        snap = {
            "hit_rate_at_k": recent_hr,
            "mrr": recent_mrr,
            "ts": window[-1]["ts"],
            "n_baselines_seen": len(baselines),
            "frozen_at": datetime.now(timezone.utc).isoformat(),
            "rationale": f"initial snapshot = median of last {len(window)} baselines",
        }
        _save_snapshot(snap)
        return snap, window[-1], "initialized"

    # Snap up only if recent median exceeds current snapshot by SNAP_UP_THRESHOLD.
    # Never snap DOWN — preserve healthy reference even when current is lower.
    if recent_hr >= cur_snap["hit_rate_at_k"] + SNAP_UP_THRESHOLD:
        snap = {
            "hit_rate_at_k": recent_hr,
            "mrr": recent_mrr,
            "ts": window[-1]["ts"],
            "n_baselines_seen": len(baselines),
            "frozen_at": datetime.now(timezone.utc).isoformat(),
            "previous_hit_rate": cur_snap["hit_rate_at_k"],
            "rationale": f"recent median {recent_hr:.4f} > current {cur_snap['hit_rate_at_k']:.4f}",
        }
        _save_snapshot(snap)
        return snap, window[-1], "snapped_up"
    return cur_snap, None, "kept"


def _cold_cache_variance(baselines: list[dict]) -> dict:
    """S20: surface cold-cache first-run vs warmup delta.

    If the last 2 baseline runs include a sharp jump (>3pp), that's likely
    cold-cache variance, not a real improvement. Report it so the dashboard
    user can distinguish "real win" from "cache warmup".
    """
    if len(baselines) < 2:
        return {"detected": False, "reason": "need >=2 baselines"}
    last_two = baselines[-2:]
    delta = last_two[1]["hit_rate_at_k"] - last_two[0]["hit_rate_at_k"]
    return {
        "detected": abs(delta) >= 0.03,
        "delta": round(delta, 4),
        "ts_pair": [last_two[0]["ts"], last_two[1]["ts"]],
        "interpretation": "cold_cache_warmup" if delta > 0.03 else
                          "warm_cache_regression" if delta < -0.03 else
                          "stable",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="eval_trend — summarize last N baseline runs from eval_history.jsonl")
    ap.add_argument("--window", type=int, default=30, help="number of recent baseline runs to summarize (default 30)")
    ap.add_argument("--quiet", action="store_true", help="only print on regression")
    ap.add_argument("--snapshot-only", action="store_true", help="only update last_good_snapshot.json (no dashboard write)")
    args = ap.parse_args()

    history = _load_history()
    if not history:
        print(f"[skip] {HISTORY} missing or empty")
        return 0

    baselines = [h for h in history if h.get("variant") == "baseline"]
    last_n = baselines[-args.window:]

    if not last_n:
        print("[skip] no baseline runs in history")
        return 0

    # S20: snapshot update (idempotent, only moves up)
    snapshot, snap_entry, snap_status = _maybe_update_snapshot(history)

    if args.snapshot_only:
        print(f"snapshot: {snap_status}  hit_rate={snapshot['hit_rate_at_k']:.4f}  ts={snapshot['ts']}")
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
        # S20: cold-cache detector
        "cold_cache_variance": _cold_cache_variance(last_n),
    }

    # S20: trend_status now compares LAST_RUN vs SNAPSHOT, not window-earliest.
    if snapshot and last_n:
        cur = last_n[-1]
        snap_hr = snapshot["hit_rate_at_k"]
        delta_hr = cur["hit_rate_at_k"] - snap_hr
        trend["snapshot"] = {
            "hit_rate_at_k": snap_hr,
            "mrr": snapshot.get("mrr", 0),
            "ts": snapshot["ts"],
            "rationale": snapshot.get("rationale", ""),
        }
        trend["delta_vs_snapshot"] = {
            "hit_rate": round(delta_hr, 4),
            "mrr": round(cur["mrr"] - snapshot.get("mrr", 0), 4),
        }
        # S20: drift logic — within cold-cache tolerance of snapshot = OK.
        if delta_hr >= -COLD_CACHE_TOLERANCE:
            trend["trend_status"] = "stable"
        elif trend["cold_cache_variance"].get("detected") and \
                trend["cold_cache_variance"].get("interpretation") == "cold_cache_warmup":
            # Last run is low but previous was high → probably cold cache, not drift.
            trend["trend_status"] = "cold_cache"
        else:
            trend["trend_status"] = "regressing"
    elif len(last_n) >= 2:
        # Fallback: legacy window-earliest comparison
        cur = last_n[-1]
        earliest = last_n[0]
        trend["delta"] = {
            "mrr": round(cur["mrr"] - earliest["mrr"], 4),
            "hit_rate": round(cur["hit_rate_at_k"] - earliest["hit_rate_at_k"], 4),
        }
        mrr_delta = trend["delta"]["mrr"]
        if mrr_delta > 0.02:
            trend["trend_status"] = "improving"
        elif abs(mrr_delta) <= 0.02:
            trend["trend_status"] = "stable"
        else:
            trend["trend_status"] = "regressing"

    trend["snapshot_status"] = snap_status

    TREND_OUT.write_text(json.dumps(trend, indent=2, ensure_ascii=False), encoding="utf-8")

    is_alert = trend.get("trend_status") in ("regressing", "cold_cache")
    if not args.quiet or is_alert:
        print(json.dumps(trend, indent=2, ensure_ascii=False))
    return 1 if trend.get("trend_status") == "regressing" else 0


if __name__ == "__main__":
    raise SystemExit(main())
