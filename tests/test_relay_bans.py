"""A public relay has to be able to say no.

v1.17.23. Without a way to remove somebody, a public relay is not a
public relay -- it is a service everyone may use and nobody may stop.
The ban list is the one thing on the relay's disk that is not a log: it
has to survive a restart, or a restart would be a way out.

Both a key id and an address can be banned. A banned node that simply
makes a new key still comes from the same place, so banning the
address is what actually stops it.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _identity(tmp: str) -> tuple[str, str]:
    from astor_memory._internal.peer_identity import (
        _identity_dir, _PEER_ID_FILE, _KEYPAIR_FILE,
    )
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    iddir = _identity_dir(tmp)
    iddir.mkdir(parents=True, exist_ok=True)
    sk = Ed25519PrivateKey.generate()
    priv = sk.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub = sk.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    (iddir / _KEYPAIR_FILE).write_text(json.dumps({
        "public_key": base64.b64encode(pub).decode(),
        "private_key": base64.b64encode(priv).decode(),
    }), encoding="utf-8")
    (iddir / _PEER_ID_FILE).write_text("astor:" + "e" * 32, encoding="utf-8")
    return base64.b64encode(pub).decode(), base64.b64encode(priv).decode()


class TestRelayBans(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="astor_relay_ban_")
        self._saved = dict(os.environ)
        os.environ["ASTOR_DIR"] = self.tmp
        from astor_memory.relay_server import create_relay_app
        self.app = create_relay_app(state_dir=self.tmp)
        self.client = self.app.test_client()
        self.pub, self.priv = _identity(self.tmp)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _register(self, key_id=None, public_key=None):
        from astor_memory._internal import relay_crypto as rc
        from astor_memory._internal.peer_identity import sign
        key_id = key_id or rc.key_id_for(self.pub)
        public_key = public_key or self.pub
        nonce = self.client.post("/v1/relay/challenge").get_json()["nonce"]
        sig = sign(rc.registration_payload(key_id, public_key, nonce), self.tmp)
        return self.client.post("/v1/relay/register", json={
            "peer_id": "astor:test", "key_id": key_id,
            "public_key": public_key, "nonce": nonce, "signature": sig,
            "ttl_seconds": 600,
        })

    def test_a_banned_key_id_cannot_register(self):
        from astor_memory._internal import relay_crypto as rc
        key_id = rc.key_id_for(self.pub)
        self.app.astor_bans.add(key_id, kind="key_id", reason="abuse")
        r = self._register()
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json()["error"], "banned")
        # and it did not enter the routing table
        self.assertNotIn(key_id, self.app.astor_state.routes)

    def test_a_banned_address_cannot_register(self):
        self.app.astor_bans.add("127.0.0.1", kind="address",
                                reason="abuse")
        r = self._register()
        self.assertEqual(r.status_code, 403)

    def test_a_ban_can_be_lifted(self):
        from astor_memory._internal import relay_crypto as rc
        key_id = rc.key_id_for(self.pub)
        self.app.astor_bans.add(key_id)
        self.assertEqual(self._register().status_code, 403)
        self.assertTrue(self.app.astor_bans.remove(key_id))
        self.assertEqual(self._register().status_code, 200)

    def test_a_report_bans(self):
        from astor_memory._internal import relay_crypto as rc
        key_id = rc.key_id_for(self.pub)
        r = self.client.post("/v1/relay/report", json={
            "key_id": key_id, "reason": "spamming the relay"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self._register().status_code, 403)

    def test_the_ban_list_survives_a_restart(self):
        from astor_memory._internal import relay_crypto as rc
        key_id = rc.key_id_for(self.pub)
        self.app.astor_bans.add(key_id, reason="abuse")
        # a fresh app over the same directory is a restart
        from astor_memory.relay_server import create_relay_app
        again = create_relay_app(state_dir=self.tmp)
        self.assertTrue(again.astor_bans.is_banned(key_id))

    def test_the_ban_list_is_readable(self):
        self.app.astor_bans.add("astor:someone", reason="noise")
        rows = self.client.get("/v1/relay/bans").get_json()["bans"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["who"], "astor:someone")
        self.assertEqual(rows[0]["reason"], "noise")


if __name__ == "__main__":
    unittest.main()
