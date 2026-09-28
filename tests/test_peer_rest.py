"""Tests for v1.15.19 (2026-09-28) — Peer CRUD + search + adopt REST endpoints.

Covers:
  GET    /v1/peer/list
  POST   /v1/peer/add
  POST   /v1/peer/<id>/trust
  POST   /v1/peer/<id>/allow-search
  POST   /v1/peer/<id>/blacklist
  POST   /v1/peer/<id>/unblacklist
  DELETE /v1/peer/<id>
  GET    /v1/peer/search  (server-as-client; tested with no eligible targets)
  POST   /v1/peer/adopt  (manual adopt flow)
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class _Tmp:
    def setUp_tmp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown_tmp(self):
        try:
            from astor_memory.nest import lex_index as _lex_mod
            from astor_memory.nest.vector_store import astor_reset_nest
            from astor_memory._internal.peer_relationships import close_all_connections
            # close + clear lex singletons
            with _lex_mod._LEX_SINGLETONS_LOCK:
                for lex in list(_lex_mod._LEX_SINGLETONS.values()):
                    try: lex.close()
                    except Exception: pass
                _lex_mod._LEX_SINGLETONS.clear()
            astor_reset_nest()
            # Clear BOTH bus singleton stores: legacy + new 9-db dict
            try:
                from astor_memory.bus.store import (
                    astor_reset_bus, _astor_bus_singleton, _BUS_SINGLETONS,
                )
                astor_reset_bus()
                if _BUS_SINGLETONS is not None:
                    for _b in list(_BUS_SINGLETONS.values()):
                        try: _b.close()
                        except Exception: pass
                    _BUS_SINGLETONS.clear()
            except Exception:
                pass
            close_all_connections()
            # v1.15.25 Ship I: close audit_logger singleton too, so the
            # audit db file handle is released before tmpdir cleanup.
            try:
                from astor_memory._internal.audit_logger import (
                    _reset_audit_conn,
                )
                _reset_audit_conn()
            except Exception:
                pass
        except Exception:
            pass
        self._tmp.cleanup()


class TestPeerRestCRUD(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()
        from astor_memory.server import create_app
        self.app = create_app(astor_dir=str(self.tmpdir))
        self.client = self.app.test_client()

    def tearDown(self):
        self.tearDown_tmp()

    def _valid_pid(self, ch="a"):
        return "astor:" + ch * 32

    def test_list_empty(self):
        resp = self.client.get("/v1/peer/list")
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertEqual(d["count"], 0)
        self.assertEqual(d["peers"], [])

    def test_add_invalid_peer_id_400(self):
        resp = self.client.post("/v1/peer/add",
            json={"peer_id": "nope"}, headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "invalid_peer_id")

    def test_add_out_of_range_trust_400(self):
        resp = self.client.post("/v1/peer/add", json={
            "peer_id": self._valid_pid("a"), "trust": 200},
            headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 400)

    def test_add_then_list_round_trip(self):
        pid = self._valid_pid("b")
        self.client.post("/v1/peer/add", json={
            "peer_id": pid, "alias": "alice", "trust": 80,
            "endpoint": "https://alice.example.com:7803",
            "pubkey": "AAAA"}, headers={"Content-Type": "application/json"})
        resp = self.client.get("/v1/peer/list")
        d = resp.get_json()
        self.assertEqual(d["count"], 1)
        p = d["peers"][0]
        self.assertEqual(p["peer_id"], pid)
        self.assertEqual(p["alias"], "alice")
        self.assertEqual(p["trust"], 80)
        self.assertTrue(p["has_pubkey"])
        self.assertTrue(p["endpoint"].endswith(":7803"))
        self.assertFalse(p["allow_search"])

    def test_trust_update_unknown_404(self):
        resp = self.client.post(f"/v1/peer/{self._valid_pid('x')}/trust",
            json={"trust": 50}, headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 404)

    def test_trust_update_bad_value(self):
        pid = self._valid_pid("c")
        self.client.post("/v1/peer/add", json={"peer_id": pid, "trust": 50},
            headers={"Content-Type": "application/json"})
        resp = self.client.post(f"/v1/peer/{pid}/trust",
            json={"trust": "hi"}, headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 400)

    def test_allow_search_toggle(self):
        pid = self._valid_pid("d")
        self.client.post("/v1/peer/add", json={"peer_id": pid, "trust": 80},
            headers={"Content-Type": "application/json"})
        # ON
        r = self.client.post(f"/v1/peer/{pid}/allow-search",
            json={"allow": True}, headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d["allow_search"])
        list_resp = self.client.get("/v1/peer/list").get_json()
        self.assertTrue(list_resp["peers"][0]["allow_search"])
        # OFF
        r = self.client.post(f"/v1/peer/{pid}/allow-search",
            json={"allow": False}, headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["allow_search"])

    def test_blacklist_then_unblacklist(self):
        pid = self._valid_pid("e")
        self.client.post("/v1/peer/add", json={"peer_id": pid, "trust": 80},
            headers={"Content-Type": "application/json"})
        r = self.client.post(f"/v1/peer/{pid}/blacklist",
            json={"reason": "spam"}, headers={"Content-Type": "application/json"})
        self.assertEqual(r.get_json()["kind"], "blacklist")
        self.assertEqual(r.get_json()["reason"], "spam")
        # verify
        d = self.client.get("/v1/peer/list").get_json()
        self.assertEqual(d["peers"][0]["kind"], "blacklist")
        # unblacklist
        r = self.client.post(f"/v1/peer/{pid}/unblacklist",
            json={}, headers={"Content-Type": "application/json"})
        self.assertEqual(r.get_json()["kind"], "friend")

    def test_delete(self):
        pid = self._valid_pid("f")
        self.client.post("/v1/peer/add", json={"peer_id": pid, "trust": 50},
            headers={"Content-Type": "application/json"})
        d_before = self.client.get("/v1/peer/list").get_json()
        self.assertEqual(d_before["count"], 1)
        r = self.client.delete(f"/v1/peer/{pid}")
        self.assertEqual(r.status_code, 200)
        d_after = self.client.get("/v1/peer/list").get_json()
        self.assertEqual(d_after["count"], 0)

    def test_filter_by_kind_and_min_trust(self):
        # Two friends at different trust + one blacklist
        self.client.post("/v1/peer/add", json={
            "peer_id": self._valid_pid("g"), "trust": 80},
            headers={"Content-Type": "application/json"})
        self.client.post("/v1/peer/add", json={
            "peer_id": self._valid_pid("h"), "trust": 30},
            headers={"Content-Type": "application/json"})
        self.client.post("/v1/peer/add", json={
            "peer_id": self._valid_pid("i"), "trust": 100},
            headers={"Content-Type": "application/json"})
        self.client.post(f"/v1/peer/{self._valid_pid('i')}/blacklist",
            json={}, headers={"Content-Type": "application/json"})
        # friends only with trust >= 50
        r = self.client.get("/v1/peer/list?kind=friend&min_trust=50")
        d = r.get_json()
        ids = [p["peer_id"] for p in d["peers"]]
        self.assertIn(self._valid_pid("g"), ids)
        self.assertNotIn(self._valid_pid("h"), ids)
        self.assertNotIn(self._valid_pid("i"), ids)  # blacklisted


class TestPeerAdopt(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()
        from astor_memory.server import create_app
        self.app = create_app(astor_dir=str(self.tmpdir))
        self.client = self.app.test_client()

    def tearDown(self):
        self.tearDown_tmp()

    def test_adopt_missing_source_peer_400(self):
        resp = self.client.post("/v1/peer/adopt",
            json={"tier": "source", "facts": [{"content": "x"}]},
            headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "source_peer_id_required")

    def test_adopt_missing_facts_400(self):
        resp = self.client.post("/v1/peer/adopt",
            json={"source_peer_id": "astor:" + "a"*32, "tier": "source"},
            headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "facts_required")

    def test_adopt_invalid_tier_400(self):
        resp = self.client.post("/v1/peer/adopt",
            json={"source_peer_id": "astor:" + "a"*32, "tier": "bogus-tier",
                  "facts": [{"content": "hi there"}]},
            headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["error"], "invalid_tier")

    def test_adopt_writes_to_source_tier(self):
        peer_id = "astor:" + "b" * 32
        body = {
            "source_peer_id": peer_id, "tier": "source",
            "facts": [
                {"content": "PPS adopt smoke test fact (must be 8+ chars)",
                 "kind": "fact", "tags": ["smoke"], "importance": 0.6, "fact_id": 99},
            ],
        }
        resp = self.client.post("/v1/peer/adopt", json=body,
            headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["count"], 1)
        self.assertEqual(d["written"][0]["original_fact_id"], 99)
        new_fact_id = d["written"][0]["new_fact_id"]
        self.assertGreater(new_fact_id, 0)
        # Verify it's visible in source tier
        read_resp = self.client.post("/v1/read",
            json={"query": "PPS adopt smoke test", "tier": "source", "top_k": 3},
            headers={"Content-Type": "application/json"})
        rd = read_resp.get_json()
        fids = [r["fact_id"] for r in (rd.get("results") or [])]
        self.assertIn(new_fact_id, fids)

    def test_adopt_skips_too_short(self):
        peer_id = "astor:" + "c" * 32
        body = {
            "source_peer_id": peer_id, "tier": "public",
            "facts": [
                {"content": "abc"},   # 3 chars, skipped (<8)
                {"content": "valid length fact about peer adopt flow", "kind": "fact"},
            ],
        }
        resp = self.client.post("/v1/peer/adopt", json=body,
            headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertEqual(d["count"], 1)
        self.assertEqual(d["skipped"], 1)


class TestPeerSearchLocal(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()
        from astor_memory.server import create_app
        self.app = create_app(astor_dir=str(self.tmpdir))
        self.client = self.app.test_client()

    def tearDown(self):
        self.tearDown_tmp()

    def test_search_missing_q_400(self):
        resp = self.client.get("/v1/peer/search")
        self.assertEqual(resp.status_code, 400)

    def test_search_returns_empty_when_no_friends(self):
        resp = self.client.get("/v1/peer/search?q=anything")
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertEqual(d["count"], 0)
        self.assertEqual(d["targets"], 0)
        self.assertIn("hint", d)


if __name__ == "__main__":
    unittest.main()
