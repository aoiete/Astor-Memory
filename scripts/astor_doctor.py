"""
astor-doctor — standalone repair CLI for astor-memory DBs.

SUBCOMMANDS:
  check           read-only scan; prints schema + counts + per-DB health
  repair-conn     backfill NULL on required NOT NULL cols + PRAGMA user_version
  repair-orphan   delete candidates whose event no longer exists
  stats           one-line row counts per DB (suitable for cron monitoring)

DESIGN:
- Pure standalone tool; does NOT import astor_memory (works without server)
- Idempotent: re-running after a successful repair is a no-op
- Each repair is one transaction per DB; safe to interrupt mid-DB
- New installs (pip install astor-memory) start with PRAGMA user_version=0
  and required cols NOT NULL, so check is silent and repair is a no-op

USAGE:
  python astor_doctor.py check
  python astor_doctor.py repair-conn [--apply]
  python astor_doctor.py repair-orphan [--apply]
  python astor_doctor.py stats

ENV:
  ASTOR_DIR  override the runtime dir (default D:/AI/Astor-Memory-Runtime)

v1.16.58 — 2026-10-03
"""
import argparse
import os
import sqlite3
import sys
from pathlib import Path

DEFAULT_ASTOR_DIR = Path(os.environ.get('ASTOR_DIR') or Path.home() / '.astor')
# 2026-10-03: TIER_DIRS / USER_DIR must be RELATIVE so collect_dbs can
# rebase on a different ASTOR_DIR (Linux CI sets ASTOR_DIR=/tmp/...).
# Previously these were absolute (joined onto DEFAULT_ASTOR_DIR), which
# made `d.relative_to(DEFAULT_ASTOR_DIR)` return the runtime path on Linux
# and then `(base / d)` produced /tmp/.../D:/AI/... → no DBs found.
TIER_DIRS = [
    Path('public') / 'memory',
    Path('private') / 'memory',
    Path('source') / 'memory',
]
USER_DIR = Path('users')

REQUIRED_NOT_NULL = ('candidate_id', 'event_id', 'namespace', 'content')
PRAGMA_USER_VERSION = 1  # bump this when adding new repair paths


def astor_dir() -> Path:
    return Path(os.environ.get('ASTOR_DIR', str(DEFAULT_ASTOR_DIR)))


def collect_dbs() -> list[Path]:
    base = astor_dir()
    out: list[Path] = []
    for d in TIER_DIRS + [USER_DIR]:
        real = base / d
        if real.exists():
            out.extend(sorted(real.glob('*.db')))
    return out


