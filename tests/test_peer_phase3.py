"""Tests for v1.14.72 Phase 3 — peer endpoint + send + recv."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from astor_memory._internal.peer_relationships import (
    add_peer, get_peer, list_peers, update_endpoint,
    close_all_connections,
)


class _Tmp:
    def setUp_tmp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown_tmp(self):
        close_all_connections()
        self._tmp.cleanup()


class TestEndpointCRUD(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()

    def tearDown(self):
        self.tearDown_tmp()

    def test_add_peer_with_endpoint(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            p = add_peer(
                "astor:abc123abc123abc123abc123abc12345",
                endpoint="https://alice.example.com:7803",
                trust=80,
            )
        self.assertEqual(p["endpoint"], "https://alice.example.com:7803")

    def test_update_endpoint_valid(self):
        pid = "astor:" + "1" * 32
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            add_peer(pid, trust=50)
            result = update_endpoint(pid, "https://bob.example.com:7803")
        self.assertEqual(result["endpoint"], "https://bob.example.com:7803")

    def test_update_endpoint_rejects_non_http(self):
        pid = "astor:" + "2" * 32
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            add_peer(pid, trust=50)
            with self.assertRaises(ValueError):
                update_endpoint(pid, "ftp://bad")
            with self.assertRaises(ValueError):
                update_endpoint(pid, "not-a-url")

    def test_update_endpoint_preserved_through_rekey(self):
        """apply_rekey should preserve the endpoint field."""
        pid = "astor:" + "3" * 32
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            add_peer(pid, alias="x", trust=80,
                     endpoint="https://x.example.com:7803")
            from astor_memory._internal.peer_identity import build_rekey_message
            from astor_memory._internal.peer_relationships import (
                record_rekey, apply_rekey,
            )
            msg = build_rekey_message(old_peer_id=pid, new_peer_id="")
            rid = record_rekey(msg["old_peer_id"], msg["new_peer_id"],
                              msg["signature"], msg["new_public_key"],
                              status="auto_accepted")
            apply_rekey(rid)
            new_p = get_peer(msg["new_peer_id"])
        self.assertIsNotNone(new_p)
        self.assertEqual(new_p["endpoint"], "https://x.example.com:7803")


class TestRecvEndpointLogic(_Tmp, unittest.TestCase):
    """Test the /v1/peer/recv handler logic via Flask test client."""

    def setUp(self):
        self.setUp_tmp()
        # Lazy import Flask only when test runs
        from astor_memory.server import create_app
        os.environ["ASTOR_DIR"] = str(self.tmpdir)
        self.app = create_app(astor_dir=str(self.tmpdir))
        self.client = self.app.test_client()

    def tearDown(self):
        self.tearDown_tmp()

    def test_recv_malformed_body_400(self):
        resp = self.client.post("/v1/peer/recv",
                                json={},
                                headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertIn("missing msg_type", data.get("error", ""))

    def test_recv_unknown_msg_type_400(self):
        resp = self.client.post("/v1/peer/recv",
                                json={"msg_type": "weird", "msg": {"foo": 1}},
                                headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertIn("unknown msg_type", data.get("error", ""))

    def test_recv_invalid_rekey_sig_rejected(self):
        resp = self.client.post("/v1/peer/recv",
                                json={"msg_type": "rekey", "msg": {
                                    "old_peer_id": "astor:o",
                                    "new_peer_id": "astor:n",
                                    "new_public_key": "x",
                                    "timestamp": "2026-09-18T00:00:00Z",
                                    "signature": "invalidsig",
                                    "signer_pubkey": "x",
                                }},
                                headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["action"], "rejected")
        self.assertEqual(data["reason"], "signature_invalid")

    def test_recv_valid_rekey_new_peer_manual_pending(self):
        """First contact (no existing relationship) → manual_pending."""
        from astor_memory._internal.peer_identity import build_rekey_message
        msg = build_rekey_message(old_peer_id="astor:" + "0" * 32,
                                   new_peer_id="")
        resp = self.client.post("/v1/peer/recv",
                                json={"msg_type": "rekey", "msg": msg},
                                headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["action"], "manual_pending")
        self.assertIn("rekey_id", data)

    def test_recv_topic_index_creates_entries(self):
        resp = self.client.post("/v1/peer/recv",
                                json={"msg_type": "topic_index", "msg": {
                                    "sender_peer_id": "astor:" + "5" * 32,
                                    "sender_pubkey": "dGVzdA==",
                                    "topics": [
                                        {"topic": "poker", "weight": 0.9},
                                        {"topic": "nlhe", "weight": 0.5},
                                    ],
                                }},
                                headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["topics_applied"], 2)

        # Verify topic_index row was created
        from astor_memory._internal.peer_relationships import (
            list_peers_for_topic,
        )
        peers = list_peers_for_topic("poker")
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0]["peer_id"], "astor:" + "5" * 32)
        self.assertAlmostEqual(peers[0]["weight"], 0.9)

    def test_recv_topic_index_invalid_sender(self):
        resp = self.client.post("/v1/peer/recv",
                                json={"msg_type": "topic_index", "msg": {
                                    "sender_peer_id": "not-a-valid-id",
                                    "topics": [],
                                }},
                                headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
