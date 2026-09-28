"""Tests for v1.15.28 (2026-09-28) - Ship L: PPS peer quarantine."""
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


class TestQuarantineHelper(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()
        from astor_memory._internal.peer_identity import init_identity
        from astor_memory._internal.peer_relationships import add_peer
        init_identity(str(self.tmpdir))
        add_peer(
            "astor:" + "a" * 32, kind="friend", trust=80,
            alias="alice", endpoint="http://alice.example.com:7803",
            public_key="k", metadata={"allow_search": True},
        )

    def tearDown(self):
        self.tearDown_tmp()

    def test_quarantine_sets_kind_and_trust(self):
        from astor_memory._internal.peer_relationships import (
            quarantine_peer, get_peer,
        )
        result = quarantine_peer(
            "astor:" + "a" * 32, reason="too many errors",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["kind"], "quarantine")
        self.assertEqual(int(result["trust"]), 0)

    def test_quarantine_preserves_metadata(self):
        from astor_memory._internal.peer_relationships import (
            quarantine_peer, get_peer,
        )
        quarantine_peer("astor:" + "a" * 32, reason="abuse")
        p = get_peer("astor:" + "a" * 32)
        meta = p.get("metadata") or {}
        self.assertEqual(meta.get("quarantine_reason"), "abuse")
        self.assertEqual(int(meta.get("original_trust")), 80)
        self.assertIn("quarantined_at", meta)

    def test_quarantine_idempotent(self):
        from astor_memory._internal.peer_relationships import (
            quarantine_peer, get_peer,
        )
        quarantine_peer("astor:" + "a" * 32, reason="first")
        quarantine_peer("astor:" + "a" * 32, reason="second")
        p = get_peer("astor:" + "a" * 32)
        meta = p.get("metadata") or {}
        # Reason overwritten; original_trust preserved
        self.assertEqual(meta.get("quarantine_reason"), "second")
        self.assertEqual(int(meta.get("original_trust")), 80)

    def test_quarantine_unknown_peer(self):
        from astor_memory._internal.peer_relationships import quarantine_peer
        result = quarantine_peer("astor:" + "f" * 32)
        self.assertIsNone(result)

    def test_unquarantine_restores_kind_and_trust(self):
        from astor_memory._internal.peer_relationships import (
            quarantine_peer, unquarantine_peer,
        )
        quarantine_peer("astor:" + "a" * 32, reason="test")
        result = unquarantine_peer("astor:" + "a" * 32, restore_trust=True)
        self.assertEqual(result["kind"], "friend")
        self.assertEqual(int(result["trust"]), 80)  # restored to original

    def test_unquarantine_no_restore_keeps_zero(self):
        from astor_memory._internal.peer_relationships import (
            quarantine_peer, unquarantine_peer,
        )
        quarantine_peer("astor:" + "a" * 32, reason="test")
        result = unquarantine_peer("astor:" + "a" * 32, restore_trust=False)
        self.assertEqual(result["kind"], "friend")
        self.assertEqual(int(result["trust"]), 0)
        # original_trust may still be in metadata (it's the audit trail
        # of what was lost); not asserted either way.

    def test_unquarantine_unknown_peer(self):
        from astor_memory._internal.peer_relationships import unquarantine_peer
        self.assertIsNone(unquarantine_peer("astor:" + "f" * 32))

    def test_unquarantine_non_quarantined_is_noop(self):
        from astor_memory._internal.peer_relationships import (
            unquarantine_peer, get_peer,
        )
        # Peer is 'friend', not 'quarantine' — unquarantine returns peer
        # as-is (no error, no trust change)
        result = unquarantine_peer("astor:" + "a" * 32, restore_trust=False)
        self.assertEqual(result["kind"], "friend")
        self.assertEqual(int(result["trust"]), 80)

    def test_list_quarantined_peers(self):
        from astor_memory._internal.peer_relationships import (
            add_peer, quarantine_peer, list_quarantined_peers,
        )
        add_peer(
            "astor:" + "b" * 32, kind="friend", trust=50,
            endpoint="http://b:7803", public_key="k",
            metadata={"allow_search": True},
        )
        quarantine_peer("astor:" + "a" * 32, reason="r1")
        quarantine_peer("astor:" + "b" * 32, reason="r2")
        rows = list_quarantined_peers()
        self.assertEqual(len(rows), 2)
        pids = sorted([r["peer_id"] for r in rows])
        self.assertEqual(pids, sorted([
            "astor:" + "a" * 32, "astor:" + "b" * 32,
        ]))


class TestSelectSearchTargetsExcludesQuarantine(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()
        from astor_memory._internal.peer_identity import init_identity
        from astor_memory._internal.peer_relationships import (
            add_peer, quarantine_peer,
        )
        init_identity(str(self.tmpdir))
        add_peer(
            "astor:" + "a" * 32, kind="friend", trust=80,
            endpoint="http://a:7803", public_key="k",
            metadata={"allow_search": True},
        )
        add_peer(
            "astor:" + "b" * 32, kind="friend", trust=80,
            endpoint="http://b:7803", public_key="k",
            metadata={"allow_search": True},
        )
        quarantine_peer("astor:" + "a" * 32, reason="test")

    def tearDown(self):
        self.tearDown_tmp()

    def test_quarantined_peer_excluded(self):
        from astor_memory._internal.peer_search import select_search_targets
        from astor_memory._internal.peer_relationships import list_peers
        targets = select_search_targets(list_peers())
        pids = [t["peer_id"] for t in targets]
        self.assertEqual(pids, ["astor:" + "b" * 32])


class TestPeerHealthQuarantine(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()
        from astor_memory._internal.peer_identity import init_identity
        from astor_memory._internal.peer_relationships import (
            add_peer, quarantine_peer,
        )
        init_identity(str(self.tmpdir))
        add_peer(
            "astor:" + "a" * 32, kind="friend", trust=80,
            endpoint="http://a:7803", public_key="k",
            metadata={"allow_search": True},
        )
        quarantine_peer("astor:" + "a" * 32, reason="r1")

    def tearDown(self):
        self.tearDown_tmp()

    def test_quarantined_health_status(self):
        from astor_memory._internal.peer_health import peer_health
        h = peer_health("astor:" + "a" * 32)
        self.assertEqual(h["kind"], "quarantine")
        self.assertEqual(h["health"], "quarantined")


class TestQuarantineEndpoint(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()
        from astor_memory._internal.peer_identity import init_identity
        from astor_memory._internal.peer_relationships import add_peer
        from astor_memory.server import create_app
        init_identity(str(self.tmpdir))
        add_peer(
            "astor:" + "a" * 32, kind="friend", trust=80,
            alias="alice", endpoint="http://alice.example.com:7803",
            public_key="k", metadata={"allow_search": True},
        )
        self.app = create_app(astor_dir=str(self.tmpdir))
        self.client = self.app.test_client()

    def tearDown(self):
        self.tearDown_tmp()

    def test_quarantine_endpoint(self):
        resp = self.client.post(
            "/v1/peer/quarantine",
            json={"peer_id": "astor:" + "a" * 32, "reason": "abuse"},
        )
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["kind"], "quarantine")
        self.assertEqual(d["trust"], 0)

    def test_quarantine_missing_peer_id(self):
        resp = self.client.post("/v1/peer/quarantine", json={})
        self.assertEqual(resp.status_code, 400)

    def test_quarantine_unknown_peer(self):
        resp = self.client.post(
            "/v1/peer/quarantine", json={"peer_id": "astor:" + "f" * 32}
        )
        self.assertEqual(resp.status_code, 404)

    def test_unquarantine_endpoint(self):
        # Quarantine first
        self.client.post(
            "/v1/peer/quarantine", json={"peer_id": "astor:" + "a" * 32}
        )
        # Then unquarantine
        resp = self.client.post(
            "/v1/peer/unquarantine",
            json={"peer_id": "astor:" + "a" * 32, "restore_trust": True},
        )
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertEqual(d["kind"], "friend")
        self.assertEqual(d["trust"], 80)

    def test_list_endpoint(self):
        self.client.post(
            "/v1/peer/quarantine", json={"peer_id": "astor:" + "a" * 32}
        )
        resp = self.client.get("/v1/peer/quarantine/list")
        d = resp.get_json()
        self.assertEqual(d["count"], 1)
        self.assertEqual(d["peers"][0]["kind"], "quarantine")


class TestCliPeerQuarantine(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()
        from astor_memory._internal.peer_identity import init_identity
        from astor_memory._internal.peer_relationships import add_peer
        init_identity(str(self.tmpdir))
        add_peer(
            "astor:" + "a" * 32, kind="friend", trust=80,
            alias="alice", endpoint="http://alice.example.com:7803",
            public_key="k", metadata={"allow_search": True},
        )

    def tearDown(self):
        self.tearDown_tmp()

    def test_am_peer_quarantine_then_unquarantine(self):
        import sys
        from astor_memory.cli.main import main as _cli_main
        old_argv = sys.argv
        # Quarantine
        sys.argv = ["am", "peer", "quarantine", "astor:" + "a" * 32,
                    "--reason", "test"]
        rc = _cli_main()
        self.assertEqual(rc, 0)
        # List
        sys.argv = ["am", "peer", "quarantine", "--list"]
        rc = _cli_main()
        self.assertEqual(rc, 0)
        # Unquarantine
        sys.argv = ["am", "peer", "quarantine", "astor:" + "a" * 32,
                    "--unquarantine"]
        rc = _cli_main()
        self.assertEqual(rc, 0)
        # List should now be empty
        sys.argv = ["am", "peer", "quarantine", "--list"]
        rc = _cli_main()
        self.assertEqual(rc, 0)
        sys.argv = old_argv







