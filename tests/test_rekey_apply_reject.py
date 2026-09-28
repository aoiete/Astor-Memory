"""Tests for v1.15.30 (2026-09-28) - Ship N: PPS rekey apply/reject flow."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path


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
            from astor_memory.nest.vector_store import astor_reset_nest
            astor_reset_nest()
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


class TestRekeyMigration(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()

    def tearDown(self):
        self.tearDown_tmp()

    def test_migration_adds_message_column(self):
        # Build a rekey_log with old schema (no message column) by
        # directly creating a SQLite file with the old DDL.
        import sqlite3
        audit_dir = self.tmpdir / "identity"
        audit_dir.mkdir(parents=True, exist_ok=True)
        path = str(audit_dir / "relationships.db")
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE rekey_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                old_peer_id TEXT NOT NULL,
                new_peer_id TEXT NOT NULL,
                signature TEXT NOT NULL,
                applied_at TEXT NOT NULL,
                sender_pubkey TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                note TEXT
            );
            INSERT INTO rekey_log (old_peer_id, new_peer_id, signature,
                applied_at, sender_pubkey, status, note)
            VALUES ('a', 'b', 'sig', '2026', 'pub', 'manual_pending', 'test');
        """)
        con.close()
        # Trigger migration
        from astor_memory._internal.peer_relationships import _get_conn
        conn = _get_conn(str(self.tmpdir))
        cols = {row[1] for row in conn.execute("PRAGMA table_info(rekey_log)").fetchall()}
        self.assertIn("message", cols)
        # Row preserved
        n = conn.execute("SELECT COUNT(*) FROM rekey_log").fetchone()[0]
        self.assertEqual(n, 1)

    def test_migration_idempotent(self):
        from astor_memory._internal.peer_relationships import (
            _get_conn, _migrate_rekey_log_v1530,
        )
        conn = _get_conn(str(self.tmpdir))
        _migrate_rekey_log_v1530(conn)  # no-op
        _migrate_rekey_log_v1530(conn)  # still no-op
        cols = {row[1] for row in conn.execute("PRAGMA table_info(rekey_log)").fetchall()}
        self.assertIn("message", cols)


