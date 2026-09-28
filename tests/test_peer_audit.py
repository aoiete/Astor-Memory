"""Tests for v1.15.25 (2026-09-28) - Ship I: PPS per-peer audit log endpoint."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path


def _make_audit_conn_with_old_schema(path: str):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            actor TEXT NOT NULL,
            tier TEXT NOT NULL CHECK(tier IN ('public', 'source', 'private')),
            user_id TEXT,
            action TEXT NOT NULL CHECK(action IN (
                'read', 'write', 'delete', 'compact', 'migrate',
                'admin_op', 'recall', 'init'
            )),
            target TEXT,
            reason TEXT,
            metadata TEXT NOT NULL DEFAULT '{}'
        );
    """)
    return con


class TestAstorSetPeerId(unittest.TestCase):
    def test_server_actor_extracts_peer_id(self):
        from astor_memory._internal.audit_logger import astor_set_peer_id
        self.assertEqual(
            astor_set_peer_id("server:astor:abc1234567890abcdef1234567890abc"),
            "astor:abc1234567890abcdef1234567890abc"
        )

    def test_other_actor_returns_none(self):
        from astor_memory._internal.audit_logger import astor_set_peer_id
        self.assertIsNone(astor_set_peer_id("admin:first_admin"))
        self.assertIsNone(astor_set_peer_id("user:alice"))
        self.assertIsNone(astor_set_peer_id("system"))
        self.assertIsNone(astor_set_peer_id(None))
        self.assertIsNone(astor_set_peer_id(""))

    def test_wrong_length_returns_none(self):
        from astor_memory._internal.audit_logger import astor_set_peer_id
        self.assertIsNone(astor_set_peer_id("server:astor:tooshort"))
        self.assertIsNone(astor_set_peer_id("server:astor:" + "a" * 32 + "extra"))


