"""Tests for the `am migrate` CLI (memory-bus → astor-memory).

Per user (2026-08-15): these tests are for the internal transition tool
(only needed for users migrating from legacy memory-bus). New users
installing fresh don't need them.

Separated from test_basic.py so the main test suite stays focused on
core functionality + doesn't depend on legacy memory-bus schema.
"""
import sqlite3


def test_migrate_dry_run(tmp_path, monkeypatch):
    """Migrate dry-run reports counts without writing."""
    from astor_memory.cli.migrate import astor_migrate_from_memory_bus

    # Create a fake legacy memory-bus DB
    legacy = tmp_path / 'bus.db'
    conn = sqlite3.connect(str(legacy))
    conn.execute("""
        CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT, namespace TEXT,
            agent_id TEXT, source TEXT, action TEXT, content TEXT,
            provenance TEXT, request_id TEXT, prev_event_id INTEGER,
            tombstone INTEGER, visibility TEXT)
    """)
    conn.execute("INSERT INTO events VALUES (1, '2026-08-15T10:00:00Z', 'admin', 'cli', 'cli.write', 'write', 'fact 1', NULL, NULL, NULL, 0, 'default')")
    conn.execute("""
        CREATE TABLE memory_candidates (id INTEGER PRIMARY KEY, event_id INTEGER,
            namespace TEXT, content TEXT, kind TEXT, confidence REAL, importance REAL,
            tags TEXT, metadata TEXT, provenance TEXT, created_at TEXT, review_state TEXT,
            promoted_at TEXT, promoted_to INTEGER, rejected_reason TEXT,
            ttl_days INTEGER, expires_at TEXT, scene TEXT)
    """)
    conn.execute("INSERT INTO memory_candidates VALUES (1, 1, 'admin', 'I love coffee', 'user_preference', 0.7, 0.5, '[]', NULL, NULL, '2026-08-15T10:00:00Z', 'promoted', NULL, 1, NULL, NULL, NULL, 'casual')")
    conn.execute("""
        CREATE TABLE memory_canonical (id INTEGER PRIMARY KEY, candidate_id INTEGER,
            event_id INTEGER, namespace TEXT, content TEXT, kind TEXT, confidence REAL,
            importance REAL, tags TEXT, metadata TEXT, provenance TEXT, promoted_at TEXT,
            promoted_by TEXT, last_accessed_at TEXT, access_count INTEGER,
            tombstoned INTEGER, tombstoned_at TEXT, expires_at TEXT, scene TEXT,
            embedding BLOB, stable_id TEXT, user_id TEXT, session_id TEXT,
            scope_type TEXT, valid_from TEXT, valid_to TEXT, status TEXT,
            superseded_by INTEGER, revision INTEGER)
    """)
    conn.execute("INSERT INTO memory_canonical (id, candidate_id, event_id, namespace, content, kind, confidence, importance, tags, promoted_at, promoted_by, status, scope_type, revision) VALUES (1, 1, 1, 'admin', 'I love coffee', 'user_preference', 0.7, 0.5, '[]', '2026-08-15T10:00:00Z', 'cli.write', 'active', 'long_term', 1)")
    conn.commit()
    conn.close()

    target = tmp_path / 'astor'
    report = astor_migrate_from_memory_bus(legacy, target, dry_run=True)
    assert report.events_migrated == 1
    assert report.candidates_migrated == 1
    assert report.canonical_migrated == 1
    assert report.errors == []
    # Verify nothing was written
    assert not (target / 'astor_bus.db').exists()


