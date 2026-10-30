"""The relay is a postman, not a reader.

v1.17.20 / relay slice 1. These tests pin the properties the design
document calls structural -- the ones that must not depend on anybody
remembering a rule:

  - the relay module contains no outbound-request call at all, so it
    cannot become an open proxy (design 8.2)
  - the relay never holds the plaintext: what sits in the mailbox is
    the ciphertext and nothing else
  - a node that cannot sign does not enter the routing table
  - a key_id that does not match the key that signed is refused
  - a plaintext payload is refused (no downgrade)
  - queued messages expire (60s) and are dropped on ack
  - the audit table records who talked to whom, and no content
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory._internal import relay_crypto as rc  # noqa: E402
from astor_memory.relay_server import create_relay_app  # noqa: E402


def _write_identity(tmp: str, name: str = "keypair.json") -> tuple[str, str]:
    """An identity keypair on disk, so peer_identity.sign can use it."""
    from astor_memory._internal.peer_identity import _identity_dir
    iddir = _identity_dir(tmp)
    iddir.mkdir(parents=True, exist_ok=True)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
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
    data = {"public_key": base64.b64encode(pub).decode(),
            "private_key": base64.b64encode(priv).decode()}
    (iddir / name).write_text(json.dumps(data), encoding="utf-8")
    # get_identity() only reads the keypair when the peer id file is
    # there too -- without it, sign() quietly generates a fresh keypair
    # and the signature no longer matches the key we handed out
    from astor_memory._internal.peer_identity import _PEER_ID_FILE
    (iddir / _PEER_ID_FILE).write_text("astor:test", encoding="utf-8")
    return data["public_key"], data["private_key"]


class TestRelayServer(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="astor_relay_")
        self.app = create_relay_app(state_dir=self.tmp)
        self.client = self.app.test_client()
        self.pub, self.priv = _write_identity(self.tmp)
        self.key_id = rc.key_id_for(self.pub)
        self.x_pub, self.x_priv = rc.generate_x25519_keypair()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _token(self):
        """The credential the relay handed out.

        Registered once per test: registering again would hand out a
        new credential and make the old one useless, and the relay
        limits registrations per address on purpose.
        """
        if getattr(self, "_tok", None) is None:
            r = self._register()
            self.assertEqual(r.status_code, 200, r.get_json())
            self._tok = r.get_json()["token"]
        return self._tok

    def _register(self, key_id=None, public_key=None, signature=None):
        chal = self.client.post("/v1/relay/challenge").get_json()
        nonce = chal["nonce"]
        if signature is None:
            from astor_memory._internal.peer_identity import sign
            signature = sign(rc.registration_payload(
                key_id or self.key_id, public_key or self.pub, nonce),
                self.tmp)
        return self.client.post("/v1/relay/register", json={
            "peer_id": "astor:test",
            "key_id": key_id or self.key_id,
            "public_key": public_key or self.pub,
            "nonce": nonce,
            "signature": signature,
            "ttl_seconds": 600,
        })

    # --- the structural constraint (design 8.2) ---

    def test_the_relay_has_no_outbound_request_path(self):
        src = (ROOT / "astor_memory" / "relay_server.py").read_text(
            encoding="utf-8")
        # strip comments and docstrings so a mention in prose is fine
        code = re.sub(r'""".*?"""', "", src, flags=re.S)
        code = re.sub(r"#.*", "", code)
        for pattern in (r"\brequests\.", r"\bhttpx\.", r"\burllib\.request\b",
                        r"\burlopen\b", r"\bsocket\.create_connection\b"):
            self.assertIsNone(
                re.search(pattern, code),
                f"the relay must not be able to make outbound requests "
                f"(found {pattern!r}) -- it would become an open proxy")

    # --- the registration handshake ---

    def test_a_node_registers(self):
        r = self._register()
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json()["ok"])

    def test_a_bad_signature_is_refused(self):
        r = self._register(signature=base64.b64encode(b"x" * 64).decode())
        self.assertEqual(r.status_code, 401)

    def test_a_key_id_that_does_not_match_the_key_is_refused(self):
        other_pub, _ = _write_identity(self.tmp, "other.json")
        r = self._register(key_id="0123456789abcdef",
                           public_key=other_pub)
        self.assertEqual(r.status_code, 400)

    def test_a_nonce_cannot_be_reused(self):
        chal = self.client.post("/v1/relay/challenge").get_json()
        nonce = chal["nonce"]
        from astor_memory._internal.peer_identity import sign
        sig = sign(rc.registration_payload(self.key_id, self.pub, nonce),
                   self.tmp)
        body = {"peer_id": "astor:test", "key_id": self.key_id,
                "public_key": self.pub, "nonce": nonce, "signature": sig}
        self.assertEqual(
            self.client.post("/v1/relay/register", json=body).status_code, 200)
        # the second attempt has to fail even with a fresh signature
        body2 = dict(body)
        body2["signature"] = sign(
            rc.registration_payload(self.key_id, self.pub, nonce), self.tmp)
        self.assertNotEqual(
            self.client.post("/v1/relay/register", json=body2).status_code, 200)

    def test_lookup_reports_online_separately_from_found(self):
        self._register()
        r = self.client.get(f"/v1/relay/lookup/{self.key_id}")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["found"])
        self.assertTrue(body["online"])
        # the public key is not served -- it is exchanged between friends
        self.assertNotIn("public_key", body)
        self.assertEqual(
            self.client.get("/v1/relay/lookup/nobody").status_code, 404)

    # --- the message path ---

    def _sealed(self, plaintext: bytes = b"the search request") -> dict:
        return rc.seal(self.x_pub, plaintext)

    def test_a_sealed_message_is_queued_and_delivered(self):
        env = self._sealed()
        r = self.client.post("/v1/relay/send", json={
            "msg_type": "public_search",
            "payload": env,
            "to_key_id": self.key_id,
            "from_key_id": self.key_id,
        }, headers={"X-Relay-Token": self._token()})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["queued"])
        got = self.client.get("/v1/relay/poll/" + self.key_id,
                              headers={"X-Relay-Token": self._token()}).get_json()
        self.assertEqual(len(got["messages"]), 1)
        self.assertEqual(got["messages"][0]["payload"], env)

    def test_the_relay_never_holds_the_plaintext(self):
        secret = b"THE-PLAINTEXT-MUST-NOT-BE-IN-THE-RELAY"
        env = self._sealed(secret)
        self.client.post("/v1/relay/send", json={
            "msg_type": "public_search",
            "payload": env,
            "to_key_id": self.key_id,
            "from_key_id": self.key_id,
        }, headers={"X-Relay-Token": self._token()})
        # the mailbox holds the envelope and nothing else
        box = self.app.astor_state.mailboxes.get("recipient-2") or []
        blob = json.dumps(box)
        self.assertNotIn(secret.decode(), blob)
        self.assertNotIn(base64.b64encode(secret).decode(), blob)

    def test_a_plaintext_payload_is_refused(self):
        r = self.client.post("/v1/relay/send", json={
            "msg_type": "public_search",
            "payload": "just a plain string",
            "to_key_id": self.key_id,
            "from_key_id": self.key_id,
        }, headers={"X-Relay-Token": self._token()})
        self.assertEqual(r.status_code, 400)

    def test_acking_removes_the_message(self):
        env = self._sealed()
        r = self.client.post("/v1/relay/send", json={
            "msg_type": "public_search", "payload": env,
            "to_key_id": self.key_id, "from_key_id": self.key_id,
        }, headers={"X-Relay-Token": self._token()})
        msg_id = r.get_json()["msg_id"]
        got = self.client.get("/v1/relay/poll/" + self.key_id,
                              headers={"X-Relay-Token": self._token()}).get_json()
        self.assertEqual(len(got["messages"]), 1)
        ack = self.client.post("/v1/relay/ack", json={
            "key_id": self.key_id, "msg_ids": [msg_id]},
            headers={"X-Relay-Token": self._token()})
        self.assertTrue(ack.get_json()["ok"])
        got2 = self.client.get("/v1/relay/poll/" + self.key_id,
                               headers={"X-Relay-Token": self._token()}).get_json()
        self.assertEqual(got2["messages"], [])

    def test_a_message_expires(self):
        env = self._sealed()
        self.client.post("/v1/relay/send", json={
            "msg_type": "public_search", "payload": env,
            "to_key_id": self.key_id, "from_key_id": self.key_id,
        }, headers={"X-Relay-Token": self._token()})
        # age the queue past its TTL
        box = self.app.astor_state.mailboxes[self.key_id]
        box[0]["expires_at"] = time.time() - 1
        got = self.client.get("/v1/relay/poll/" + self.key_id,
                              headers={"X-Relay-Token": self._token()}).get_json()
        self.assertEqual(got["messages"], [])

    def test_the_audit_records_the_graph_but_not_the_content(self):
        env = self._sealed(b"a secret question")
        self.client.post("/v1/relay/send", json={
            "msg_type": "public_search", "payload": env,
            "to_key_id": self.key_id, "from_key_id": self.key_id,
        }, headers={"X-Relay-Token": self._token()})
        # read the row, not the file: the audit db runs in WAL mode and
        # the bytes may still be in the sidecar file
        import sqlite3
        con = sqlite3.connect(str(self.app.astor_audit.path))
        rows = con.execute(
            "SELECT from_key_id, to_key_id, msg_type FROM relay_audit"
        ).fetchall()
        con.close()
        self.assertIn((self.key_id, self.key_id, "public_search"), rows)
        # the row has no content column at all
        con = sqlite3.connect(str(self.app.astor_audit.path))
        cols = [c[1] for c in con.execute("PRAGMA table_info(relay_audit)")]
        con.close()
        self.assertEqual(sorted(cols),
                         ["from_key_id", "id", "msg_type", "to_key_id", "ts"])

    def test_the_send_rate_limit_works(self):
        env = self._sealed()
        last = None
        for _ in range(70):
            last = self.client.post("/v1/relay/send", json={
                "msg_type": "public_search", "payload": env,
                "to_key_id": self.key_id, "from_key_id": self.key_id,
            }, headers={"X-Relay-Token": self._token()})
        self.assertEqual(last.status_code, 429)


if __name__ == "__main__":
    unittest.main()
