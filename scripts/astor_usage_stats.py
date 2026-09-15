#!/usr/bin/env python3
"""
astor_usage_stats.py — aggregate /v1/read usage over a time window.

v1.14.28 Ship J (2026-09-15): reads astor/metrics/recall_log.jsonl,
groups by (tier, user_id), counts total reads, used_hint/used_filter/
used_time ratios, distinct query hashes, avg top_k. Designed to be
run weekly by hermes cron to decide whether Ship C (entity_lex 3rd
retrieval path) is justified by real-world traffic.

Output:
  astor/metrics/usage_stats_<window>_<ts>.json  — full summary
  astor/metrics/usage_stats_<window>_<ts>_telegram.txt  — short text
       suitable for a Telegram push (max ~4000 chars).
  astor_write via CLI flag --astor-write  — write a fact to bus with
       the weekly summary so future /v1/read can recall "what was
       last week's usage like" (ship-and-forget, low importance).

Usage:
    python scripts/astor_usage_stats.py [--window 7d] [--astor-dir X]
        [--astor-write] [--no-telegram]
    python scripts/astor_usage_stats.py --window 24h  # 1-day snapshot

Default window = 7 days (matches weekly cron cadence).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

_DEFAULT_ASTOR_DIR = 'D:/AI/Astor-Memory-Runtime'
LOG_FILENAME = 'recall_log.jsonl'


def aggregate(log_path: Path, since: datetime, until: datetime) -> dict:
    """Read recall_log.jsonl and aggregate per (tier, user_id)."""
    if not log_path.exists():
        return {'error': 'log_file_missing', 'log_path': str(log_path)}

    per_tier: dict[str, dict] = defaultdict(lambda: {
        'reads': 0,
        'distinct_qhash': set(),
        'top_k_sum': 0,
        'n_results_sum': 0,
        'used_hint': 0,
        'used_filter': 0,
        'used_time': 0,
        'q_len_sum': 0,
    })
    per_user: dict[str, dict] = defaultdict(lambda: {
        'reads': 0, 'tiers': Counter(),
        'used_hint': 0, 'used_filter': 0, 'used_time': 0,
    })
    total = 0
    out_of_window = 0
    malformed = 0

    with open(log_path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            ts_str = e.get('ts', '')
            try:
                ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
            except Exception:
                malformed += 1
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < since or ts >= until:
                out_of_window += 1
                continue
            tier = e.get('tier', 'unknown')
            user = e.get('user_id', 'none')
            t = per_tier[tier]
            t['reads'] += 1
            t['distinct_qhash'].add(e.get('qhash', ''))
            t['top_k_sum'] += int(e.get('top_k', 0))
            t['n_results_sum'] += int(e.get('n_results', 0))
            t['q_len_sum'] += int(e.get('q_len', 0))
            if e.get('used_hint'):
                t['used_hint'] += 1
            if e.get('used_filter'):
                t['used_filter'] += 1
            if e.get('used_time'):
                t['used_time'] += 1

            u = per_user[user]
            u['reads'] += 1
            u['tiers'][tier] += 1
            if e.get('used_hint'):
                u['used_hint'] += 1
            if e.get('used_filter'):
                u['used_filter'] += 1
            if e.get('used_time'):
                u['used_time'] += 1
            total += 1

    # post-process: convert sets to counts, ratios
    for t in per_tier.values():
        reads = t['reads']
        t['distinct_qhash'] = len(t['distinct_qhash'])
        t['avg_top_k'] = round(t['top_k_sum'] / reads, 2) if reads > 0 else 0
        t['avg_n_results'] = round(t['n_results_sum'] / reads, 2) if reads > 0 else 0
        t['avg_q_len'] = round(t['q_len_sum'] / reads, 1) if reads > 0 else 0
        t['hint_ratio'] = round(t['used_hint'] / reads, 4) if reads > 0 else 0
        t['filter_ratio'] = round(t['used_filter'] / reads, 4) if reads > 0 else 0
        t['time_ratio'] = round(t['used_time'] / reads, 4) if reads > 0 else 0
        # Drop sum fields to keep summary compact
        del t['top_k_sum'], t['n_results_sum'], t['q_len_sum'], t['used_hint'], t['used_filter'], t['used_time']

    for u in per_user.values():
        u['tiers'] = dict(u['tiers'])
        reads = u['reads']
        u['hint_ratio'] = round(u['used_hint'] / reads, 4) if reads > 0 else 0
        u['filter_ratio'] = round(u['used_filter'] / reads, 4) if reads > 0 else 0
        u['time_ratio'] = round(u['used_time'] / reads, 4) if reads > 0 else 0
        del u['used_hint'], u['used_filter'], u['used_time']

    return {
        'window_start': since.isoformat(),
        'window_end': until.isoformat(),
        'total_reads_in_window': total,
        'out_of_window': out_of_window,
        'malformed_lines': malformed,
        'per_tier': dict(per_tier),
        'per_user': dict(per_user),
    }


def format_telegram(summary: dict, window_label: str) -> str:
    """Render summary as short Telegram text."""
    lines = [f'astor_usage_stats ({window_label})']
    lines.append(f'window: {summary["window_start"][:10]}..{summary["window_end"][:10]}')
    lines.append(f'total_reads: {summary["total_reads_in_window"]}')
    lines.append('')
    lines.append('per tier:')
    for tier, t in sorted(summary['per_tier'].items(), key=lambda kv: -kv[1]['reads']):
        lines.append(
            f'  {tier:8s} reads={t["reads"]:4d} '
            f'distinct_q={t["distinct_qhash"]:3d} '
            f'avg_k={t["avg_top_k"]:.1f} '
            f'hint={t["hint_ratio"]:.1%} '
            f'filter={t["filter_ratio"]:.1%} '
            f'time={t["time_ratio"]:.1%}'
        )
    lines.append('')
    lines.append('per user:')
    for user, u in sorted(summary['per_user'].items(), key=lambda kv: -kv[1]['reads'])[:5]:
        tiers_str = ','.join(f'{k}={v}' for k, v in u['tiers'].items())
        lines.append(
            f'  {user[:12]:12s} reads={u["reads"]:4d} '
            f'hint={u["hint_ratio"]:.1%} '
            f'filter={u["filter_ratio"]:.1%} '
            f'time={u["time_ratio"]:.1%} [{tiers_str}]'
        )
    # Recommendation
    lines.append('')
    total_reads = summary['total_reads_in_window']
    if total_reads == 0:
        rec = 'NO DATA. Server not yet seen any /v1/read in window.'
    else:
        # Weighted hint/filter/time ratio across tiers
        total_weighted = 0
        hint_w = filter_w = time_w = 0
        for t in summary['per_tier'].values():
            r = t['reads']
            total_weighted += r
            hint_w += r * t['hint_ratio']
            filter_w += r * t['filter_ratio']
            time_w += r * t['time_ratio']
        if total_weighted > 0:
            avg_hint = hint_w / total_weighted
            avg_filter = filter_w / total_weighted
            avg_time = time_w / total_weighted
        else:
            avg_hint = avg_filter = avg_time = 0
        lines.append(
            f'avg_ratios: hint={avg_hint:.1%} filter={avg_filter:.1%} time={avg_time:.1%}'
        )
        if avg_hint + avg_filter + avg_time >= 0.10:
            rec = (f'Ship C (entity_lex 3rd retrieval path) is JUSTIFIED — '
                   f'{total_reads} reads/week with {(avg_hint + avg_filter + avg_time):.1%} '
                   f'combined usage of RippleMem params.')
        else:
            rec = (f'Ship C NOT yet justified — only '
                   f'{(avg_hint + avg_filter + avg_time):.1%} param usage. '
                   f'Wait for more traffic or re-evaluate.')
        lines.append('')
        lines.append(f'RECOMMENDATION: {rec}')

    return '\n'.join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--astor-dir', default=_DEFAULT_ASTOR_DIR,
                   help='ASTOR_DIR path (default %(default)s)')
    p.add_argument('--window', default='7d',
                   help='Time window: 7d / 24h / Nd (default %(default)s)')
    p.add_argument('--astor-write', action='store_true',
                   help='Also write the summary as a fact via /v1/write (admin tier).')
    p.add_argument('--no-telegram', action='store_true',
                   help='Skip writing the telegram-format file (debug only).')
    args = p.parse_args()

    astor_dir = Path(args.astor_dir)
    log_path = astor_dir / 'astor' / 'metrics' / LOG_FILENAME

    # Parse window
    win_str = args.window.strip().lower()
    if win_str.endswith('d'):
        try:
            days = int(win_str[:-1])
        except ValueError:
            print(f'bad window: {args.window}', file=sys.stderr)
            return 2
        window_label = f'{days}d'
    elif win_str.endswith('h'):
        try:
            hours = int(win_str[:-1])
        except ValueError:
            print(f'bad window: {args.window}', file=sys.stderr)
            return 2
        days = hours / 24
        window_label = f'{hours}h'
    else:
        print(f'bad window: {args.window}', file=sys.stderr)
        return 2

    until = datetime.now(timezone.utc)
    since = until - timedelta(days=days)

    summary = aggregate(log_path, since, until)
    summary['window_label'] = window_label
    summary['astor_dir'] = str(astor_dir)
    summary['log_path'] = str(log_path)
    summary['generated_at'] = datetime.now(timezone.utc).isoformat()

    # Output paths
    metrics_dir = astor_dir / 'astor' / 'metrics'
    metrics_dir.mkdir(parents=True, exist_ok=True)
    ts_str = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    json_path = metrics_dir / f'usage_stats_{window_label}_{ts_str}.json'
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    tg_text = format_telegram(summary, window_label)
    if not args.no_telegram:
        tg_path = metrics_dir / f'usage_stats_{window_label}_{ts_str}_telegram.txt'
        with open(tg_path, 'w', encoding='utf-8') as f:
            f.write(tg_text)

    # Stdout
    print(f'summary: {json_path}')
    print(f'telegram: {tg_path if not args.no_telegram else "(skipped)"}')
    print('---')
    print(tg_text)

    # Optional astor_write
    if args.astor_write:
        try:
            import urllib.request
            text = (
                f'astor_usage_stats {window_label}: {summary["total_reads_in_window"]} reads '
                f'in {since.date()}..{until.date()}. '
                + tg_text.replace('\n', ' | ')
            )
            body = json.dumps({
                'text': text[:500],
                'user': 'admin',
                'tier': 'private',
                'kind': 'usage_stats',
                'importance': 0.5,
            }).encode('utf-8')
            req = urllib.request.Request(
                'http://127.0.0.1:7803/v1/write',
                data=body, method='POST',
                headers={'Content-Type': 'application/json'},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                print(f'\nastor_write: {r.status} {json.loads(r.read())["fact_ids"]}')
        except Exception as e:
            print(f'\nastor_write failed: {e}', file=sys.stderr)

    return 0


if __name__ == '__main__':
    sys.exit(main())