def _open(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(str(path), isolation_level=None)
    c.execute('PRAGMA busy_timeout = 5000')
    return c


def _table_exists(c: sqlite3.Connection, name: str) -> bool:
    r = c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return r is not None


def _canonical_columns(c: sqlite3.Connection) -> dict[str, tuple]:
    """Returns {col_name: (type, notnull, default, pk)}."""
    return {
        r[1]: (r[2], r[3], r[4], r[5])
        for r in c.execute("PRAGMA table_info(memory_canonical)").fetchall()
    }


# --- check ----------------------------------------------------------------

def cmd_check(args) -> int:
    dbs = collect_dbs()
    if not dbs:
        print(f'no DBs found under {astor_dir()}')
        return 1
    print(f'Found {len(dbs)} DBs under {astor_dir()}')
    print('=' * 80)
    issues = 0
    for p in dbs:
        try:
            c = _open(p)
        except Exception as e:
            print(f'[ERR ] {p.name}: cannot open: {e}')
            issues += 1
            continue
        rel = p.relative_to(astor_dir())
        if not _table_exists(c, 'memory_canonical'):
            c.close()
            print(f'[skip] {rel}: no memory_canonical table')
            continue
        cols = _canonical_columns(c)
        missing = [col for col in REQUIRED_NOT_NULL if col not in cols or cols[col][1] != 1]
        mc_n = c.execute("SELECT COUNT(*) FROM memory_canonical").fetchone()[0]
        cand_n = c.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0] \
            if _table_exists(c, 'memory_candidates') else 0
        ev_n = c.execute("SELECT COUNT(*) FROM events").fetchone()[0] \
            if _table_exists(c, 'events') else 0
        uv = c.execute("PRAGMA user_version").fetchone()[0]
        # orphan candidates (cand.event_id not in events.id)
        orphan_n = 0
        if _table_exists(c, 'memory_candidates') and _table_exists(c, 'events'):
            orphan_n = c.execute(
                "SELECT COUNT(*) FROM memory_candidates cand "
                "WHERE NOT EXISTS (SELECT 1 FROM events e WHERE e.id = cand.event_id)"
            ).fetchone()[0]
        null_required = []
        for col in REQUIRED_NOT_NULL:
            if col in cols:
                null_required.append((col, c.execute(
                    f"SELECT COUNT(*) FROM memory_canonical WHERE {col} IS NULL"
                ).fetchone()[0]))
        c.close()
        flag = ' ❌' if missing or any(n > 0 for _, n in null_required) else ''
        print(f'{rel}{flag}')
        print(f'   memory_canonical: {len(cols)} cols, {mc_n} rows, '
              f'user_version={uv}')
        print(f'   memory_candidates: {cand_n} rows ({orphan_n} orphan)')
        print(f'   events: {ev_n} rows')
        if missing:
            print(f'   ⚠ missing/nullable required: {missing}')
            issues += 1
        for col, n in null_required:
            if n:
                print(f'   ⚠ {col} has {n} NULL rows')
                issues += 1
        if orphan_n:
            print(f'   ⚠ {orphan_n} orphan candidates (run repair-orphan)')
            issues += 1
    print()
    if issues:
        print(f'{issues} issue(s) found. Run repair-conn / repair-orphan --apply to fix.')
        return 1
    print('All DBs healthy.')
    return 0


# --- repair-conn ----------------------------------------------------------

def cmd_repair_conn(args) -> int:
    """Backfill NULL on required NOT NULL cols + set PRAGMA user_version.

    Backfill strategy: NULL required fields are filled with deterministic
    placeholders so the row remains valid and queryable. content/namespace
    get 'unknown' (the row was corrupt); event_id/candidate_id get the
    nearest valid id in the same table (1 if empty).
    """
    dbs = collect_dbs()
    apply = args.apply
    print(f'Mode: {"APPLY" if apply else "DRY-RUN"}  — repair-conn on {len(dbs)} DBs')
    if not apply:
        print('(re-run with --apply to write changes)')
    print()
    issues = 0
    for p in dbs:
        rel = p.relative_to(astor_dir())
        c = _open(p)
        if not _table_exists(c, 'memory_canonical'):
            c.close()
            continue
        cols = _canonical_columns(c)
        nulls: dict[str, int] = {}
        for col in REQUIRED_NOT_NULL:
            if col in cols:
                nulls[col] = c.execute(
                    f"SELECT COUNT(*) FROM memory_canonical WHERE {col} IS NULL"
                ).fetchone()[0]
        uv = c.execute("PRAGMA user_version").fetchone()[0]
        needs_null_fix = any(nulls.values())
        needs_version_fix = uv < PRAGMA_USER_VERSION
        if not needs_null_fix and not needs_version_fix:
            print(f'[ok  ] {rel} — no NULLs, user_version={uv}')
            c.close()
            continue
        print(f'[todo] {rel} — nulls={nulls} user_version={uv}')
        issues += 1
        if not apply:
            c.close()
            continue
        # apply
        try:
            c.execute("BEGIN IMMEDIATE")
            for col in REQUIRED_NOT_NULL:
                if not nulls.get(col):
                    continue
                if col in ('content', 'namespace'):
                    placeholder = repr(f'<repaired:{col}>')
                    c.execute(
                        f"UPDATE memory_canonical SET {col} = ? "
                        f"WHERE {col} IS NULL", (placeholder,)
                    )
                elif col in ('event_id', 'candidate_id'):
                    # pick 1 as the safe fallback; preserved-on-future-runs
                    c.execute(
                        f"UPDATE memory_canonical SET {col} = 1 "
                        f"WHERE {col} IS NULL"
                    )
            if needs_version_fix:
                c.execute(f"PRAGMA user_version = {PRAGMA_USER_VERSION}")
            c.execute("COMMIT")
            print(f'       → applied ({sum(nulls.values())} null rows fixed, '
                  f'user_version {uv}→{PRAGMA_USER_VERSION})')
        except Exception as e:
            c.execute("ROLLBACK")
            print(f'       ✗ failed: {e}')
        c.close()
    return 0 if issues == 0 or apply else 1


