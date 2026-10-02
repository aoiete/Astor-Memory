"""tests/test_episodes.py — v1.16.12 (2026-09-30)

Tests for the L0 episode layer (M-flow inspired).

Critical tests:
  - write/read/list episodes
  - derived_fact_ids link + idempotent re-link
  - 3-tier isolation (namespace/user_id filter)
  - find_episodes_by_fact reverse lookup
  - Migration creates the table on fresh DBs
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astor_memory.bus.schema import (
    SCHEMA_VERSION,
    _astor_upgrade_v14_to_v15,
    astor_init_schema,
)
from astor_memory.nest.episodes import (
    write_episode,
    read_episode,
    list_episodes,
    link_fact_to_episode,
    find_episodes_by_fact,
)


def _fresh_db() -> str:
    """Create a fresh in-memory-style temp DB with all migrations applied."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    return path


class TestEpisodesMigration(unittest.TestCase):
    def test_schema_version_is_15(self):
        # v1.16.x: schema bumped to 16 (visibility tier + provenance_kind).
        # Keep the test name (semantic in history) but assert the current value.
        self.assertEqual(SCHEMA_VERSION, 16)

    def test_v14_to_v15_creates_episodes_table(self):
        import sqlite3
        path = _fresh_db()
        try:
            conn = sqlite3.connect(path)
            astor_init_schema(conn)
            # The episodes table should exist
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='episodes'"
            ).fetchone()
            self.assertIsNotNone(row)
            conn.close()
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_idempotent_migration(self):
        """Running v14_to_v15 twice should not error."""
        import sqlite3
        path = _fresh_db()
        try:
            conn = sqlite3.connect(path)
            astor_init_schema(conn)
            _astor_upgrade_v14_to_v15(conn)
            _astor_upgrade_v14_to_v15(conn)  # second call should be no-op
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='episodes'"
            ).fetchone()
            self.assertIsNotNone(row)
            conn.close()
        finally:
            if os.path.exists(path):
                os.remove(path)


class TestEpisodesCRUD(unittest.TestCase):
    def setUp(self):
        import sqlite3
        self.path = _fresh_db()
        self.conn = sqlite3.connect(self.path)
        astor_init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_write_and_read_episode(self):
        eid = write_episode(
            self.conn,
            raw_text="Maria 完成了 Q3 发布",
            namespace="admin",
            user_id="admin",
            tier="public",
            session_id="sess-1",
            derived_fact_ids=[42],
            entities=[{"type": "person", "value": "Maria"}],
        )
        self.assertIsInstance(eid, int)
        ep = read_episode(self.conn, eid)
        self.assertIsNotNone(ep)
        self.assertEqual(ep["raw_text"], "Maria 完成了 Q3 发布")
        self.assertEqual(ep["session_id"], "sess-1")
        self.assertEqual(ep["derived_fact_ids"], [42])
        self.assertEqual(ep["entities_json"], [{"type": "person", "value": "Maria"}])

    def test_read_missing_episode_returns_none(self):
        ep = read_episode(self.conn, 99999)
        self.assertIsNone(ep)

    def test_list_filtered_by_namespace(self):
        write_episode(self.conn, raw_text="A", namespace="admin", user_id="admin")
        write_episode(self.conn, raw_text="B", namespace="public", user_id="admin")
        write_episode(self.conn, raw_text="C", namespace="admin", user_id="admin")
        admin_eps = list_episodes(self.conn, namespace="admin")
        self.assertEqual(len(admin_eps), 2)
        public_eps = list_episodes(self.conn, namespace="public")
        self.assertEqual(len(public_eps), 1)
        self.assertEqual(public_eps[0]["raw_text"], "B")

    def test_list_filtered_by_user_id(self):
        write_episode(self.conn, raw_text="A", namespace="admin", user_id="alice")
        write_episode(self.conn, raw_text="B", namespace="admin", user_id="bob")
        alice_eps = list_episodes(self.conn, user_id="alice")
        self.assertEqual(len(alice_eps), 1)
        self.assertEqual(alice_eps[0]["raw_text"], "A")

    def test_list_filtered_by_session_id(self):
        write_episode(self.conn, raw_text="A", session_id="sess-1")
        write_episode(self.conn, raw_text="B", session_id="sess-2")
        write_episode(self.conn, raw_text="C", session_id="sess-1")
        sess1 = list_episodes(self.conn, session_id="sess-1")
        self.assertEqual(len(sess1), 2)

    def test_list_limit_offset(self):
        for i in range(5):
            write_episode(self.conn, raw_text=f"ep{i}", namespace="admin")
        page1 = list_episodes(self.conn, namespace="admin", limit=2, offset=0)
        page2 = list_episodes(self.conn, namespace="admin", limit=2, offset=2)
        self.assertEqual(len(page1), 2)
        self.assertEqual(len(page2), 2)
        # Different rows
        self.assertNotEqual(page1[0]["id"], page2[0]["id"])


class TestEpisodesLink(unittest.TestCase):
    def setUp(self):
        import sqlite3
        self.path = _fresh_db()
        self.conn = sqlite3.connect(self.path)
        astor_init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_link_fact_to_episode(self):
        eid = write_episode(self.conn, raw_text="test", derived_fact_ids=[])
        link_fact_to_episode(self.conn, eid, 100)
        ep = read_episode(self.conn, eid)
        self.assertIn(100, ep["derived_fact_ids"])

    def test_link_fact_is_idempotent(self):
        eid = write_episode(self.conn, raw_text="test", derived_fact_ids=[100])
        link_fact_to_episode(self.conn, eid, 100)  # duplicate
        ep = read_episode(self.conn, eid)
        # Should still be [100], not [100, 100]
        self.assertEqual(ep["derived_fact_ids"], [100])

    def test_link_multiple_facts(self):
        eid = write_episode(self.conn, raw_text="test")
        link_fact_to_episode(self.conn, eid, 100)
        link_fact_to_episode(self.conn, eid, 200)
        link_fact_to_episode(self.conn, eid, 300)
        ep = read_episode(self.conn, eid)
        self.assertEqual(set(ep["derived_fact_ids"]), {100, 200, 300})

    def test_link_to_missing_episode_silent(self):
        link_fact_to_episode(self.conn, 99999, 100)  # no error
        # No way to verify directly — just confirms no exception


class TestFindEpisodesByFact(unittest.TestCase):
    def setUp(self):
        import sqlite3
        self.path = _fresh_db()
        self.conn = sqlite3.connect(self.path)
        astor_init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_find_returns_matching_episodes(self):
        eid1 = write_episode(self.conn, raw_text="A", derived_fact_ids=[100, 200])
        eid2 = write_episode(self.conn, raw_text="B", derived_fact_ids=[200, 300])
        eid3 = write_episode(self.conn, raw_text="C", derived_fact_ids=[400])
        results = find_episodes_by_fact(self.conn, 200)
        self.assertEqual(len(results), 2)
        ids = {r["id"] for r in results}
        self.assertIn(eid1, ids)
        self.assertIn(eid2, ids)
        self.assertNotIn(eid3, ids)

    def test_find_no_match(self):
        write_episode(self.conn, raw_text="A", derived_fact_ids=[100])
        results = find_episodes_by_fact(self.conn, 999)
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
