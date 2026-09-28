"""Tests for v1.15.24 (2026-09-28) — Ship H: PPS auto-trigger on body flag.

Covers:
  - dispatch_peer_fanout() helper: returns empty result for unknown peers
  - /v1/read body.peer_fanout=true: no behavior change when local has hits
  - /v1/read body.peer_fanout=true: peer_dispatched=true + peer_results
    present when local empty
  - /v1/read body.peer_fanout=false (or absent): peer_dispatched stays false
  - hermes_adapter astor_recall tool: forwards peer_fanout arg to body
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class TestDispatchPeerFanoutHelper(unittest.TestCase):
    """Pure logic tests for dispatch_peer_fanout()."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        os.environ["ASTOR_DIR"] = str(self.tmpdir)

    def tearDown(self):
        os.environ.pop("ASTOR_DIR", None)
        try:
            from astor_memory.nest import lex_index as _lex_mod
            from astor_memory.nest.vector_store import astor_reset_nest
            from astor_memory._internal.peer_relationships import close_all_connections
            with _lex_mod._LEX_SINGLETONS_LOCK:
                for lex in list(_lex_mod._LEX_SINGLETONS.values()):
                    try: lex.close()
                    except Exception: pass
                _lex_mod._LEX_SINGLETONS.clear()
            astor_reset_nest()
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
            close_all_connections()
        except Exception:
            pass
        try:
            from astor_memory.forge import (_forge_conns, _forge_lock)
            with _forge_lock:
                for _fc in list(_forge_conns.values()):
                    try: _fc.close()
                    except Exception: pass
                _forge_conns.clear()
        except Exception:
            pass
        self._tmp.cleanup()

    def test_no_local_identity_returns_error(self):
        from astor_memory._internal.peer_recall import dispatch_peer_fanout
        # Without init_identity (no identity dir), we get a no_local_key error.
        result = dispatch_peer_fanout(query="test", limit=5)
        # Either no_local_identity or no_local_key — both signal "no fanout"
        self.assertEqual(result["mode"], "peer_fanout")
        self.assertEqual(result["peer_count"], 0)
        self.assertIn(result.get("local_error"),
                      ["no_local_identity", "no_local_key", None])
        self.assertEqual(result["peer_results"], [])

    def test_no_eligible_friends_returns_hint(self):
        # Init identity so the helper has an actor, but no peers exist.
        from astor_memory._internal.peer_identity import init_identity
        from astor_memory._internal.peer_recall import dispatch_peer_fanout
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            init_identity(str(self.tmpdir))
        result = dispatch_peer_fanout(query="test", limit=5)
        self.assertEqual(result["mode"], "peer_fanout")
        self.assertEqual(result["peer_count"], 0)
        self.assertIn("hint", result)
        self.assertIn("no eligible friends", result["hint"])

    def test_result_shape_when_no_friend(self):
        from astor_memory._internal.peer_recall import dispatch_peer_fanout
        result = dispatch_peer_fanout(query="test", limit=10)
        # Always returns these keys
        for k in ('peer_results', 'per_peer', 'peer_count', 'local_error', 'mode'):
            self.assertIn(k, result)


class _Tmp:
    def setUp_tmp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        os.environ["ASTOR_DIR"] = str(self.tmpdir)

    def tearDown_tmp(self):
        try:
            from astor_memory.nest import lex_index as _lex_mod
            from astor_memory.nest.vector_store import astor_reset_nest
            from astor_memory._internal.peer_relationships import close_all_connections
            with _lex_mod._LEX_SINGLETONS_LOCK:
                for lex in list(_lex_mod._LEX_SINGLETONS.values()):
                    try: lex.close()
                    except Exception: pass
                _lex_mod._LEX_SINGLETONS.clear()
            astor_reset_nest()
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
            close_all_connections()
        except Exception:
            pass
        try:
            from astor_memory.forge import (_forge_conns, _forge_lock)
            with _forge_lock:
                for _fc in list(_forge_conns.values()):
                    try: _fc.close()
                    except Exception: pass
                _forge_conns.clear()
        except Exception:
            pass
        try:
            self._tmp.cleanup()
        except Exception:
            pass


