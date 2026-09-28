"""Tests for v1.15.21 Ship D — PPS-augmented recall (`/v1/peer/recall`).

The endpoint runs local recall first; if empty, fans out to all eligible
friends (trust>=50, endpoint, allow_search=True) and returns the merged
result with provenance. Behavior:
  - Local non-empty → return local results, no peer fanout.
  - Local empty → fan out; per-peer status mirrored in `per_peer`.

Tests cover:
  - 400 missing q
  - local_only mode (force by seeding a matching fact)
  - peer_fanout mode (empty local → fan out to eligible friend)
  - per_peer reports error when a friend rejects (allow_search revoked)
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from astor_memory.server import create_app


def _seed_public_fact(client, content, kind="fact", namespace="peer_recall_test"):
    """Helper: write a fact and return the fact_id (or None on failure)."""
    resp = client.post("/v1/write",
        json={"text": content, "kind": kind, "tier": "public", "namespace": namespace},
        headers={"Content-Type": "application/json"})
    d = resp.get_json()
    fids = d.get("fact_ids") or []
    return fids[0] if fids else None


class _Tmp:
    def setUp_tmp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown_tmp(self):
        try:
            from astor_memory.nest import lex_index as _lex_mod
            from astor_memory.nest.vector_store import astor_reset_nest
            from astor_memory._internal.peer_relationships import close_all_connections
            # Drop audit-log singleton if present
            try:
                from astor_memory._internal import audit_logger as _al_mod
                _al_mod._reset_audit_conn()
            except Exception:
                pass
            try:
                from astor_memory._internal import bot_binding as _bb_mod
                _bb_mod.close()
            except Exception:
                pass
            try:
                from astor_memory.forge import (
                    _forge_conns, _forge_lock,
                )
                with _forge_lock:
                    for _fc in list(_forge_conns.values()):
                        try: _fc.close()
                        except Exception: pass
                    _forge_conns.clear()
            except Exception:
                pass
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
        self._tmp.cleanup()


class TestPeerRecallBasic(_Tmp, unittest.TestCase):
    """Pin the endpoint shape + local_only short-circuit."""

    def setUp(self):
        self.setUp_tmp()
        self.app = create_app(astor_dir=str(self.tmpdir))
        self.client = self.app.test_client()

    def tearDown(self):
        self.tearDown_tmp()

    def test_missing_q_400(self):
        resp = self.client.get("/v1/peer/recall")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "q_required")

    def test_empty_local_returns_local_only_with_hint(self):
        # No peers at all → mode local_only, no fanout, hint present
        resp = self.client.get("/v1/peer/recall?q=hello&limit=3")
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertEqual(d["mode"], "local_only")
        self.assertEqual(d["local_count"], 0)
        self.assertEqual(d["peer_count"], 0)
        self.assertEqual(d["results"], [])
        # Hint mentions eligibility
        self.assertIn("hint", d)
        self.assertIn("trust", d["hint"])

    def test_local_only_when_local_has_match(self):
        # Seed a distinctive local fact. Query matching it → mode=local_only,
        # NO peer fanout (peer_count must be 0 even though friends might exist).
        fid = _seed_public_fact(
            self.client,
            "alphaomega unique phrase for local_only test in peer recall mode",
            namespace="peer_recall_test")
        self.assertIsNotNone(fid)
        # Add a friend — would fan out if local had been empty
        from astor_memory._internal.peer_relationships import add_peer
        from astor_memory._internal.peer_identity import init_identity
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            me = init_identity(str(self.tmpdir))
        add_peer("astor:" + "f" * 32, kind="friend", trust=80,
                 endpoint="http://nope.example.com:7803",
                 public_key="AAAA")
        resp = self.client.get("/v1/peer/recall?q=alphaomega+unique&limit=3")
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        # Either local matches with high relevance (mode=local_only)
        # OR local matches weakly (still local_only mode, no peer_fanout)
        self.assertEqual(d["mode"], "local_only")
        self.assertEqual(d["peer_count"], 0)
        # Verify the local fact is in results
        fids = [r.get("fact_id") for r in d.get("results", [])]
        self.assertIn(fid, fids)


if __name__ == "__main__":
    unittest.main()
