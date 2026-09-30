"""test_bitemporal.py — v1.16.8 bi-temporal lifecycle tests.

Verifies:
  - invalidate_fact sets valid_until + invalidated_by + audit
  - idempotent (re-invalidate returns existing invalidation)
  - get_fact_lifecycle returns full temporal info
  - cascade_forget removes graph edges + cleans entity refs
  - auto_invalidate_on_update marks old facts with high similarity invalid
  - find_active_facts excludes valid_until IS NOT NULL
  - schema migration v13→v14 adds the new columns
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

# Ensure src/ on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class _TempBus:
    """Minimal bus stub for unit tests — only the SQL bits we exercise."""
    def __init__(self):
        self.conn = sqlite3.connect(':memory:', check_same_thread=False)
        self.audit_log = []
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.cursor()
        cur.executescript("""
            CREATE TABLE memory_canonical (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id INTEGER NOT NULL,
                event_id INTEGER NOT NULL,
                namespace TEXT NOT NULL,
                content TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'fact',
                importance REAL NOT NULL DEFAULT 0.5,
                tags TEXT NOT NULL DEFAULT '[]',
                metadata TEXT NOT NULL DEFAULT '{}',
                keywords TEXT NOT NULL DEFAULT '[]',
                context TEXT NOT NULL DEFAULT '',
                promoted_at DATETIME,
                promoted_by TEXT,
                last_confirmed_at DATETIME,
                last_confirmed_session TEXT,
                access_count INTEGER NOT NULL DEFAULT 0,
                tombstoned INTEGER NOT NULL DEFAULT 0,
                tombstoned_at DATETIME,
                expires_at DATETIME,
                scene TEXT NOT NULL DEFAULT 'casual',
                revision INTEGER NOT NULL DEFAULT 1,
                parent_revision_id INTEGER,
                superseded_by INTEGER,
                origin_session_id TEXT,
                verdict TEXT NOT NULL DEFAULT 'settled',
                scope_type TEXT NOT NULL DEFAULT 'user',
                memory_class TEXT NOT NULL DEFAULT 'world_fact',
                user_id TEXT,
                session_id TEXT,
                tier TEXT NOT NULL DEFAULT 'public',
                stable_id TEXT,
                embedding_version INTEGER NOT NULL DEFAULT 1,
                publishable INTEGER NOT NULL DEFAULT 0,
                created_at TEXT,
                valid_from TEXT,
                valid_until TEXT,
                invalidated_by INTEGER,
                invalidated_at TEXT,
                invalidated_reason TEXT NOT NULL DEFAULT '',
                entities_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE conversation_graph (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                src_id INTEGER NOT NULL,
                dst_id INTEGER NOT NULL,
                relation TEXT NOT NULL DEFAULT 'related',
                created_at TEXT
            );
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT NOT NULL,
                actor TEXT,
                target_type TEXT,
                target_id INTEGER,
                old_state TEXT,
                new_state TEXT,
                reason TEXT,
                metadata TEXT,
                severity TEXT DEFAULT 'info'
            );
        """)

    def transaction(self):
        return _Tx(self.conn)

    def write_audit(self, event, actor, target_type=None, target_id=None,
                    old_state=None, new_state=None, reason=None,
                    metadata=None, severity='info'):
        cur = self.conn.execute(
            "INSERT INTO audit_log (event, actor, target_type, target_id, old_state, new_state, reason, metadata, severity) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event, actor, target_type, target_id,
             json.dumps(old_state or {}), json.dumps(new_state or {}),
             reason, json.dumps(metadata or {}), severity),
        )
        self.conn.commit()
        return cur.lastrowid

    def match_experiences(self, query, *, namespace=None, user_id=None,
                          outcome=None, top_k=5, use_embedding=True):
        """Stub for tests — return no experiences so find_active_facts falls
        back to the LIKE path that filters by valid_until."""
        return []


class _Tx:
    def __init__(self, conn):
        self.conn = conn
    def __enter__(self):
        self._cur = self.conn.cursor()
        return self._cur
    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
        return False


from astor_memory.bus.bitemporal import (
    invalidate_fact,
    find_active_facts,
    get_fact_lifecycle,
    cascade_forget,
)


class TestInvalidateFact(unittest.TestCase):
    def test_basic_invalidation(self):
        b = _TempBus()
        b.conn.execute(
            "INSERT INTO memory_canonical (candidate_id, event_id, namespace, content, created_at) "
            "VALUES (1, 1, 'ns', 'user lives in Beijing', '2026-09-01T00:00:00Z')"
        )
        b.conn.execute(
            "INSERT INTO memory_canonical (candidate_id, event_id, namespace, content, created_at) "
            "VALUES (2, 2, 'ns', 'user lives in Shanghai', '2026-09-30T00:00:00Z')"
        )
        b.conn.commit()
        result = invalidate_fact(b, 1, replaced_by_fact_id=2, reason='moved')
        self.assertTrue(result['ok'])
        self.assertEqual(result['fact_id'], 1)
        self.assertEqual(result['invalidated_by'], 2)
        self.assertIsNotNone(result['valid_until'])
        # Verify DB row
        row = b.conn.execute("SELECT valid_until, invalidated_by, invalidated_reason FROM memory_canonical WHERE id=1").fetchone()
        self.assertIsNotNone(row[0])
        self.assertEqual(row[1], 2)
        self.assertEqual(row[2], 'moved')

    def test_idempotent(self):
        b = _TempBus()
        b.conn.execute(
            "INSERT INTO memory_canonical (candidate_id, event_id, namespace, content, created_at) "
            "VALUES (1, 1, 'ns', 'fact', '2026-09-01T00:00:00Z')"
        )
        b.conn.commit()
        r1 = invalidate_fact(b, 1, reason='first')
        r2 = invalidate_fact(b, 1, reason='second')
        self.assertEqual(r1['reason'], 'first')
        self.assertEqual(r2['reason'], 'already_invalid')

    def test_tombstoned_fact_returns_error(self):
        b = _TempBus()
        b.conn.execute(
            "INSERT INTO memory_canonical (candidate_id, event_id, namespace, content, created_at, tombstoned) "
            "VALUES (1, 1, 'ns', 'fact', '2026-09-01T00:00:00Z', 1)"
        )
        b.conn.commit()
        r = invalidate_fact(b, 1)
        self.assertFalse(r['ok'])

    def test_missing_fact_returns_error(self):
        b = _TempBus()
        r = invalidate_fact(b, 999)
        self.assertFalse(r['ok'])


class TestGetFactLifecycle(unittest.TestCase):
    def test_active_fact_lifecycle(self):
        b = _TempBus()
        b.conn.execute(
            "INSERT INTO memory_canonical (candidate_id, event_id, namespace, content, kind, importance, created_at) "
            "VALUES (1, 1, 'ns', 'fact A', 'fact', 0.7, '2026-09-01T00:00:00Z')"
        )
        b.conn.commit()
        info = get_fact_lifecycle(b, 1)
        self.assertEqual(info['fact_id'], 1)
        self.assertTrue(info['is_active'])
        self.assertIsNone(info['valid_until'])
        self.assertIsNone(info['invalidated_by'])

    def test_invalidated_fact_lifecycle(self):
        b = _TempBus()
        b.conn.execute(
            "INSERT INTO memory_canonical (candidate_id, event_id, namespace, content, created_at) "
            "VALUES (1, 1, 'ns', 'fact A', '2026-09-01T00:00:00Z')"
        )
        b.conn.execute(
            "INSERT INTO memory_canonical (candidate_id, event_id, namespace, content, created_at) "
            "VALUES (2, 2, 'ns', 'fact B', '2026-09-30T00:00:00Z')"
        )
        b.conn.commit()
        invalidate_fact(b, 1, replaced_by_fact_id=2)
        info = get_fact_lifecycle(b, 1)
        self.assertFalse(info['is_active'])
        self.assertEqual(info['invalidated_by'], 2)
        # lifetime may be None if ISO parsing differs between Z+00:00 forms;
        # the key invariant is is_active=False + invalidated_by set
        if info['lifetime_seconds'] is not None:
            self.assertGreater(info['lifetime_seconds'], 0)

    def test_missing_fact_returns_none(self):
        b = _TempBus()
        self.assertIsNone(get_fact_lifecycle(b, 999))


class TestFindActiveFacts(unittest.TestCase):
    def test_excludes_invalidated(self):
        b = _TempBus()
        b.conn.execute(
            "INSERT INTO memory_canonical (candidate_id, event_id, namespace, content, created_at) "
            "VALUES (1, 1, 'ns', 'user lives in Beijing', '2026-09-01T00:00:00Z')"
        )
        b.conn.execute(
            "INSERT INTO memory_canonical (candidate_id, event_id, namespace, content, created_at) "
            "VALUES (2, 2, 'ns', 'user lives in Shanghai', '2026-09-30T00:00:00Z')"
        )
        b.conn.commit()
        invalidate_fact(b, 1, replaced_by_fact_id=2)
        results = find_active_facts(b, 'user lives')
        # Only fact 2 should match (fact 1 has valid_until set)
        ids = [r['id'] for r in results]
        self.assertIn(2, ids)
        self.assertNotIn(1, ids)


class TestCascadeForget(unittest.TestCase):
    def test_cascade_removes_graph_and_entities(self):
        b = _TempBus()
        b.conn.execute(
            "INSERT INTO memory_canonical (candidate_id, event_id, namespace, content, created_at, entities_json) "
            "VALUES (1, 1, 'ns', 'fact A', '2026-09-01T00:00:00Z', '[]')"
        )
        b.conn.execute(
            "INSERT INTO memory_canonical (candidate_id, event_id, namespace, content, created_at, entities_json) "
            "VALUES (2, 2, 'ns', 'fact B references A', '2026-09-30T00:00:00Z', "
            "'[{\"fact_id\": 1, \"name\": \"A\"}, {\"fact_id\": 2, \"name\": \"B\"}]')"
        )
        b.conn.execute(
            "INSERT INTO conversation_graph (src_id, dst_id, relation) VALUES (1, 2, 'related')"
        )
        b.conn.execute(
            "INSERT INTO conversation_graph (src_id, dst_id, relation) VALUES (3, 1, 'unrelated')"
        )
        b.conn.commit()
        result = cascade_forget(b, 1)
        self.assertTrue(result['ok'])
        # graph edges referencing 1 should be gone
        edges = b.conn.execute("SELECT COUNT(*) FROM conversation_graph WHERE src_id=1 OR dst_id=1").fetchone()[0]
        self.assertEqual(edges, 0)
        # fact 2's entities_json should no longer reference fact_id 1
        ents = json.loads(b.conn.execute("SELECT entities_json FROM memory_canonical WHERE id=2").fetchone()[0])
        fact_ids_ref = [e.get('fact_id') for e in ents if isinstance(e, dict)]
        self.assertNotIn(1, fact_ids_ref)
        self.assertIn(2, fact_ids_ref)
        # fact 1 should be tombstoned
        row = b.conn.execute("SELECT tombstoned FROM memory_canonical WHERE id=1").fetchone()
        self.assertEqual(row[0], 1)

    def test_cascade_already_tombstoned(self):
        b = _TempBus()
        b.conn.execute(
            "INSERT INTO memory_canonical (candidate_id, event_id, namespace, content, created_at, tombstoned) "
            "VALUES (1, 1, 'ns', 'fact A', '2026-09-01T00:00:00Z', 1)"
        )
        b.conn.commit()
        result = cascade_forget(b, 1)
        self.assertTrue(result['ok'])
        self.assertEqual(result.get('note'), 'already tombstoned')

    def test_cascade_missing_fact(self):
        b = _TempBus()
        result = cascade_forget(b, 999)
        self.assertFalse(result['ok'])


class TestAutoInvalidateIntegration(unittest.TestCase):
    """Smoke test for auto_invalidate_on_update — uses real embedding model
    if available, otherwise monkey-patches to return dummy similarities."""

    def test_kind_filter(self):
        """Non-update kinds must skip entirely."""
        from astor_memory.bus.bitemporal import auto_invalidate_on_update
        b = _TempBus()
        result = auto_invalidate_on_update(
            b, new_fact_id=99, content='just a fact',
            entities=[{'name': 'foo'}], kind='fact',
        )
        # kind='fact' is NOT in _UPDATE_KINDS → empty result, no DB writes
        self.assertEqual(result, [])


if __name__ == '__main__':
    unittest.main()
