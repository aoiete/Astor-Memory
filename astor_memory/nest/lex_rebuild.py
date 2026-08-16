"""One-shot backfill: index every fact in bus DB into the lex index.

Usage:
    python -m astor_memory.nest.lex_rebuild --tier public
    python -m astor_memory.nest.lex_rebuild --tier source
    python -m astor_memory.nest.lex_rebuild --tier private --user admin
    python -m astor_memory.nest.lex_rebuild --tier all

Idempotent: re-running drops the index and rebuilds. Drops are per-tier.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from .lex_index import astor_lex, _lex_db_path
from .._internal.acl_layout import get_db_path, list_user_ids, list_repo_ids


def _bus_db_path(tier: str, user_id: str | None) -> Path:
    """Resolve canonical facts DB path via acl_layout (single source of truth)."""
    return get_db_path(tier, 'bus', user_id)


def rebuild(tier: str, user_id: str | None = None, drop: bool = True) -> dict:
    bus_path = _bus_db_path(tier, user_id)
    if not bus_path.exists():
        return {'tier': tier, 'user_id': user_id, 'skipped': 'no bus db'}

    # Drop and re-create lex db if requested.
    # IMPORTANT: the running astor server may hold the lex DB file open.
    # `unlink()` would raise PermissionError on Windows. So when drop=True,
    # we try unlink first (offline case), and on failure fall back to
    # truncate-in-place (online case). The truncate pattern uses the
    # existing AstorLex singleton (if any) and DELETEs every row, then
    # VACUUMs to reclaim space.
    lex_path = _lex_db_path(tier, user_id)
    if drop and lex_path.exists():
        try:
            lex_path.unlink()
            for ext in ('-wal', '-shm'):
                p = lex_path.with_name(lex_path.name + ext)
                if p.exists():
                    try:
                        p.unlink()
                    except OSError:
                        pass
        except OSError:
            # Server is holding the file. In-place truncate instead.
            from .lex_index import (
                _LEX_SINGLETONS_LOCK, _LEX_SINGLETONS,
            )
            with _LEX_SINGLETONS_LOCK:
                live = _LEX_SINGLETONS.get((tier, user_id))
            if live is not None:
                with live._lock:
                    live._conn.executescript('''
                        DELETE FROM documents;
                        DELETE FROM terms;
                        DELETE FROM postings;
                        DELETE FROM meta;
                        INSERT INTO meta(key,value) VALUES ('schema_version','1');
                        INSERT INTO meta(key,value) VALUES ('total_docs','0');
                        INSERT INTO meta(key,value) VALUES ('avgdl','0');
                    ''')
                    live._conn.commit()
            # Best-effort VACUUM (may also fail under exclusive lock; ignore)
            try:
                if live is not None:
                    live._conn.execute('VACUUM')
            except Exception:
                pass

    lex = astor_lex(tier=tier, user_id=user_id)
    conn = sqlite3.connect(str(bus_path))
    rows = conn.execute(
        'SELECT id, content FROM memory_canonical '
        'WHERE tombstoned = 0 OR tombstoned IS NULL'
    ).fetchall()
    conn.close()
    n = 0
    for fid, content in rows:
        lex.index_fact(int(fid), str(content))
        n += 1
    return {
        'tier': tier, 'user_id': user_id,
        'indexed_docs': n,
        'lex_path': str(lex_path),
        'stats': lex.stats(),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description='Rebuild lex inverted index from bus')
    ap.add_argument('--tier', default='public',
                    choices=['public', 'source', 'private', 'repo', 'all'])
    ap.add_argument('--user', help='user_id for private tier')
    ap.add_argument('--repo', help='repo_id for repo tier')
    ap.add_argument('--no-drop', action='store_true',
                    help='do not drop existing lex DB (append-only)')
    args = ap.parse_args()

    results = []
    drop = not args.no_drop

    if args.tier == 'all':
        results.append(rebuild('public', None, drop))
        results.append(rebuild('source', None, drop))
        for u in list_user_ids():
            results.append(rebuild('private', u, drop))
        for r in list_repo_ids():
            results.append(rebuild('repo', r, drop))
    elif args.tier == 'private':
        if not args.user:
            print('private tier needs --user', file=sys.stderr)
            return 2
        results.append(rebuild('private', args.user, drop))
    elif args.tier == 'repo':
        if not args.repo:
            print('repo tier needs --repo', file=sys.stderr)
            return 2
        results.append(rebuild('repo', args.repo, drop))
    else:
        results.append(rebuild(args.tier, None, drop))

    import json as _json
    print(_json.dumps(results, indent=2, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