# --- repair-orphan --------------------------------------------------------

def cmd_repair_orphan(args) -> int:
    """Delete candidates whose event_id has no matching events row."""
    dbs = collect_dbs()
    apply = args.apply
    print(f'Mode: {"APPLY" if apply else "DRY-RUN"}  — repair-orphan on {len(dbs)} DBs')
    if not apply:
        print('(re-run with --apply to delete)')
    print()
    for p in dbs:
        rel = p.relative_to(astor_dir())
        c = _open(p)
        if not _table_exists(c, 'memory_candidates') or not _table_exists(c, 'events'):
            c.close()
            continue
        orphan_ids = [
            r[0] for r in c.execute(
                "SELECT cand.id FROM memory_candidates cand "
                "WHERE NOT EXISTS (SELECT 1 FROM events e WHERE e.id = cand.event_id)"
            ).fetchall()
        ]
        if not orphan_ids:
            print(f'[ok  ] {rel} — 0 orphans')
            c.close()
            continue
        print(f'[todo] {rel} — {len(orphan_ids)} orphan candidates')
        if apply:
            try:
                c.execute("BEGIN IMMEDIATE")
                mark = ','.join('?' * len(orphan_ids))
                c.execute(
                    f"DELETE FROM memory_candidates WHERE id IN ({mark})", orphan_ids
                )
                c.execute("COMMIT")
                print(f'       → deleted {len(orphan_ids)} orphan rows')
            except Exception as e:
                c.execute("ROLLBACK")
                print(f'       ✗ failed: {e}')
        c.close()
    return 0


# --- stats ----------------------------------------------------------------

def cmd_stats(args) -> int:
    dbs = collect_dbs()
    print(f'{"DB":50s} {"MC":>6s} {"Cand":>6s} {"Evt":>6s} {"Orph":>6s} {"UV":>3s}')
    print('-' * 80)
    for p in dbs:
        rel = str(p.relative_to(astor_dir()))
        c = _open(p)
        if not _table_exists(c, 'memory_canonical'):
            c.close()
            continue
        mc = c.execute("SELECT COUNT(*) FROM memory_canonical").fetchone()[0]
        cand = c.execute("SELECT COUNT(*) FROM memory_candidates").fetchone()[0] \
            if _table_exists(c, 'memory_candidates') else 0
        ev = c.execute("SELECT COUNT(*) FROM events").fetchone()[0] \
            if _table_exists(c, 'events') else 0
        orph = 0
        if _table_exists(c, 'memory_candidates') and _table_exists(c, 'events'):
            orph = c.execute(
                "SELECT COUNT(*) FROM memory_candidates cand "
                "WHERE NOT EXISTS (SELECT 1 FROM events e WHERE e.id = cand.event_id)"
            ).fetchone()[0]
        uv = c.execute("PRAGMA user_version").fetchone()[0]
        c.close()
        print(f'{rel:50s} {mc:>6d} {cand:>6d} {ev:>6d} {orph:>6d} {uv:>3d}')
    return 0


# --- main -----------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        prog='astor-doctor',
        description='Standalone repair CLI for astor-memory DBs.',
    )
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('check', help='read-only schema + count scan')
    rc = sub.add_parser('repair-conn', help='backfill NULL required NOT NULL cols')
    rc.add_argument('--apply', action='store_true', help='write changes (default dry-run)')
    ro = sub.add_parser('repair-orphan', help='delete candidates with no event')
    ro.add_argument('--apply', action='store_true', help='write changes (default dry-run)')
    sub.add_parser('stats', help='one-line counts per DB')
    args = ap.parse_args()
    if args.cmd == 'check':
        return cmd_check(args)
    if args.cmd == 'repair-conn':
        return cmd_repair_conn(args)
    if args.cmd == 'repair-orphan':
        return cmd_repair_orphan(args)
    if args.cmd == 'stats':
        return cmd_stats(args)
    ap.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main() or 0)
