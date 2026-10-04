"""
astor_self_eval.py — SimpleMem-inspired mini-eval.

Reads ASTOR_DIR/astor/metrics/recall_log.jsonl and computes per-intent
hit rate (top-1 score > 0.5 = hit). Lets the operator see which query
intents the memory system is serving well vs poorly, without
running a full eval set.

Usage:
    python scripts/astor_self_eval.py
    python scripts/astor_self_eval.py --window 7  # last 7 days
    python scripts/astor_self_eval.py --threshold 0.7  # higher bar for "hit"

v1.16.61 — 2026-10-03.

Design notes:
- Hit threshold default 0.5. The recall_log.jsonl top_hit_score is
  the hybrid score from the BM25+vector fusion (range 0-1 in
  practice). 0.5 is empirically a "useful hit" boundary for admin
  corpus (5K+ facts).
- "Empty recall" (n_results=0) is its own bucket, not a hit.
- Output is plain text so the operator can pipe to grep / sort.

Why this matters:
  The SimpleMem paper (arxiv:2601.02553) shows that intent-aware
  retrieval planning is the third pillar of good memory. Before we
  can rerank by intent, we need a measurement: which intents are
  well-served today? This script is the cheap pre-step.

No API key, no DB connection. Pure file reader. Safe to schedule.
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_ASTOR_DIR = Path(r'D:\AI\Astor-Memory-Runtime')
DEFAULT_WINDOW_DAYS = 1
DEFAULT_HIT_THRESHOLD = 0.5


def astor_dir() -> Path:
    return Path(os.environ.get('ASTOR_DIR', str(DEFAULT_ASTOR_DIR)))


def collect_log_paths(base: Path) -> list[Path]:
    """Find recall_log.jsonl under any of the 3 tiers + users/."""
    candidates = []
    # astor-write stores recalls in <ASTOR_DIR>/astor/metrics/recall_log.jsonl
    # (single shared metrics dir, not per-tier). New installs after
    # v1.16.61 also write `intent` to this file.
    candidates.append(base / 'astor' / 'metrics' / 'recall_log.jsonl')
    # Legacy path: per-tier recall_log under <tier>/memory/astor/metrics/.
    # Older ships may have written here; still checked for backward compat.
    for tier in ('public', 'private', 'source'):
        candidates.append(base / tier / 'memory' / 'astor' / 'metrics' / 'recall_log.jsonl')
    users_dir = base / 'users'
    if users_dir.exists():
        for user_dir in users_dir.iterdir():
            if user_dir.is_dir():
                candidates.append(user_dir / 'memory' / 'astor' / 'metrics' / 'recall_log.jsonl')
                candidates.append(user_dir / 'astor' / 'metrics' / 'recall_log.jsonl')
    return [p for p in candidates if p.exists()]


def parse_window(window_days: int) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    return now - timedelta(days=window_days), now


def read_events(log_paths: list[Path], since: datetime) -> list[dict]:
    out = []
    for path in log_paths:
        try:
            with path.open('r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = ev.get('ts')
                    if not ts:
                        continue
                    try:
                        ev_dt = datetime.fromisoformat(ts)
                    except ValueError:
                        continue
                    if ev_dt >= since:
                        out.append(ev)
        except OSError:
            continue
    return out


def compute(intent_counts: Counter, top_score_counter: Counter,
            n_results_counter: Counter, intent_n_results_sum: dict,
            threshold: float) -> tuple[list, list, int]:
    """Compute per-intent hit rate and top-hit avg. Returns (rows, totals_rows, total_n)."""
    rows = []
    for intent in sorted(intent_counts.keys()):
        n_events = intent_counts[intent]
        n_top_ge_t = top_score_counter[(intent, True)]
        n_top_lt_t = top_score_counter[(intent, False)]
        n_empty = intent_n_results_sum.get((intent, 0), 0)
        n_has_hit = n_top_ge_t
        hit_rate = n_has_hit / n_events if n_events else 0
        avg_top = sum(s for (i, _), s in top_score_counter.items() if i == intent) / max(n_events - n_empty, 1) if n_events else 0
        rows.append((intent, n_events, n_has_hit, n_empty, hit_rate, avg_top))
    # totals
    total_events = sum(intent_counts.values())
    total_has_hit = sum(v for (i, t), v in top_score_counter.items() if t)
    total_rate = total_has_hit / total_events if total_events else 0
    return rows, [(total_events, total_has_hit, total_rate)], total_events


def format_table(rows: list, totals: list, total_n: int) -> str:
    out = []
    out.append(f'  total events: {total_n}')
    out.append('')
    out.append(f"  {'intent':<12} {'n':>5} {'hits':>5} {'empty':>6} {'hit_rate':>9} {'avg_top':>9}")
    out.append(f"  {'-'*12} {'-'*5} {'-'*5} {'-'*6} {'-'*9} {'-'*9}")
    for intent, n_events, hits, ev_empty, rate, avg_top in rows:
        out.append(f'  {intent:<12} {n_events:>5d} {hits:>5d} {ev_empty:>6d} {rate*100:>8.1f}% {avg_top:>9.3f}')
    out.append('')
    n_events, hits, rate = totals[0]
    out.append(f'  {"TOTAL":<12} {n_events:>5d} {hits:>5d} {"-":>6} {rate*100:>8.1f}% {"-":>9}')
    return '\n'.join(out)


def main() -> int:
    ap = argparse.ArgumentParser(prog='astor-self-eval')
    ap.add_argument('--window', type=int, default=DEFAULT_WINDOW_DAYS,
                    help=f'days to look back (default {DEFAULT_WINDOW_DAYS})')
    ap.add_argument('--threshold', type=float, default=DEFAULT_HIT_THRESHOLD,
                    help=f'min top_hit_score to count as hit (default {DEFAULT_HIT_THRESHOLD})')
    ap.add_argument('--limit', type=int, default=0, help='stop after N events (debug)')
    args = ap.parse_args()

    base = astor_dir()
    paths = collect_log_paths(base)
    if not paths:
        print(f'no recall_log.jsonl under {base}/. Run some /v1/read calls first.')
        return 0
    since, until = parse_window(args.window)
    events = read_events(paths, since)
    if args.limit:
        events = events[-args.limit:]
    if not events:
        print(f'no events in last {args.window}d ({len(paths)} log files scanned)')
        return 0

    intent_counts = Counter()
    top_score_counter = Counter()
    n_results_counter = Counter()
    intent_n_results_sum = defaultdict(int)
    for ev in events:
        intent = ev.get('intent') or 'factual'
        intent_counts[intent] += 1
        n_results = int(ev.get('n_results') or 0)
        if n_results == 0:
            intent_n_results_sum[(intent, 0)] += 1
        else:
            top = float(ev.get('top_hit_score') or 0.0)
            top_score_counter[(intent, top >= args.threshold)] += 1
            intent_n_results_sum[(intent, 1)] += 1
        n_results_counter[n_results] += 1

    rows, totals, total_n = compute(intent_counts, top_score_counter,
                                     n_results_counter, intent_n_results_sum,
                                     args.threshold)
    print(f'astor-self-eval — last {args.window}d (>= {since.isoformat()})')
    print(f'  threshold: top_hit_score >= {args.threshold}')
    print(f'  log files: {len(paths)}')
    print()
    print(format_table(rows, totals, total_n))
    return 0


if __name__ == '__main__':
    sys.exit(main() or 0)