class TestMigrationV1525(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        # audit_logger expects ASTOR_DIR/audit/astor_audit.db
        audit_dir = self.tmpdir / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        self.audit_path = str(audit_dir / "astor_audit.db")
        os.environ["ASTOR_DIR"] = str(self.tmpdir)

    def tearDown(self):
        from astor_memory._internal.audit_logger import _reset_audit_conn
        try: _reset_audit_conn()
        except Exception: pass
        os.environ.pop("ASTOR_DIR", None)
        try: self._tmp.cleanup()
        except Exception: pass

    def test_migrates_old_schema_and_preserves_rows(self):
        con = _make_audit_conn_with_old_schema(self.audit_path)
        con.execute(
            "INSERT INTO audit (actor, tier, action, target, metadata) "
            "VALUES (?, ?, ?, ?, ?)",
            ("admin:first_admin", "public", "read",
             "memory_canonical/id=42", '{"src":"old_schema"}'),
        )
        con.execute(
            "INSERT INTO audit (actor, tier, action, target, metadata) "
            "VALUES (?, ?, ?, ?, ?)",
            ("user:alice", "private", "write",
             "memory_canonical/id=43", '{"src":"old_schema_2"}'),
        )
        con.commit()
        con.close()

        from astor_memory._internal.audit_logger import _get_audit_conn
        conn = _get_audit_conn()
        cols = {row[1] for row in conn.execute("PRAGMA table_info(audit)").fetchall()}
        self.assertIn("peer_id", cols)
        n_after = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        self.assertEqual(n_after, 2)
        rows = conn.execute("SELECT actor, peer_id, metadata FROM audit").fetchall()
        for actor, pid, md in rows:
            self.assertIsNone(pid, f"unexpected peer_id for {actor}: {pid}")
            self.assertIn("old_schema", md)

    def test_idempotent_when_already_migrated(self):
        from astor_memory._internal.audit_logger import (
            _get_audit_conn, _migrate_audit_schema_v1525,
        )
        conn = _get_audit_conn()
        conn.execute(
            "INSERT INTO audit (actor, tier, action, peer_id, metadata) "
            "VALUES (?, ?, ?, ?, ?)",
            ("server:astor:abc1234567890abcdef1234567890abc",
             "public", "peer_recall",
             "astor:abc1234567890abcdef1234567890abc",
             '{"src":"already_migrated"}'),
        )
        n_before = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        _migrate_audit_schema_v1525(conn)
        n_after = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        self.assertEqual(n_before, n_after)

    def test_old_action_check_no_longer_blocks(self):
        from astor_memory._internal.audit_logger import _get_audit_conn
        conn = _get_audit_conn()
        conn.execute(
            "INSERT INTO audit (actor, tier, action, peer_id, metadata) "
            "VALUES (?, ?, ?, ?, ?)",
            ("server:astor:abc1234567890abcdef1234567890abc",
             "public", "peer_recall",
             "astor:abc1234567890abcdef1234567890abc", "{}"),
        )
        n = conn.execute(
            "SELECT COUNT(*) FROM audit WHERE action = 'peer_recall'"
        ).fetchone()[0]
        self.assertEqual(n, 1)


class TestAstorQueryPeerAudit(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        os.environ["ASTOR_DIR"] = str(self.tmpdir)
        from astor_memory._internal.audit_logger import _get_audit_conn
        self.conn = _get_audit_conn()
        for i in range(3):
            self.conn.execute(
                "INSERT INTO audit (actor, tier, action, peer_id, metadata) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"server:astor:{'a' * 32}", "public",
                 "peer_recall", f"astor:{'a' * 32}",
                 json.dumps({"i": i})),
            )
        for i in range(2):
            self.conn.execute(
                "INSERT INTO audit (actor, tier, action, peer_id, metadata) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"server:astor:{'b' * 32}", "public",
                 "peer_adopt", f"astor:{'b' * 32}",
                 json.dumps({"i": i})),
            )

    def tearDown(self):
        from astor_memory._internal.audit_logger import _reset_audit_conn
        try: _reset_audit_conn()
        except Exception: pass
        os.environ.pop("ASTOR_DIR", None)
        try: self._tmp.cleanup()
        except Exception: pass

    def test_returns_only_target_peer(self):
        from astor_memory._internal.audit_logger import astor_query_peer_audit
        rows = astor_query_peer_audit(f"astor:{'a' * 32}")
        self.assertEqual(len(rows), 3)
        for r in rows:
            self.assertEqual(r["peer_id"], f"astor:{'a' * 32}")

    def test_filter_by_action(self):
        from astor_memory._internal.audit_logger import astor_query_peer_audit
        rows = astor_query_peer_audit(
            f"astor:{'b' * 32}", action="peer_adopt"
        )
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertEqual(r["action"], "peer_adopt")

    def test_filter_by_action_pattern(self):
        from astor_memory._internal.audit_logger import astor_query_peer_audit
        rows = astor_query_peer_audit(
            f"astor:{'a' * 32}", action="peer_%"
        )
        self.assertEqual(len(rows), 3)

    def test_metadata_parsed_as_dict(self):
        from astor_memory._internal.audit_logger import astor_query_peer_audit
        rows = astor_query_peer_audit(f"astor:{'a' * 32}")
        for r in rows:
            self.assertIsInstance(r["metadata"], dict)
            self.assertIn("i", r["metadata"])

    def test_limit_clamped(self):
        from astor_memory._internal.audit_logger import astor_query_peer_audit
        rows = astor_query_peer_audit(f"astor:{'a' * 32}", limit=9999)
        self.assertEqual(len(rows), 3)
        rows = astor_query_peer_audit(f"astor:{'a' * 32}", limit=0)
        self.assertEqual(len(rows), 1)

    def test_empty_when_no_match(self):
        from astor_memory._internal.audit_logger import astor_query_peer_audit
        rows = astor_query_peer_audit("astor:nonexistent")
        self.assertEqual(rows, [])

    def test_filter_by_since(self):
        from astor_memory._internal.audit_logger import astor_query_peer_audit
        rows = astor_query_peer_audit(
            f"astor:{'a' * 32}", since="2099-01-01T00:00:00Z"
        )
        self.assertEqual(len(rows), 0)


if __name__ == "__main__":
    unittest.main()