def test_migrate_actual_writes(tmp_path, monkeypatch):
    """Migrate actually writes to astor_bus.db."""
    from astor_memory.cli.migrate import astor_migrate_from_memory_bus

    # Fake legacy
    legacy = tmp_path / 'bus.db'
    conn = sqlite3.connect(str(legacy))
    conn.execute("""CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT, namespace TEXT,
        agent_id TEXT, source TEXT, action TEXT, content TEXT, provenance TEXT,
        request_id TEXT, prev_event_id INTEGER, tombstone INTEGER, visibility TEXT)""")
    conn.execute("INSERT INTO events VALUES (1, '2026-08-15T10:00:00Z', 'admin', 'cli', 'cli.write', 'write', 'hello world', NULL, NULL, NULL, 0, 'default')")
    conn.execute("""CREATE TABLE memory_candidates (id INTEGER PRIMARY KEY, event_id INTEGER,
        namespace TEXT, content TEXT, kind TEXT, confidence REAL, importance REAL,
        tags TEXT, metadata TEXT, provenance TEXT, created_at TEXT, review_state TEXT,
        promoted_at TEXT, promoted_to INTEGER, rejected_reason TEXT,
        ttl_days INTEGER, expires_at TEXT, scene TEXT)""")
    conn.execute("""CREATE TABLE memory_canonical (id INTEGER PRIMARY KEY, candidate_id INTEGER,
        event_id INTEGER, namespace TEXT, content TEXT, kind TEXT, confidence REAL,
        importance REAL, tags TEXT, metadata TEXT, provenance TEXT, promoted_at TEXT,
        promoted_by TEXT, last_accessed_at TEXT, access_count INTEGER,
        tombstoned INTEGER, tombstoned_at TEXT, expires_at TEXT, scene TEXT,
        embedding BLOB, stable_id TEXT, user_id TEXT, session_id TEXT,
        scope_type TEXT, valid_from TEXT, valid_to TEXT, status TEXT,
        superseded_by INTEGER, revision INTEGER)""")
    conn.execute("INSERT INTO memory_canonical (id, candidate_id, event_id, namespace, content, kind, confidence, importance, tags, promoted_at, promoted_by, status, scope_type, revision, stable_id) VALUES (1, NULL, 1, 'admin', 'hello world fact', 'fact', 0.7, 0.5, '[]', '2026-08-15T10:00:00Z', 'cli.write', 'active', 'long_term', 1, 'stable_1')")
    conn.commit()
    conn.close()

    target = tmp_path / 'astor'
    report = astor_migrate_from_memory_bus(legacy, target, dry_run=False)
    assert report.events_migrated == 1
    assert report.canonical_migrated == 1
    assert (target / 'astor_bus.db').exists()
    assert (target / 'astor_nest.db').exists()

    # Verify canonical row has verdict='settled' (from status='active')
    bus_conn = sqlite3.connect(str(target / 'astor_bus.db'))
    row = bus_conn.execute("SELECT content, verdict, stable_id FROM memory_canonical").fetchone()
    assert row[0] == 'hello world fact'
    assert row[1] == 'settled'
    assert row[2] == 'stable_1'
    bus_conn.close()


def test_migrate_idempotent(tmp_path):
    """Migrating twice doesn't duplicate rows."""
    from astor_memory.cli.migrate import astor_migrate_from_memory_bus

    legacy = tmp_path / 'bus.db'
    conn = sqlite3.connect(str(legacy))
    conn.execute("""CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT, namespace TEXT,
        agent_id TEXT, source TEXT, action TEXT, content TEXT, provenance TEXT,
        request_id TEXT, prev_event_id INTEGER, tombstone INTEGER, visibility TEXT)""")
    conn.execute("INSERT INTO events VALUES (1, '2026-08-15T10:00:00Z', 'admin', 'cli', 'cli.write', 'write', 'test', NULL, NULL, NULL, 0, 'default')")
    conn.execute("""CREATE TABLE memory_candidates (id INTEGER PRIMARY KEY, event_id INTEGER,
        namespace TEXT, content TEXT, kind TEXT, confidence REAL, importance REAL,
        tags TEXT, metadata TEXT, provenance TEXT, created_at TEXT, review_state TEXT,
        promoted_at TEXT, promoted_to INTEGER, rejected_reason TEXT,
        ttl_days INTEGER, expires_at TEXT, scene TEXT)""")
    conn.execute("""CREATE TABLE memory_canonical (id INTEGER PRIMARY KEY, candidate_id INTEGER,
        event_id INTEGER, namespace TEXT, content TEXT, kind TEXT, confidence REAL,
        importance REAL, tags TEXT, metadata TEXT, provenance TEXT, promoted_at TEXT,
        promoted_by TEXT, last_accessed_at TEXT, access_count INTEGER,
        tombstoned INTEGER, tombstoned_at TEXT, expires_at TEXT, scene TEXT,
        embedding BLOB, stable_id TEXT, user_id TEXT, session_id TEXT,
        scope_type TEXT, valid_from TEXT, valid_to TEXT, status TEXT,
        superseded_by INTEGER, revision INTEGER)""")
    conn.execute("INSERT INTO memory_canonical (id, candidate_id, event_id, namespace, content, kind, confidence, importance, tags, promoted_at, promoted_by, status, scope_type, revision, stable_id) VALUES (1, NULL, 1, 'admin', 'idempotent test', 'fact', 0.7, 0.5, '[]', '2026-08-15T10:00:00Z', 'cli', 'active', 'long_term', 1, 'stable_idem')")
    conn.commit()
    conn.close()

    target = tmp_path / 'astor'
    report1 = astor_migrate_from_memory_bus(legacy, target, dry_run=False)
    report2 = astor_migrate_from_memory_bus(legacy, target, dry_run=False)
    assert report1.canonical_migrated == 1
    assert report2.canonical_migrated == 0
    assert report2.skipped_existing >= 1


def test_migrate_missing_source(tmp_path):
    """Migrate from non-existent DB returns error."""
    from astor_memory.cli.migrate import astor_migrate_from_memory_bus

    report = astor_migrate_from_memory_bus(tmp_path / 'nonexistent.db')
    assert len(report.errors) >= 1
    assert 'not found' in report.errors[0]
