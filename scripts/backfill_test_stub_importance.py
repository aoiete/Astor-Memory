"""backfill_test_stub_importance.py — S29 (2026-09-30) one-shot utility.

Why: 3 facts (12597/12598/12599) from a 2026-09-16 platform-test sweep
(origin_session_id = discord-test-session-20260916 / telegram-test-session-20260916 /
weixin-test-session-20260916) were captured with high importance (0.85/0.9).

Effect: they pollute success_pattern/failure_pattern recall with fake
"user voice" content that wasn't actually said by any real user.

Fix: tombstone the 3 test stubs. Keeps audit trail (tombstoned_at +
reason), reduces noise without losing history.

Run once: `python backfill_test_stub_importance.py [--execute]`
Default: dry-run. --execute to actually write.

v1.15.55 S29 (2026-09-30).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ASTOR_DIR = Path(os.environ.get('ASTOR_DIR', r'D:\AI\Astor-Memory-Runtime'))


def find_test_stub_facts(admin_db: Path) -> list[dict]:
    conn = sqlite3.connect(str(admin_db))
    cur = conn.cursor()
    cur.execute(
        "SELECT id, kind, importance, origin_session_id, substr(content, 1, 80) "
        "FROM memory_canonical "
        "WHERE origin_session_id LIKE '%test-session-20260916%' "
        "AND tombstoned = 0"
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {
            'fact_id': r[0], 'kind': r[1], 'importance': r[2],
            'origin_session_id': r[3], 'preview': r[4],
        } for r in rows
    ]


def tombstone(admin_db: Path, fact_ids: list[int]) -> dict:
    conn = sqlite3.connect(str(admin_db))
    cur = conn.cursor()
    ts = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    with conn:
        for fid in fact_ids:
            cur.execute(
                "UPDATE memory_canonical "
                "SET tombstoned = 1, tombstoned_at = ?, importance = 0.1, "
                "    metadata = json_set(COALESCE(metadata, '{}'), '$.tombstone_reason', 'S29 test stub cleanup 2026-09-30') "
                "WHERE id = ? AND tombstoned = 0",
                (ts, fid),
            )
    conn.close()
    return {'tombstoned': len(fact_ids)}


def main():
    ap = argparse.ArgumentParser(description='S29 tombstone 2026-09-16 platform-test stubs')
    ap.add_argument('--execute', action='store_true', default=False)
    args = ap.parse_args()
    admin_db = ASTOR_DIR / 'users/admin/memory/astor_bus_admin.db'
    if not admin_db.exists():
        print(json.dumps({'error': 'admin bus DB not found', 'path': str(admin_db)}, indent=2))
        return
    stubs = find_test_stub_facts(admin_db)
    out = {
        'dry_run': not args.execute,
        'astor_dir': str(ASTOR_DIR),
        'admin_db': str(admin_db),
        'candidates': stubs,
        'n_candidates': len(stubs),
        'ts': datetime.now(timezone.utc).isoformat(),
    }
    if args.execute and stubs:
        out['result'] = tombstone(admin_db, [s['fact_id'] for s in stubs])
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
