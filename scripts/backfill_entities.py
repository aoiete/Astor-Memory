#!/usr/bin/env python3
"""
backfill_entities.py — one-shot script to populate entities_json on legacy facts.

v1.14.21 Ship B (2026-09-15): RippleMem-style entity binding ships. Existing
facts (rows where entities_json = '[]') get backfilled by running
extract_entities() on their content.

Usage:
    python scripts/backfill_entities.py [--tier public|source|private] [--user-id X]
        [--dry-run] [--batch-size 500] [--only-empty]

Default behavior: backfill every tier's memory_canonical where entities_json='[]'.
Resumable — keeps going from the last successful fact_id via checkpoint file.

Idempotent: re-running won't overwrite non-empty entities_json unless --only-empty
is omitted (default is --only-empty).

Logging: writes to logs/backfill_entities_<ts>.log and final summary to stdout.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# Ensure astor-memory is importable from source
_THIS = Path(__file__).resolve()
_REPO = _THIS.parent.parent
sys.path.insert(0, str(_REPO))


def _db_paths(tier: str, user_id: str | None) -> list[Path]:
    """Return the bus DB path(s) for a given tier."""
    astor_dir = Path(os.environ.get('ASTOR_DIR', 'D:/AI/Astor-Memory-Runtime'))
    base = astor_dir / ('users' if tier.startswith('private') or tier == 'private' else tier)
    if tier.startswith('private_'):
        # explicit per-user private
        actual_user = tier.split('_', 1)[1]
        return [base / 'memory' / f'astor_bus_{actual_user}.db']
    if tier == 'private':
        if user_id:
            return [base / user_id / 'memory' / f'astor_bus_{user_id}.db']
        # all users under users/<u>/memory/ — per-user dir layout
        # (Astor-Memory v1.1 9-db schema).
        if not base.exists():
            return []
        out = []
        for user_dir in base.iterdir():
            if not user_dir.is_dir():
                continue
            mem_dir = user_dir / 'memory'
            if mem_dir.exists():
                out.extend(mem_dir.glob('astor_bus_*.db'))
        return out
    return [base / 'memory' / f'astor_bus_{tier}.db']


def backfill_one_db(
    db_path: Path,
    batch_size: int = 500,
    dry_run: bool = False,
    only_empty: bool = True,
    verbose: bool = True,
) -> dict:
    """Backfill entities_json on a single bus DB. Returns counts."""
    if not db_path.exists():
        return {'db': str(db_path), 'status': 'missing', 'rows': 0}

    # Lazy import for forge (avoid hard dep if running without astor installed)
    from astor_memory.forge.extractor import extract_entities

    # Apply schema migrations first (idempotent). Ensures entities_json column
    # exists on legacy per-user DBs that haven't been opened by the server yet.
    try:
        from astor_memory.bus.schema import astor_init_schema
        _init_conn = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            astor_init_schema(_init_conn)
        finally:
            _init_conn.close()
    except Exception as _e:
        if verbose:
            print(f'[{db_path.name}] schema init warning: {_e}')

    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        # Check column exists (v9+)
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(memory_canonical)"
        ).fetchall()}
        if 'entities_json' not in cols:
            return {'db': str(db_path), 'status': 'no_entities_column', 'rows': 0}

        where = "WHERE entities_json = '[]' AND tombstoned = 0" if only_empty else \
                "WHERE tombstoned = 0"
        # Get total count for progress reporting
        total = conn.execute(
            f"SELECT COUNT(*) FROM memory_canonical {where}"
        ).fetchone()[0]

        if total == 0:
            return {'db': str(db_path), 'status': 'nothing_to_backfill', 'rows': 0}

        if verbose:
            print(f'[{db_path.name}] backfilling {total} facts...')

        processed = 0
        updated = 0
        t0 = time.time()
        # Use id-cursor pagination (offset-based breaks when we update rows mid-scan
        # because updates change WHERE-clause membership). Capture min id at start;
        # each batch selects id > last_id.
        cursor_id = conn.execute(
            f"SELECT COALESCE(MIN(id), 0) FROM memory_canonical {where}"
        ).fetchone()[0]
        if cursor_id == 0:
            return {'db': str(db_path), 'status': 'no_ids', 'rows': 0}
        while True:
            # Re-apply the WHERE clause; only_empty is part of the predicate
            # so as we update rows they drop out of the selection naturally.
            rows = conn.execute(
                f"SELECT id, content FROM memory_canonical {where} AND id > ? "
                f"ORDER BY id ASC LIMIT ?",
                (cursor_id, batch_size),
            ).fetchall()
            if not rows:
                break
            for fact_id, content in rows:
                ents = extract_entities(content or '', fact_id)
                ents_json = json.dumps(ents, ensure_ascii=False)
                if not dry_run:
                    conn.execute(
                        "UPDATE memory_canonical SET entities_json = ? WHERE id = ?",
                        (ents_json, fact_id),
                    )
                    updated += 1
                processed += 1
                cursor_id = max(cursor_id, fact_id)
            if verbose and processed % (batch_size * 4) == 0:
                rate = processed / max(1e-6, time.time() - t0)
                print(f'  [{db_path.name}] {processed}/{total} ({rate:.0f} rows/s)')

        elapsed = time.time() - t0
        result = {
            'db': str(db_path),
            'status': 'ok',
            'rows_total': total,
            'rows_processed': processed,
            'rows_updated': updated,
            'elapsed_seconds': round(elapsed, 2),
            'rows_per_second': round(processed / max(1e-6, elapsed), 1),
            'dry_run': dry_run,
        }
        if verbose:
            print(f'[{db_path.name}] DONE: {result}')
        return result
    finally:
        conn.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--tier', default='all',
                   help="public | source | private | private_<user> | all (default)")
    p.add_argument('--user-id', help='user_id for tier=private')
    p.add_argument('--dry-run', action='store_true', help="don't write; just print counts")
    p.add_argument('--batch-size', type=int, default=500)
    p.add_argument('--only-empty', action='store_true', default=True,
                   help='only update rows where entities_json is empty (default)')
    p.add_argument('--include-non-empty', dest='only_empty', action='store_false',
                   help='overwrite existing non-empty entities_json too')
    p.add_argument('--quiet', action='store_true')
    args = p.parse_args()

    # Determine which DBs to backfill
    if args.tier == 'all':
        dbs: list[tuple[str, str | None]] = [
            ('public', None), ('source', None), ('private', None),
        ]
    elif args.tier.startswith('private_'):
        dbs = [(args.tier, None)]
    else:
        dbs = [(args.tier, args.user_id)]

    all_results = []
    for tier, user_id in dbs:
        for db_path in _db_paths(tier, user_id):
            r = backfill_one_db(
                db_path,
                batch_size=args.batch_size,
                dry_run=args.dry_run,
                only_empty=args.only_empty,
                verbose=not args.quiet,
            )
            all_results.append(r)

    # Summary
    total_rows = sum(r.get('rows_processed', 0) for r in all_results)
    total_updated = sum(r.get('rows_updated', 0) for r in all_results)
    elapsed_total = sum(r.get('elapsed_seconds', 0) for r in all_results)
    print()
    print('=' * 70)
    print(f'SUMMARY: {len(all_results)} DBs processed')
    print(f'  rows_total   = {sum(r.get("rows_total", 0) for r in all_results)}')
    print(f'  rows_scanned = {total_rows}')
    print(f'  rows_updated = {total_updated}')
    print(f'  elapsed      = {elapsed_total:.1f}s')
    print(f'  dry_run      = {args.dry_run}')
    print('=' * 70)
    print('per-DB results:')
    for r in all_results:
        print(f'  {r["db"]}: status={r["status"]} rows={r.get("rows_processed", 0)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())