"""
Tests for astor_doctor — 2026-10-03.

Run:
    python tests/test_astor_doctor.py

(no pytest required — uses stdlib only)
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))

import astor_doctor as ad  # noqa: E402


@contextmanager
def fake_runtime():
    tmp = Path(tempfile.mkdtemp(prefix='astor_doctor_test_'))
    pub = tmp / 'public' / 'memory'
    pub.mkdir(parents=True)
    src = tmp / 'source' / 'memory'
    src.mkdir(parents=True)
    # healthy
    h = sqlite3.connect(str(pub / 'astor_bus_public.db'))
    h.executescript("""
        CREATE TABLE memory_canonical (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            event_id INTEGER NOT NULL,
            namespace TEXT NOT NULL,
            content TEXT NOT NULL,
            visibility TEXT NOT NULL DEFAULT 'personal',
            provenance_kind TEXT NOT NULL DEFAULT 'user_write',
            kind TEXT NOT NULL DEFAULT 'fact',
            tombstoned INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE memory_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        );
        INSERT INTO events (id) VALUES (1);
        INSERT INTO memory_candidates (id, event_id) VALUES (100, 1);
        INSERT INTO memory_canonical
            (candidate_id, event_id, namespace, content) VALUES (100, 1, 'admin', 'ok');
    """)
    h.commit()
    h.close()
    # broken: NULL content (must rebuild without NOT NULL since SQLite
    # refuses to UPDATE a column to NULL when the column has NOT NULL)
    b = sqlite3.connect(str(src / 'astor_bus_source.db'))
    b.execute("PRAGMA foreign_keys = OFF")
    b.executescript("""
        CREATE TABLE memory_canonical (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            event_id INTEGER NOT NULL,
            namespace TEXT NOT NULL,
            content TEXT,                       -- intentionally nullable
            visibility TEXT NOT NULL DEFAULT 'personal',
            provenance_kind TEXT NOT NULL DEFAULT 'user_write',
            kind TEXT NOT NULL DEFAULT 'fact',
            tombstoned INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO memory_canonical
            (candidate_id, event_id, namespace, content) VALUES (1, 1, 'src', NULL);
    """)
    b.commit()
    b.close()
    old = os.environ.get('ASTOR_DIR')
    os.environ['ASTOR_DIR'] = str(tmp)
    try:
        yield tmp
    finally:
        if old is None:
            os.environ.pop('ASTOR_DIR', None)
        else:
            os.environ['ASTOR_DIR'] = old


def test_check_reports_broken_db():
    with fake_runtime():
        ns = ad.main.__globals__['argparse'].Namespace()
        rc = ad.cmd_check(ns)
    assert rc == 1


def test_check_via_cli():
    with fake_runtime() as tmp:
        r = subprocess.run(
            [sys.executable, str(SCRIPTS / 'astor_doctor.py'), 'check'],
            capture_output=True, text=True,
            env={**os.environ, 'ASTOR_DIR': str(tmp)},
            timeout=15,
        )
    assert r.returncode == 1, f'expected 1, got {r.returncode}; out={r.stdout!r}'
    assert 'NULL' in r.stdout or 'content has' in r.stdout


def test_repair_conn_dry_run_no_modify():
    with fake_runtime() as tmp:
        broken = tmp / 'source' / 'memory' / 'astor_bus_source.db'
        c = sqlite3.connect(str(broken))
        assert c.execute("SELECT COUNT(*) FROM memory_canonical WHERE content IS NULL").fetchone()[0] == 1
        assert c.execute("PRAGMA user_version").fetchone()[0] == 0
        c.close()
        ad.cmd_repair_conn(ad.main.__globals__['argparse'].Namespace(apply=False))
        c = sqlite3.connect(str(broken))
        assert c.execute("SELECT COUNT(*) FROM memory_canonical WHERE content IS NULL").fetchone()[0] == 1
        assert c.execute("PRAGMA user_version").fetchone()[0] == 0
        c.close()


def test_repair_conn_apply():
    with fake_runtime() as tmp:
        broken = tmp / 'source' / 'memory' / 'astor_bus_source.db'
        ad.cmd_repair_conn(ad.main.__globals__['argparse'].Namespace(apply=True))
        c = sqlite3.connect(str(broken))
        assert c.execute("SELECT COUNT(*) FROM memory_canonical WHERE content IS NULL").fetchone()[0] == 0
        assert c.execute("PRAGMA user_version").fetchone()[0] == 1
        c.close()


def test_repair_conn_idempotent():
    with fake_runtime() as tmp:
        ad.cmd_repair_conn(ad.main.__globals__['argparse'].Namespace(apply=True))
        # second apply: should be no-op
        ad.cmd_repair_conn(ad.main.__globals__['argparse'].Namespace(apply=True))
        broken = tmp / 'source' / 'memory' / 'astor_bus_source.db'
        c = sqlite3.connect(str(broken))
        assert c.execute("PRAGMA user_version").fetchone()[0] == 1
        c.close()


def test_repair_orphan_deletes():
    with fake_runtime() as tmp:
        pub = tmp / 'public' / 'memory' / 'astor_bus_public.db'
        c = sqlite3.connect(str(pub))
        c.execute("INSERT INTO memory_candidates (id, event_id) VALUES (999, 99999)")
        c.commit()
        c.close()
        ad.cmd_repair_orphan(ad.main.__globals__['argparse'].Namespace(apply=True))
        c = sqlite3.connect(str(pub))
        assert c.execute("SELECT COUNT(*) FROM memory_candidates WHERE id=999").fetchone()[0] == 0
        c.close()


def test_stats_runs():
    with fake_runtime():
        # capture stdout via print capture
        ad.cmd_stats(ad.main.__globals__['argparse'].Namespace())
        # no assertion on output; just that it didn't raise


def test_repair_orphan_dry_run():
    with fake_runtime() as tmp:
        pub = tmp / 'public' / 'memory' / 'astor_bus_public.db'
        c = sqlite3.connect(str(pub))
        c.execute("INSERT INTO memory_candidates (id, event_id) VALUES (888, 99999)")
        c.commit()
        c.close()
        ad.cmd_repair_orphan(ad.main.__globals__['argparse'].Namespace(apply=False))
        c = sqlite3.connect(str(pub))
        # dry-run must NOT delete
        assert c.execute("SELECT COUNT(*) FROM memory_candidates WHERE id=888").fetchone()[0] == 1
        c.close()


if __name__ == '__main__':
    import inspect
    failed = 0
    passed = 0
    for name, fn in inspect.getmembers(sys.modules[__name__], inspect.isfunction):
        if not name.startswith('test_'):
            continue
        try:
            fn()
            print(f'PASS  {name}')
            passed += 1
        except Exception as e:
            import traceback
            print(f'FAIL  {name}: {type(e).__name__}: {e}')
            traceback.print_exc()
            failed += 1
    print(f'\n{passed} passed, {failed} failed')
    sys.exit(0 if failed == 0 else 1)