class _Tmp:
    def setUp_tmp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        os.environ["ASTOR_DIR"] = str(self.tmpdir)

    def tearDown_tmp(self):
        try:
            from astor_memory._internal.audit_logger import _reset_audit_conn
            _reset_audit_conn()
        except Exception: pass
        try:
            from astor_memory.nest import lex_index as _lex_mod
            with _lex_mod._LEX_SINGLETONS_LOCK:
                for lex in list(_lex_mod._LEX_SINGLETONS.values()):
                    try: lex.close()
                    except Exception: pass
                _lex_mod._LEX_SINGLETONS.clear()
        except Exception: pass
        try:
            from astor_memory.bus.store import (
                astor_reset_bus, _BUS_SINGLETONS,
            )
            astor_reset_bus()
            if _BUS_SINGLETONS is not None:
                for _b in list(_BUS_SINGLETONS.values()):
                    try: _b.close()
                    except Exception: pass
                _BUS_SINGLETONS.clear()
        except Exception: pass
        try:
            from astor_memory._internal.peer_relationships import close_all_connections
            close_all_connections()
        except Exception: pass
        try:
            from astor_memory.forge import (_forge_conns, _forge_lock)
            with _forge_lock:
                for _fc in list(_forge_conns.values()):
                    try: _fc.close()
                    except Exception: pass
                _forge_conns.clear()
        except Exception: pass
        try: self._tmp.cleanup()
        except Exception: pass


class TestPeerAuditEndpoint(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()
        from astor_memory.server import create_app
        self.app = create_app(astor_dir=str(self.tmpdir))
        self.client = self.app.test_client()
        from astor_memory._internal.audit_logger import _get_audit_conn
        self.conn = _get_audit_conn()
        for i, action in enumerate(["peer_recall", "peer_adopt"]):
            self.conn.execute(
                "INSERT INTO audit (actor, tier, action, peer_id, metadata) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"server:astor:{'c' * 32}", "public", action,
                 f"astor:{'c' * 32}", json.dumps({"i": i})),
            )

    def tearDown(self):
        self.tearDown_tmp()

    def test_returns_400_without_peer_id(self):
        resp = self.client.get("/v1/peer/audit")
        self.assertEqual(resp.status_code, 400)
        d = resp.get_json()
        self.assertEqual(d.get("error"), "peer_id_required")

    def test_returns_rows_for_peer(self):
        resp = self.client.get(
            f"/v1/peer/audit?peer_id=astor:{'c' * 32}"
        )
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertEqual(d["peer_id"], f"astor:{'c' * 32}")
        self.assertEqual(d["count"], 2)
        self.assertEqual(len(d["results"]), 2)

    def test_filter_by_action(self):
        resp = self.client.get(
            f"/v1/peer/audit?peer_id=astor:{'c' * 32}&action=peer_recall"
        )
        d = resp.get_json()
        self.assertEqual(d["count"], 1)
        self.assertEqual(d["results"][0]["action"], "peer_recall")

    def test_filter_by_action_pattern(self):
        resp = self.client.get(
            f"/v1/peer/audit?peer_id=astor:{'c' * 32}&action=peer_%25"
        )
        d = resp.get_json()
        self.assertEqual(d["count"], 2)

    def test_limit_param(self):
        resp = self.client.get(
            f"/v1/peer/audit?peer_id=astor:{'c' * 32}&limit=1"
        )
        d = resp.get_json()
        self.assertEqual(d["count"], 1)

    def test_empty_for_unknown_peer(self):
        resp = self.client.get(
            "/v1/peer/audit?peer_id=astor:00000000000000000000000000000000"
        )
        d = resp.get_json()
        self.assertEqual(d["count"], 0)
        self.assertEqual(d["results"], [])


class TestCliPeerAudit(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()
        from astor_memory._internal.audit_logger import (
            _get_audit_conn, astor_audit,
        )
        self.conn = _get_audit_conn()
        astor_audit(
            actor="server:astor:" + "d" * 32,
            tier="public", action="peer_recall",
            peer_id="astor:" + "d" * 32,
            metadata={"i": 1},
        )

    def tearDown(self):
        self.tearDown_tmp()

    def test_am_peer_audit_runs(self):
        from astor_memory.cli.main import main as _cli_main
        import sys
        old_argv = sys.argv
        sys.argv = ["am", "peer", "audit", "astor:" + "d" * 32]
        try:
            rc = _cli_main()
        finally:
            sys.argv = old_argv
        self.assertEqual(rc, 0)







