"""Tests for v1.14.68 peer Phase 2 — relationships + rekey flow."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from astor_memory._internal.peer_identity import (
    init_identity,
    build_rekey_message,
    verify_rekey_message,
    decide_rekey_action,
)
from astor_memory._internal.peer_relationships import (
    add_peer,
    get_peer,
    list_peers,
    remove_peer,
    update_trust,
    record_rekey,
    update_rekey_status,
    apply_rekey,
    get_rekey_log,
    close_all_connections,
)


class _AstorTmp:
    """Helper: provide a tmpdir + close_all_connections in tearDown."""

    def setUp_tmp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown_tmp(self):
        close_all_connections()
        self._tmp.cleanup()


class TestPeerRelationships(_AstorTmp, unittest.TestCase):
    """Friend + trust + blacklist CRUD."""

    def setUp(self):
        self.setUp_tmp()

    def tearDown(self):
        self.tearDown_tmp()

    def test_add_peer_basic(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            p = add_peer(
                "astor:abc123abc123abc123abc123abc12345",
                alias="alice", trust=80, kind="friend",
            )
        self.assertEqual(p["alias"], "alice")
        self.assertEqual(p["trust"], 80)
        self.assertEqual(p["kind"], "friend")

    def test_add_peer_invalid_id_raises(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            with self.assertRaises(ValueError):
                add_peer("not-a-peer-id")

    def test_add_peer_invalid_trust_raises(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            with self.assertRaises(ValueError):
                add_peer(
                    "astor:abc123abc123abc123abc123abc12345",
                    trust=200,
                )

    def test_add_peer_idempotent(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            add_peer("astor:abc123abc123abc123abc123abc12345", alias="a", trust=50)
            add_peer("astor:abc123abc123abc123abc123abc12345", alias="a2", trust=70)
            p = get_peer("astor:abc123abc123abc123abc123abc12345")
        self.assertEqual(p["alias"], "a2")
        self.assertEqual(p["trust"], 70)

    def test_list_peers_filtered(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            add_peer("astor:aaa11111111111111111111111111aa", kind="friend", trust=80)
            add_peer("astor:bbb22222222222222222222222222bb", kind="friend", trust=40)
            add_peer("astor:ccc33333333333333333333333333cc", kind="blacklist", trust=0)
            high = list_peers(min_trust=70)
            blacklist = list_peers(kind="blacklist")
        self.assertEqual(len(high), 1)
        self.assertEqual(high[0]["trust"], 80)
        self.assertEqual(len(blacklist), 1)

    def test_remove_peer(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            add_peer("astor:abc123abc123abc123abc123abc12345", alias="x")
            removed = remove_peer("astor:abc123abc123abc123abc123abc12345")
            still = get_peer("astor:abc123abc123abc123abc123abc12345")
        self.assertTrue(removed)
        self.assertIsNone(still)

    def test_remove_nonexistent_returns_false(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            removed = remove_peer("astor:neverexistedneverexistednever")
        self.assertFalse(removed)

    def test_update_trust(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            add_peer("astor:abc123abc123abc123abc123abc12345", trust=30)
            p = update_trust("astor:abc123abc123abc123abc123abc12345", 90)
        self.assertEqual(p["trust"], 90)


class TestRekeyDecisionMatrix(unittest.TestCase):
    """decide_rekey_action() follows the locked policy."""

    def test_trust_above_70_auto_accepts(self):
        for t in (70, 80, 90, 100):
            self.assertEqual(decide_rekey_action({}, t), "auto_accept",
                             f"trust={t} should auto_accept")

    def test_trust_30_to_69_pending(self):
        for t in (30, 40, 50, 60, 69):
            self.assertEqual(decide_rekey_action({}, t), "manual_pending",
                             f"trust={t} should be manual_pending")

    def test_trust_below_30_rejects(self):
        for t in (0, 10, 20, 29):
            self.assertEqual(decide_rekey_action({}, t), "reject",
                             f"trust={t} should reject")

    def test_trust_none_pending(self):
        self.assertEqual(decide_rekey_action({}, None), "manual_pending")


class TestRekeyRoundtrip(_AstorTmp, unittest.TestCase):
    """build_rekey_message + verify_rekey_message works."""

    def setUp(self):
        self.setUp_tmp()

    def tearDown(self):
        self.tearDown_tmp()

    def test_build_and_verify(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            msg = build_rekey_message(
                old_peer_id="astor:old123456789012345678901234567890",
                new_peer_id="",
            )
        self.assertTrue(verify_rekey_message(msg))
        for k in ("old_peer_id", "new_peer_id", "new_public_key",
                  "timestamp", "signature", "signer_pubkey"):
            self.assertIn(k, msg)

    def test_verify_tampered_signature_fails(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            msg = build_rekey_message(
                old_peer_id="astor:old123456789012345678901234567890",
                new_peer_id="",
            )
            msg["signature"] = "tamperedsig" + "x" * 50
        self.assertFalse(verify_rekey_message(msg))

    def test_verify_missing_field_fails(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            msg = build_rekey_message("astor:o", "")
            del msg["signature"]
        self.assertFalse(verify_rekey_message(msg))


class TestApplyRekey(_AstorTmp, unittest.TestCase):
    """apply_rekey renames peer_id and preserves trust."""

    def setUp(self):
        self.setUp_tmp()

    def tearDown(self):
        self.tearDown_tmp()

    def test_apply_rekey_renames_and_preserves_trust(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            add_peer("astor:oldpeer123456789012345678901234", alias="alice",
                    trust=85, public_key="oldpub")
            msg = build_rekey_message(
                old_peer_id="astor:oldpeer123456789012345678901234",
                new_peer_id="",
            )
            rid = record_rekey(
                msg["old_peer_id"], msg["new_peer_id"],
                msg["signature"], msg["new_public_key"],
                status="auto_accepted",
            )
            result = apply_rekey(rid)
            new_p = get_peer(msg["new_peer_id"])
            old_p = get_peer(msg["old_peer_id"])
        self.assertIsNotNone(new_p)
        self.assertIsNone(old_p)
        self.assertEqual(new_p["alias"], "alice")
        self.assertEqual(new_p["trust"], 85)
        self.assertEqual(new_p["public_key"], msg["new_public_key"])

    def test_apply_rekey_chain_accumulates(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            add_peer("astor:aaaa11111111111111111111111111aa", alias="x")
            msg1 = build_rekey_message(
                old_peer_id="astor:aaaa11111111111111111111111111aa",
                new_peer_id="",
            )
            rid1 = record_rekey(
                msg1["old_peer_id"], msg1["new_peer_id"],
                msg1["signature"], msg1["new_public_key"],
                status="auto_accepted",
            )
            apply_rekey(rid1)
            # Simulate second install: build a rekey where the "current"
            # identity is treated as the OLD peer (e.g. user lost keypair
            # and regenerated). We construct the test msg directly with
            # explicit old_peer_id pointing to msg1's new, and new_peer_id
            # pointing to a different id (simulating new install).
            # We don't need a real signature here — apply_rekey just
            # renames rows.
            fake_new_pid = "astor:ffffffffffffffffffffffffffffffff"
            rid2 = record_rekey(
                msg1["new_peer_id"], fake_new_pid,
                "fakesig", "fakepub", status="auto_accepted",
            )
            apply_rekey(rid2)
            final = get_peer(fake_new_pid)
        self.assertIsNotNone(final)
        chain = final["rekey_chain"]
        self.assertIn("astor:aaaa11111111111111111111111111aa", chain)
        self.assertIn(msg1["new_peer_id"], chain)
        self.assertEqual(len(chain), 2)

    def test_apply_rekey_no_existing_relationship(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            rid = record_rekey(
                "astor:unknown123456789012345678901234ab",
                "astor:newid12345678901234567890123456c",
                "fake", "fakepub", status="auto_accepted",
            )
            result = apply_rekey(rid)
        self.assertFalse(result["applied"])


class TestRekeyLog(_AstorTmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()

    def tearDown(self):
        self.tearDown_tmp()

    def test_log_entries_filtered(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            record_rekey("astor:o1", "astor:n1", "s", "p", status="auto_accepted")
            record_rekey("astor:o2", "astor:n2", "s", "p", status="manual_pending")
            record_rekey("astor:o3", "astor:n3", "s", "p", status="rejected")
            all_entries = get_rekey_log()
            auto = get_rekey_log(status="auto_accepted")
        self.assertEqual(len(all_entries), 3)
        self.assertEqual(len(auto), 1)

    def test_update_rekey_status(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            rid = record_rekey("astor:o", "astor:n", "s", "p",
                               status="manual_pending")
            update_rekey_status(rid, "auto_accepted", note="admin approved")
            entry = get_rekey_log(status="auto_accepted")[0]
        self.assertEqual(entry["status"], "auto_accepted")
        self.assertEqual(entry["note"], "admin approved")


if __name__ == "__main__":
    unittest.main()
