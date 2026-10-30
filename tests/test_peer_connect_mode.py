"""A peer's connect mode, and its encryption key.

v1.17.21. The relay needs two things on a peer row: how we reach it
(direct or relay) and the key we seal relay messages to. Both live in
the metadata JSON column, next to the switches that are already there,
because a column that did not exist when the table was created is not
worth a migration.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestConnectMode(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="astor_cmode_")
        os.environ["ASTOR_DIR"] = self.tmp
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add_peer(self, peer_id="astor:peer1", **kw):
        from astor_memory._internal.peer_relationships import add_peer
        return add_peer(peer_id, endpoint="http://relay.example.com",
                        **kw)

    def test_a_new_peer_is_direct(self):
        from astor_memory._internal.peer_relationships import (
            get_peer, _read_connect_mode,
        )
        self._add_peer()
        self.assertEqual(_read_connect_mode(get_peer("astor:peer1")),
                         "direct")

    def test_the_mode_can_be_switched(self):
        from astor_memory._internal.peer_relationships import (
            get_peer, set_connect_mode, _read_connect_mode,
        )
        self._add_peer()
        set_connect_mode("astor:peer1", "relay")
        self.assertEqual(_read_connect_mode(get_peer("astor:peer1")),
                         "relay")
        set_connect_mode("astor:peer1", "direct")
        self.assertEqual(_read_connect_mode(get_peer("astor:peer1")),
                         "direct")

    def test_an_unknown_mode_is_refused(self):
        from astor_memory._internal.peer_relationships import (
            set_connect_mode,
        )
        self._add_peer()
        with self.assertRaises(ValueError):
            set_connect_mode("astor:peer1", "sideways")

    def test_setting_the_mode_keeps_the_other_metadata_keys(self):
        from astor_memory._internal.peer_relationships import (
            get_peer, set_connect_mode,
        )
        self._add_peer(metadata={"allow_search": True})
        set_connect_mode("astor:peer1", "relay")
        raw = get_peer("astor:peer1")["metadata"]
        meta = json.loads(raw) if isinstance(raw, str) else dict(raw)
        self.assertEqual(meta["connect_mode"], "relay")
        self.assertTrue(meta["allow_search"])

    def test_the_encryption_key_is_stored_and_read_back(self):
        from astor_memory._internal.peer_relationships import (
            get_peer, set_peer_x25519_key, _read_x25519_public_key,
        )
        self._add_peer()
        set_peer_x25519_key("astor:peer1", "the-encryption-key")
        self.assertEqual(
            _read_x25519_public_key(get_peer("astor:peer1")),
            "the-encryption-key")

    def test_a_peer_with_no_encryption_key_reads_as_empty(self):
        from astor_memory._internal.peer_relationships import (
            get_peer, _read_x25519_public_key,
        )
        self._add_peer()
        self.assertEqual(_read_x25519_public_key(get_peer("astor:peer1")), "")

    def test_a_peer_that_does_not_exist_is_refused(self):
        from astor_memory._internal.peer_relationships import (
            set_connect_mode, set_peer_x25519_key,
        )
        with self.assertRaises(ValueError):
            set_connect_mode("astor:nobody", "relay")
        with self.assertRaises(ValueError):
            set_peer_x25519_key("astor:nobody", "x")


class TestIdentityCarriesTheEncryptionKey(unittest.TestCase):
    """The identity endpoint has to serve the key, or a peer can never
    seal anything to us."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="astor_ident_")
        os.environ["ASTOR_DIR"] = self.tmp
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_identity_endpoint_serves_the_encryption_key(self):
        from astor_memory.server import create_app
        app = create_app(astor_dir=self.tmp)
        client = app.test_client()
        resp = client.get("/v1/peer/identity")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body.get("public_key"))
        self.assertTrue(body.get("x25519_public_key"),
                        "the endpoint has to serve the encryption key")
        # and the private key must not be anywhere near it
        from astor_memory._internal.relay_crypto import node_x25519_keys
        _pub, priv = node_x25519_keys(self.tmp)
        self.assertNotIn(priv, json.dumps(body))

    def test_the_client_extracts_the_encryption_key(self):
        from astor_memory._internal.peer_connect import _extract_identity
        got = _extract_identity({
            "peer_id": "astor:x", "public_key": "pk",
            "x25519_public_key": "xk",
        })
        self.assertEqual(got["x25519_public_key"], "xk")


if __name__ == "__main__":
    unittest.main()