class TestApplyRejectRekeyById(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()
        from astor_memory._internal.peer_identity import init_identity
        from astor_memory._internal.peer_relationships import add_peer, record_rekey
        init_identity(str(self.tmpdir))
        add_peer(
            "astor:" + "a" * 32, kind="friend", trust=50,
            alias="alice", endpoint="http://alice:7803",
            public_key="orig-pubkey",
            metadata={"allow_search": True},
        )
        # Create a pending rekey (no message → unapplicable)
        rid = record_rekey(
            "astor:" + "a" * 32, "astor:" + "b" * 32,
            "sig", "pubkey",
            status="manual_pending", note="test",
        )
        self.rid_no_msg = rid
        # Create a pending rekey WITH a message
        rid2 = record_rekey(
            "astor:" + "a" * 32, "astor:" + "c" * 32,
            "sig2", "pubkey2",
            status="manual_pending", note="with_msg",
            message=json.dumps({
                "old_peer_id": "astor:" + "a" * 32,
                "new_peer_id": "astor:" + "c" * 32,
                "timestamp": "2026-01-01T00:00:00Z",
                "signature": "sig2",
                "signer_pubkey": "pubkey2",
            }),
        )
        self.rid_with_msg = rid2

    def tearDown(self):
        self.tearDown_tmp()

    def test_apply_rekey_no_message_returns_error(self):
        from astor_memory._internal.peer_relationships import apply_rekey_by_id
        result = apply_rekey_by_id(self.rid_no_msg)
        self.assertIn("error", result)
        self.assertEqual(result["error"], "no_message_stored")

    def test_apply_rekey_unknown_id_returns_none(self):
        from astor_memory._internal.peer_relationships import apply_rekey_by_id
        self.assertIsNone(apply_rekey_by_id(99999))

    def test_apply_rekey_with_message(self):
        from astor_memory._internal.peer_relationships import apply_rekey_by_id
        result = apply_rekey_by_id(self.rid_with_msg)
        # Either applies (if signature is valid) or returns error.
        # We just check the function returns a dict with the rekey_id.
        self.assertIsNotNone(result)
        self.assertIn("rekey_id", result)

    def test_reject_rekey_by_id(self):
        from astor_memory._internal.peer_relationships import (
            apply_rekey_by_id, reject_rekey_by_id, get_rekey_log,
        )
        result = reject_rekey_by_id(self.rid_no_msg, reason="test reject")
        self.assertEqual(result.get("ok"), True)
        # Status updated
        rows = get_rekey_log(status="rejected")
        ids = [r["id"] for r in rows]
        self.assertIn(self.rid_no_msg, ids)

    def test_reject_unknown_id(self):
        from astor_memory._internal.peer_relationships import reject_rekey_by_id
        self.assertIsNone(reject_rekey_by_id(99999))

    def test_reject_already_accepted(self):
        # First mark as auto_accepted
        from astor_memory._internal.peer_relationships import (
            reject_rekey_by_id, get_rekey_log, update_rekey_status,
        )
        update_rekey_status(self.rid_no_msg, "auto_accepted", note="seed")
        result = reject_rekey_by_id(self.rid_no_msg)
        self.assertIn("error", result)
        self.assertEqual(result["error"], "already_accepted")


class TestRecordRekeyMessage(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()
        from astor_memory._internal.peer_identity import init_identity
        init_identity(str(self.tmpdir))

    def tearDown(self):
        self.tearDown_tmp()

    def test_record_rekey_stores_message(self):
        from astor_memory._internal.peer_relationships import (
            record_rekey, get_rekey_log,
        )
        msg = {"key": "value", "old_peer_id": "astor:" + "a" * 32}
        rid = record_rekey(
            "astor:" + "a" * 32, "astor:" + "b" * 32,
            "sig", "pubkey",
            status="manual_pending",
            message=json.dumps(msg),
        )
        # Read back
        rows = get_rekey_log()
        target = next((r for r in rows if r["id"] == rid), None)
        self.assertIsNotNone(target)
        self.assertEqual(target["message"], json.dumps(msg))

    def test_record_rekey_message_optional(self):
        from astor_memory._internal.peer_relationships import (
            record_rekey, get_rekey_log,
        )
        rid = record_rekey(
            "astor:" + "a" * 32, "astor:" + "b" * 32,
            "sig", "pubkey",
            status="manual_pending",
        )
        rows = get_rekey_log()
        target = next((r for r in rows if r["id"] == rid), None)
        self.assertIsNotNone(target)
        self.assertIsNone(target["message"])


class TestRekeyEndpoint(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()
        from astor_memory._internal.peer_identity import init_identity
        from astor_memory._internal.peer_relationships import (
            add_peer, record_rekey,
        )
        from astor_memory.server import create_app
        init_identity(str(self.tmpdir))
        add_peer("astor:" + "a" * 32, kind="friend", trust=50,
                 alias="alice", endpoint="http://a:7803", public_key="k")
        rid = record_rekey(
            "astor:" + "a" * 32, "astor:" + "b" * 32,
            "sig", "pubkey", status="manual_pending",
        )
        self.rid = rid
        self.app = create_app(astor_dir=str(self.tmpdir))
        self.client = self.app.test_client()

    def tearDown(self):
        self.tearDown_tmp()

    def test_pending_endpoint(self):
        resp = self.client.get("/v1/peer/rekey/pending")
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertEqual(d["count"], 1)
        self.assertEqual(d["results"][0]["id"], self.rid)

    def test_reject_endpoint(self):
        resp = self.client.post(
            f"/v1/peer/rekey/{self.rid}/reject",
            json={"reason": "manual reject"},
        )
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertEqual(d.get("ok"), True)

    def test_reject_unknown_id(self):
        resp = self.client.post("/v1/peer/rekey/99999/reject", json={})
        self.assertEqual(resp.status_code, 404)

    def test_accept_endpoint_no_message(self):
        resp = self.client.post(f"/v1/peer/rekey/{self.rid}/accept")
        self.assertEqual(resp.status_code, 400)
        d = resp.get_json()
        self.assertIn("error", d)


class TestCliRekey(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()
        from astor_memory._internal.peer_identity import init_identity
        from astor_memory._internal.peer_relationships import (
            add_peer, record_rekey,
        )
        init_identity(str(self.tmpdir))
        add_peer("astor:" + "a" * 32, kind="friend", trust=50,
                 alias="alice", endpoint="http://a:7803", public_key="k")
        rid = record_rekey(
            "astor:" + "a" * 32, "astor:" + "b" * 32,
            "sig", "pubkey", status="manual_pending",
        )
        self.rid = rid

    def tearDown(self):
        self.tearDown_tmp()

    def test_am_peer_rekey_pending(self):
        import sys
        from astor_memory.cli.main import main as _cli_main
        old_argv = sys.argv
        sys.argv = ["am", "peer", "rekey-pending"]
        try:
            rc = _cli_main()
        finally:
            sys.argv = old_argv
        self.assertEqual(rc, 0)

    def test_am_peer_rekey_reject(self):
        import sys
        from astor_memory.cli.main import main as _cli_main
        old_argv = sys.argv
        sys.argv = ["am", "peer", "rekey-reject", str(self.rid),
                     "--reason", "test"]
        try:
            rc = _cli_main()
        finally:
            sys.argv = old_argv
        self.assertEqual(rc, 0)





