"""Tests for v1.14.67 peer identity module."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from astor_memory._internal.peer_identity import (
    _compute_peer_id,
    init_identity,
    get_identity,
    sign,
    verify,
)


class TestPeerIdDerivation(unittest.TestCase):
    """peer_id = astor:<sha256(db_path + first_run_ts)[:32]>"""

    def test_same_inputs_same_id(self):
        id1 = _compute_peer_id("/db/path/a.db", "2026-09-17T10:00:00Z")
        id2 = _compute_peer_id("/db/path/a.db", "2026-09-17T10:00:00Z")
        self.assertEqual(id1, id2)

    def test_different_db_different_id(self):
        id1 = _compute_peer_id("/db/path/a.db", "2026-09-17T10:00:00Z")
        id2 = _compute_peer_id("/db/path/b.db", "2026-09-17T10:00:00Z")
        self.assertNotEqual(id1, id2)

    def test_different_ts_different_id(self):
        id1 = _compute_peer_id("/db/path/a.db", "2026-09-17T10:00:00Z")
        id2 = _compute_peer_id("/db/path/a.db", "2026-09-17T11:00:00Z")
        self.assertNotEqual(id1, id2)

    def test_id_format(self):
        pid = _compute_peer_id("/db/path", "2026-09-17T10:00:00Z")
        self.assertTrue(pid.startswith("astor:"))
        self.assertEqual(len(pid), len("astor:") + 32)

    def test_id_is_hex(self):
        pid = _compute_peer_id("/db/path", "2026-09-17T10:00:00Z")
        hex_part = pid.split(":")[1]
        self.assertEqual(len(hex_part), 32)
        int(hex_part, 16)  # raises if not hex


class TestIdentityLifecycle(unittest.TestCase):
    """init_identity is idempotent; same peer_id across calls."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_init_creates_files(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            ident = init_identity()
        self.assertTrue(ident["peer_id"].startswith("astor:"))
        # Files on disk
        self.assertTrue((self.tmpdir / "identity" / "peer_id").exists())
        self.assertTrue((self.tmpdir / "identity" / "first_run_ts").exists())
        self.assertTrue((self.tmpdir / "identity" / "keypair.json").exists())

    def test_init_is_idempotent(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            id1 = init_identity()
            id2 = init_identity()
        self.assertEqual(id1["peer_id"], id2["peer_id"])
        self.assertEqual(id1["first_run_ts"], id2["first_run_ts"])
        self.assertEqual(id1["public_key"], id2["public_key"])

    def test_get_identity_returns_none_before_init(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            ident = get_identity()
        self.assertIsNone(ident)

    def test_get_identity_returns_existing(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            init_identity()
            ident = get_identity()
        self.assertIsNotNone(ident)
        self.assertTrue(ident["peer_id"].startswith("astor:"))


class TestSignVerify(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_sign_verify_roundtrip(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            init_identity()
            payload = b"hello astor peer test"
            sig = sign(payload)
            ident = get_identity()
            ok = verify(payload, sig, ident["public_key"])
        self.assertTrue(ok)

    def test_verify_tampered_payload_fails(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            init_identity()
            sig = sign(b"original payload")
            ident = get_identity()
            ok = verify(b"tampered payload", sig, ident["public_key"])
        self.assertFalse(ok)

    def test_verify_wrong_public_key_fails(self):
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            init_identity()
            sig = sign(b"test")
            # Random fake public key (base64 of 32 bytes)
            import base64
            fake_key = base64.b64encode(b"x" * 32).decode("ascii")
            ok = verify(b"test", sig, fake_key)
        self.assertFalse(ok)

    def test_keypair_recovery_on_corruption(self):
        """If keypair.json is corrupted, identity is regenerated but
        peer_id is preserved (DB-bound)."""
        with mock.patch.dict(os.environ, {"ASTOR_DIR": str(self.tmpdir)}):
            id1 = init_identity()
            old_key = id1["public_key"]
            # Corrupt keypair.json
            (self.tmpdir / "identity" / "keypair.json").write_text(
                "{garbage", encoding="utf-8"
            )
            id2 = init_identity()
        self.assertEqual(id1["peer_id"], id2["peer_id"], "peer_id preserved")
        self.assertNotEqual(old_key, id2["public_key"],
                            "keypair regenerated after corruption")


if __name__ == "__main__":
    unittest.main()
