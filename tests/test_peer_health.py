"""Tests for v1.15.26 (2026-09-28) - Ship J: PPS per-peer health."""
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


class TestPeerHealthHelper(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()
        from astor_memory._internal.peer_identity import init_identity
        from astor_memory._internal.peer_relationships import add_peer
        init_identity(str(self.tmpdir))
        # Add a healthy peer
        add_peer(
            "astor:" + "a" * 32, kind="friend", trust=80,
            alias="alice", endpoint="http://alice.example.com:7803",
            public_key="fake-pubkey-bytes-1234567890",
            metadata={"allow_search": True},
        )
        # Add an unhealthy peer (no endpoint)
        add_peer(
            "astor:" + "b" * 32, kind="friend", trust=40,
            alias="bob",
        )

    def tearDown(self):
        self.tearDown_tmp()

    def test_healthy_peer_classification(self):
        from astor_memory._internal.peer_health import peer_health
        h = peer_health("astor:" + "a" * 32)
        self.assertTrue(h["found"])
        self.assertEqual(h["alias"], "alice")
        self.assertEqual(h["trust"], 80)
        self.assertTrue(h["has_pubkey"])
        self.assertTrue(h["allow_search"])
        self.assertTrue(h["online"])
        self.assertEqual(h["health"], "healthy")

    def test_no_endpoint_classified_unknown(self):
        from astor_memory._internal.peer_health import peer_health
        h = peer_health("astor:" + "b" * 32)
        self.assertTrue(h["found"])
        self.assertFalse(h["online"])
        self.assertEqual(h["health"], "unknown")

    def test_unknown_peer(self):
        from astor_memory._internal.peer_health import peer_health
        h = peer_health("astor:" + "f" * 32)
        self.assertFalse(h["found"])
        self.assertEqual(h["health"], "unknown")

    def test_low_trust_degraded(self):
        from astor_memory._internal.peer_relationships import update_trust
        from astor_memory._internal.peer_health import peer_health
        update_trust("astor:" + "a" * 32, 10)
        h = peer_health("astor:" + "a" * 32)
        self.assertEqual(h["health"], "degraded")

    def test_no_allow_search_degraded(self):
        from astor_memory._internal.peer_relationships import set_allow_search
        from astor_memory._internal.peer_health import peer_health
        set_allow_search("astor:" + "a" * 32, False)
        h = peer_health("astor:" + "a" * 32)
        self.assertEqual(h["health"], "degraded")

    def test_all_peer_health(self):
        from astor_memory._internal.peer_health import all_peer_health
        rows = all_peer_health()
        self.assertEqual(len(rows), 2)
        pids = {r["peer_id"] for r in rows}
        self.assertIn("astor:" + "a" * 32, pids)
        self.assertIn("astor:" + "b" * 32, pids)

    def test_last_seen_uses_audit_log(self):
        from astor_memory._internal.audit_logger import astor_audit
        from astor_memory._internal.peer_health import peer_health
        astor_audit(
            actor="server:astor:" + "a" * 32,
            tier="public", action="peer_recall",
            peer_id="astor:" + "a" * 32,
            metadata={"test": True},
        )
        h = peer_health("astor:" + "a" * 32)
        self.assertIsNotNone(h["last_seen_iso"])
        self.assertEqual(h["last_action"], "peer_recall")

    def test_includes_rate_limit_summary(self):
        from astor_memory._internal.peer_health import peer_health
        h = peer_health("astor:" + "a" * 32)
        # No rate limit data yet, but should be a dict (or None)
        self.assertIn("rate_limit", h)


class TestPeerHealthEndpoint(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()
        from astor_memory._internal.peer_identity import init_identity
        from astor_memory._internal.peer_relationships import add_peer
        from astor_memory.server import create_app
        init_identity(str(self.tmpdir))
        add_peer(
            "astor:" + "a" * 32, kind="friend", trust=80,
            alias="alice", endpoint="http://alice.example.com:7803",
            public_key="fake-pubkey-bytes-1234567890",
            metadata={"allow_search": True},
        )
        self.app = create_app(astor_dir=str(self.tmpdir))
        self.client = self.app.test_client()

    def tearDown(self):
        self.tearDown_tmp()

    def test_400_without_args(self):
        resp = self.client.get("/v1/peer/health")
        self.assertEqual(resp.status_code, 400)
        d = resp.get_json()
        self.assertEqual(d.get("error"), "peer_id_required")

    def test_returns_health_for_known_peer(self):
        resp = self.client.get(
            f"/v1/peer/health?peer_id=astor:{'a' * 32}"
        )
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertEqual(d["peer_id"], "astor:" + "a" * 32)
        self.assertTrue(d["found"])
        self.assertEqual(d["health"], "healthy")

    def test_returns_unknown_for_missing_peer(self):
        resp = self.client.get(
            f"/v1/peer/health?peer_id=astor:{'f' * 32}"
        )
        d = resp.get_json()
        self.assertFalse(d["found"])
        self.assertEqual(d["health"], "unknown")

    def test_all_returns_summary(self):
        resp = self.client.get("/v1/peer/health?all=true")
        d = resp.get_json()
        self.assertIn("peers", d)
        self.assertIn("by_health", d)
        self.assertEqual(d["count"], 1)
        self.assertIn("healthy", d["by_health"])


class TestCliPeerHealth(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()
        from astor_memory._internal.peer_identity import init_identity
        from astor_memory._internal.peer_relationships import add_peer
        init_identity(str(self.tmpdir))
        add_peer(
            "astor:" + "a" * 32, kind="friend", trust=80,
            alias="alice", endpoint="http://alice.example.com:7803",
            public_key="fake-pubkey-bytes-1234567890",
            metadata={"allow_search": True},
        )

    def tearDown(self):
        self.tearDown_tmp()

    def test_am_peer_health_runs(self):
        import sys
        old_argv = sys.argv
        from astor_memory.cli.main import main as _cli_main
        sys.argv = ["am", "peer", "health", "--all"]
        try:
            rc = _cli_main()
        finally:
            sys.argv = old_argv
        self.assertEqual(rc, 0)

    def test_am_peer_health_specific(self):
        import sys
        old_argv = sys.argv
        from astor_memory.cli.main import main as _cli_main
        sys.argv = ["am", "peer", "health", "astor:" + "a" * 32]
        try:
            rc = _cli_main()
        finally:
            sys.argv = old_argv
        self.assertEqual(rc, 0)







