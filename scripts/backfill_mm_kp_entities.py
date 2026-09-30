"""backfill_mm_kp_entities.py — S27 (2026-09-30) one-shot utility.

Why: 10 source-tier facts (5 mental_models + 5 knowledge_pages written by
S24/S25 auto-promote crons) have empty entities_json. The crons bypass the
forge/extractor pipeline so no entities were ever extracted.

Effect: dashboard entities_coverage.source = 0.75 instead of 1.0.

Fix: backfill entities_json from each fact's `metadata` field:
  - `parent_fact_ids` → ["evidence:504", "evidence:2109", ...]
  - `__keywords__`    → ["keyword:astor", "keyword:architecture", ...]
  - `slug` (KP)       → ["slug:astor-architecture"]
  - `question` (MM)   → ["question:<slugified question>"]

This restores source-tier entity coverage to ~1.0 without re-running
the auto-promote cron.

Run once: `python backfill_mm_kp_entities.py [--execute]`
Default: dry-run. --execute to actually write.

v1.15.53 S27 (2026-09-30).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ASTOR_DIR = Path(os.environ.get('ASTOR_DIR', r'D:\AI\Astor-Memory-Runtime'))


def _slugify(s: str) -> str:
    s = re.sub(r'[^a-zA-Z0-9]+', '-', s).strip('-').lower()
    return s[:80]


def _extract_entities(metadata_json: str, kind: str) -> list[str]:
    """Build entities_json from a fact's stored metadata."""
    if not metadata_json or metadata_json == '{}':
        return []
    try:
        m = json.loads(metadata_json)
    except (ValueError, TypeError):
        return []
    entities: list[str] = []
    # Evidence refs from parent_fact_ids (most valuable for recall)
    for pid in m.get('parent_fact_ids', []):
        entities.append(f'evidence:{int(pid)}')
    # Keywords
    for kw in m.get('__keywords__', []):
        if isinstance(kw, str):
            entities.append(f'keyword:{kw}')
    # KP slug
    if kind == 'knowledge_page' and m.get('slug'):
        entities.append(f'slug:{m["slug"]}')
    # MM question slug
    if kind == 'mental_model' and m.get('question'):
        entities.append(f'question:{_slugify(m["question"])}')
    # Dedup + cap
    seen = set()
    out = []
    for e in entities:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out[:32]


def backfill_one(db_path: Path, kind_filter: tuple, execute: bool) -> dict:
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        f"SELECT id, kind, metadata FROM memory_canonical "
        f"WHERE kind IN ({','.join('?' * len(kind_filter))}) "
        f"AND tombstoned = 0 AND (entities_json IS NULL OR entities_json = '' OR entities_json = '[]')",
        kind_filter,
    )
    rows = cur.fetchall()
    candidates = []
    for fid, kind, meta in rows:
        ents = _extract_entities(meta or '', kind)
        if ents:
            candidates.append((fid, ents))
    if not execute:
        conn.close()
        return {
            'candidates': len(candidates),
            'sample': candidates[:2],
            'action': 'preview',
        }
    with conn:
        for fid, ents in candidates:
            cur.execute(
                "UPDATE memory_canonical SET entities_json = ? "
                "WHERE id = ? AND (entities_json IS NULL OR entities_json = '' OR entities_json = '[]')",
                (json.dumps(ents, ensure_ascii=False), fid),
            )
    conn.close()
    return {
        'updated': len(candidates),
        'action': 'upserted',
    }


def main():
    ap = argparse.ArgumentParser(description='S27 backfill entities_json for source-tier MM/KP')
    ap.add_argument('--execute', action='store_true', default=False,
                    help='Actually write (default: dry-run)')
    args = ap.parse_args()
    src_db = ASTOR_DIR / 'source' / 'memory' / 'astor_bus_source.db'
    if not src_db.exists():
        print(json.dumps({'error': 'source bus DB not found', 'path': str(src_db)}, indent=2))
        return
    r = backfill_one(src_db, ('mental_model', 'knowledge_page'), args.execute)
    summary = {
        'dry_run': not args.execute,
        'astor_dir': str(ASTOR_DIR),
        'source_db': str(src_db),
        'result': r,
        'ts': datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
