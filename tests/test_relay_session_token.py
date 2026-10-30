"""Knowing a key id is not enough to read that node's mail.

v1.17.24. Before the session token, anybody who knew a key id could
pull another node's mailbox or acknowledge its messages away -- and
the relay was about to be put on the internet.

The token is the node's credential: the relay hands it out at
registration and expects it on every poll, ack and send. A made-up
sender cannot use the relay as a free pipe either.
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


def _identity(tmp: str, name: str = "keypair.json") -> tuple[str, str]:
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
    (iddir / name).write_text(json.dumps({
        "public_key": base64.b64encode(pub).decode(),
        "private_key": base64.b64encode(priv).decode(),
    }), encoding="utf-8")
    (iddir / _PEER_ID_FILE).write_text("astor:" + "f" * 32, encoding="utf-8")
    return base64.b64encode(pub).decode(), base64.b64encode(priv).decode()


class TestRelaySessionToken(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="astor_relay_token_")
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

    def _register(self):
        from astor_memory._internal import relay_crypto as rc
        from astor_memory._internal.peer_identity import sign
        key_id = rc.key_id_for(self.pub)
        nonce = self.client.post("/v1/relay/challenge").get_json()["nonce"]
        sig = sign(rc.registration_payload(key_id, self.pub, nonce), self.tmp)
        r = self.client.post("/v1/relay/register", json={
            "peer_id": "astor:test", "key_id": key_id,
            "public_key": self.pub, "nonce": nonce, "signature": sig,
            "ttl_seconds": 600,
        })
        return key_id, r.get_json()

    def test_registration_hands_out_a_token(self):
        key_id, body = self._register()
        self.assertTrue(body.get("ok"))
        self.assertTrue(body.get("token"))

    def test_polling_without_a_token_is_refused(self):
        key_id, body = self._register()
        r = self.client.get(f"/v1/relay/poll/{key_id}")
        self.assertEqual(r.status_code, 403)

    def test_polling_with_the_wrong_token_is_refused(self):
        key_id, body = self._register()
        r = self.client.get(f"/v1/relay/poll/{key_id}",
                            headers={"X-Relay-Token": "not-the-token"})
        self.assertEqual(r.status_code, 403)

    def test_polling_with_the_token_works(self):
        key_id, body = self._register()
        r = self.client.get(f"/v1/relay/poll/{key_id}",
                            headers={"X-Relay-Token": body["token"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["messages"], [])

    def test_acking_without_a_token_is_refused(self):
        key_id, body = self._register()
        r = self.client.post("/v1/relay/ack",
                              json={"key_id": key_id, "msg_ids": ["x"]})
        self.assertEqual(r.status_code, 403)

    def test_sending_without_a_token_is_refused(self):
        key_id, body = self._register()
        r = self.client.post("/v1/relay/send", json={
            "msg_type": "public_search",
            "payload": {"v": 1, "eph": "x", "nonce": "y", "ct": "z"},
            "to_key_id": "someone", "from_key_id": key_id,
        })
        self.assertEqual(r.status_code, 403)

    def test_sending_with_the_token_works(self):
        key_id, body = self._register()
        r = self.client.post("/v1/relay/send", json={
            "msg_type": "public_search",
            "payload": {"v": 1, "eph": "x", "nonce": "y", "ct": "z"},
            "to_key_id": "someone", "from_key_id": key_id,
        }, headers={"X-Relay-Token": body["token"]})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

    def test_another_node_cannot_poll_this_ones_mail(self):
        key_id, body = self._register()
        other_pub, _ = _identity(self.tmp, "other.json")
        r = self.client.get(f"/v1/relay/poll/{key_id}",
                            headers={"X-Relay-Token": "someone-elses-token"})
        self.assertEqual(r.status_code, 403)
        self.assertNotEqual(other_pub, self.pub)

    def test_a_restart_means_a_new_token(self):
        # the token lives with the route, and the route is memory only
        key_id, body = self._register()
        from astor_memory.relay_server import create_relay_app
        again = create_relay_app(state_dir=self.tmp)
        # the restarted relay has its own routing table -- that is the
        # point: the token lives with the route
        r = again.test_client().get(f"/v1/relay/poll/{key_id}",
                                    headers={"X-Relay-Token": body["token"]})
        self.assertEqual(r.status_code, 403,
                         "a token from before the restart must not work")


if __name__ == "__main__":
    unittest.main()