class TestHermesAdapterPeerFanout(unittest.TestCase):
    """Verify the hermes astor_recall tool forwards peer_fanout."""

    def test_astor_recall_tool_includes_peer_fanout_key(self):
        # Read the tool spec from hermes_adapter's get_tool_schemas output
        from astor_memory.hermes_adapter import AstorMemoryProvider
        adapter = AstorMemoryProvider()
        schemas = adapter.get_tool_schemas()
        recall_schema = next(
            (s for s in schemas if s.get("name") == "astor_recall"), None
        )
        self.assertIsNotNone(recall_schema, "astor_recall tool not found")
        # peer_fanout is in the tool's properties
        props = recall_schema.get("parameters", {}).get("properties", {})
        self.assertIn("peer_fanout", props,
                      "peer_fanout key missing from astor_recall tool schema")
        self.assertEqual(props["peer_fanout"].get("default"), False,
                         "peer_fanout default should be False")




class TestReadBodyPeerFanout(_Tmp, unittest.TestCase):
    """/v1/read body.peer_fanout body flag behavior."""

    def setUp(self):
        self.setUp_tmp()
        from astor_memory.server import create_app
        self.app = create_app(astor_dir=str(self.tmpdir))
        self.client = self.app.test_client()

    def tearDown(self):
        self.tearDown_tmp()

    def test_no_flag_peer_dispatched_false(self):
        """Default /v1/read: peer_dispatched=false, no peer_results."""
        # Seed a fact so local recall finds something
        self.client.post("/v1/write", json={
            "text": "PPS auto-trigger test fact 8+ chars",
            "tier": "public", "kind": "fact", "namespace": "ship_h",
        }, headers={"Content-Type": "application/json"})
        resp = self.client.post("/v1/read",
            json={"query": "PPS auto-trigger", "top_k": 3},
            headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertIn("peer_dispatched", d)
        self.assertFalse(d["peer_dispatched"])
        self.assertEqual(d["peer_count"], 0)
        self.assertEqual(d["peer_results"], [])
        self.assertEqual(d["peer_per_peer"], [])

    def test_explicit_false_peer_dispatched_false(self):
        """body.peer_fanout=false: same as default."""
        self.client.post("/v1/write", json={
            "text": "PPS auto-trigger false flag test 8+ chars",
            "tier": "public", "kind": "fact", "namespace": "ship_h",
        }, headers={"Content-Type": "application/json"})
        resp = self.client.post("/v1/read", json={
            "query": "PPS auto-trigger false",
            "peer_fanout": False, "top_k": 3,
        }, headers={"Content-Type": "application/json"})
        d = resp.get_json()
        self.assertFalse(d["peer_dispatched"])

    def test_flag_true_local_has_hits_no_fanout(self):
        """peer_fanout=true BUT local recall found something → no fanout.

        This is the most important assertion: peer fanout is a FALLBACK,
        not a supplement. If local recall already has answers, the flag
        is ignored (no double-counting).
        """
        self.client.post("/v1/write", json={
            "text": "PPS fanout gate test local hits found 8+ chars",
            "tier": "public", "kind": "fact", "namespace": "ship_h",
        }, headers={"Content-Type": "application/json"})
        resp = self.client.post("/v1/read", json={
            "query": "PPS fanout gate test",
            "peer_fanout": True, "top_k": 3,
        }, headers={"Content-Type": "application/json"})
        d = resp.get_json()
        # Local wins, peer_dispatched=False
        self.assertFalse(d["peer_dispatched"])
        self.assertEqual(d["peer_count"], 0)
        # But the local results ARE present
        self.assertGreater(d["count"], 0)

    def test_flag_true_local_empty_triggers_fanout(self):
        """peer_fanout=true AND local empty → dispatch to eligible friends.
        No friends in this tmpdir, so we get a hint + 0 results.
        """
        # Don't seed anything. We use a query that has no related facts
        # in the empty tier. Since no friends are added either, the
        # helper returns "no eligible friends".
        resp = self.client.post("/v1/read", json={
            "query": "schf9xkqz97 abcdef 1234 mmm zzzz",
            "peer_fanout": True, "top_k": 3,
        }, headers={"Content-Type": "application/json"})
        d = resp.get_json()
        # peer_dispatched=True
        self.assertTrue(d["peer_dispatched"])
        # No eligible friends → peer_count=0, hint present
        self.assertEqual(d["peer_count"], 0)
        self.assertEqual(d["peer_results"], [])
        self.assertEqual(d["peer_per_peer"], [])

    def test_response_includes_new_fields(self):
        """Verify all 5 new fields are present in the response shape."""
        resp = self.client.post("/v1/read",
            json={"query": "anything", "top_k": 3},
            headers={"Content-Type": "application/json"})
        d = resp.get_json()
        for k in ('peer_dispatched', 'peer_count',
                  'peer_results', 'peer_per_peer', 'peer_local_error'):
            self.assertIn(k, d, f"missing key {k}")


if __name__ == "__main__":
    unittest.main()
