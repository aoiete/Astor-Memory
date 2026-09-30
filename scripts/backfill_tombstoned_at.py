"""backfill_tombstoned_at.py — S26 (2026-09-30) one-shot utility.

Why: 14688 tombstones across all tiers lack `tombstoned_at` timestamp.
Root cause: pre-feature tombstones (before v1.14.16 added tombstoned_at
column) + maker_pocket._record_close() tombstones without timestamp
(shipped as bug fix in v0.62.5+).

Effect: `decayed_count.no_timestamp: 14688` shows 96% of tombstones can't be
aged. staleness/decay logic skips them (NULL → no age). Dashboard
"decayed_count.recent_30d vs old" splits are inaccurate.

Fix: backfill `tombstoned_at` from `promoted_at` for any row where it's
NULL. promoted_at is the closest proxy (we know the fact existed at least
that long). It's an approximation, but the alternative (leave NULL) means
the dashboard can't age-decay them at all.

Run once: `python backfill_tombstoned_at.py [--execute]`
Default: dry-run. --execute to actually write.

v1.15.52 S26 (2026-09-30).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ASTOR_DIR = Path(os.environ.get('ASTOR_DIR', r'D:\AI\Astor-Memory-Runtime'))


def _collect_bus_dbs(astor_dir: Path) -> list[tuple[str, Path]]:
    """Return (label, db_path) for every tier's bus DB."""
    out: list[tuple[str, Path]] = []
    for tier in ('public', 'source'):
        p = astor_dir / tier / 'memory' / f'astor_bus_{tier}.db'
        if p.exists():
            out.append((tier, p))
    users = astor_dir / 'users'
    if users.exists():
        for u in users.iterdir():
            if u.is_dir():
                p = u / 'memory' / f'astor_bus_{u.name}.db'
                if p.exists():
                    out.append((f'private:{u.name}', p))
    return out


def backfill(db_path: Path, execute: bool) -> dict:
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    # Find rows where tombstoned=1 AND tombstoned_at IS NULL.
    # Backfill from promoted_at (or created_at as fallback).
    cur.execute(
        "SELECT id, promoted_at, created_at FROM memory_canonical "
        "WHERE tombstoned = 1 AND (tombstoned_at IS NULL OR tombstoned_at = '')"
    )
    rows = cur.fetchall()
    candidates = []
    for fid, pa, ca in rows:
        ts = pa or ca or ''
        if not ts:
            continue  # truly no timestamp anywhere, skip
        candidates.append((fid, ts))
    if not execute:
        conn.close()
        return {
            'candidates': len(candidates),
            'sample_ts': candidates[:3] if candidates else [],
            'action': 'preview',
        }
    # Execute: UPDATE with promoted_at (or created_at) as tombstoned_at
    with conn:
        for fid, ts in candidates:
            cur.execute(
                "UPDATE memory_canonical SET tombstoned_at = ? "
                "WHERE id = ? AND (tombstoned_at IS NULL OR tombstoned_at = '')",
                (ts, fid),
            )
    conn.close()
    return {
        'updated': len(candidates),
        'action': 'upserted',
    }


def main():
    ap = argparse.ArgumentParser(description='S26 backfill tombstoned_at from promoted_at')
    ap.add_argument('--execute', action='store_true', default=False,
                    help='Actually write (default: dry-run)')
    args = ap.parse_args()
    dbs = _collect_bus_dbs(ASTOR_DIR)
    results = {}
    total = 0
    for label, p in dbs:
        r = backfill(p, args.execute)
        results[label] = r
        n = r.get('candidates', r.get('updated', 0))
        total += n
    summary = {
        'dry_run': not args.execute,
        'astor_dir': str(ASTOR_DIR),
        'total_candidates': total,
        'tiers': results,
        'ts': datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
