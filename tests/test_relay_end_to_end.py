"""Two nodes, one relay, and the relay reads nothing.

v1.17.20 / relay slice 1. This is the end-to-end check: a real relay
service in a thread, a real HTTP client on each side, and a message
that goes out sealed and comes back open -- with the relay's own
memory inspected in between to prove it never held the plaintext.
"""
from __future__ import annotations

import base64
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory._internal import relay_crypto as rc  # noqa: E402
from astor_memory._internal import relay_client as client  # noqa: E402


def _write_identity(tmp: str) -> tuple[str, str]:
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
    (iddir / _PEER_ID_FILE).write_text("astor:" + base64.b64encode(
        pub[:8]).decode().rstrip("="), encoding="utf-8")
    return base64.b64encode(pub).decode(), base64.b64encode(priv).decode()


class TestRelayEndToEnd(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from astor_memory.relay_server import create_relay_app
        cls.tmp = tempfile.mkdtemp(prefix="astor_relay_e2e_")
        cls.app = create_relay_app(state_dir=cls.tmp)
        # a real socket, so the client speaks real HTTP
        from werkzeug.serving import make_server
        cls.srv = make_server("127.0.0.1", 0, cls.app, threaded=True)
        cls.port = cls.srv.server_port
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      daemon=True)
        cls.thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.a_dir = tempfile.mkdtemp(prefix="astor_node_a_")
        self.b_dir = tempfile.mkdtemp(prefix="astor_node_b_")
        self.a_pub, _ = _write_identity(self.a_dir)
        self.b_pub, _ = _write_identity(self.b_dir)
        # both sides register, and each keeps its credential
        out_a = client.register(self.base, self.a_dir)
        out_b = client.register(self.base, self.b_dir)
        client.write_config(self.base, astor_dir=self.a_dir,
                            key_id=out_a.get("key_id"),
                            token=out_a.get("token"))
        client.write_config(self.base, astor_dir=self.b_dir,
                            key_id=out_b.get("key_id"),
                            token=out_b.get("token"))
        self.a_key = rc.key_id_for(self.a_pub)
        self.b_key = rc.key_id_for(self.b_pub)
        self.b_x_pub, self.b_x_priv = rc.node_x25519_keys(self.b_dir)

    def tearDown(self):
        shutil.rmtree(self.a_dir, ignore_errors=True)
        shutil.rmtree(self.b_dir, ignore_errors=True)

    def test_a_message_goes_out_sealed_and_comes_back_open(self):
        # the sender needs the recipient's encryption key -- it comes
        # from the friend exchange, never from the relay
        secret = {"type": "public_search", "query": "the secret question"}
        client.send(self.base, to_key_id=self.b_key,
                    to_x25519_pub=self.b_x_pub, from_key_id=self.a_key,
                    message=secret, msg_type="public_search",
                    astor_dir=self.a_dir)

        # the relay's own memory holds nothing but the envelope
        blob = json.dumps(self.app.astor_state.mailboxes)
        self.assertNotIn("the secret question", blob)

        # the recipient opens it
        got = client.poll(self.base, self.b_key, self.b_x_priv,
                          astor_dir=self.b_dir)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["message"], secret)
        self.assertEqual(got[0]["from_key_id"], self.a_key)

        # and the relay drops it once we say we have it
        client.ack(self.base, self.b_key, [got[0]["msg_id"]],
                   astor_dir=self.b_dir)
        again = client.poll(self.base, self.b_key, self.b_x_priv,
                            astor_dir=self.b_dir)
        self.assertEqual(again, [])

    def test_a_message_for_someone_else_stays_closed(self):
        c_x_pub, _ = rc.generate_x25519_keypair()
        # the message goes to B, but sealed to a key nobody holds
        client.send(self.base, to_key_id=self.b_key,
                    to_x25519_pub=c_x_pub, from_key_id=self.a_key,
                    message={"secret": "not for us"}, astor_dir=self.a_dir)
        # B holds a different key, so it does not open
        got = client.poll(self.base, self.b_key, self.b_x_priv,
                          astor_dir=self.b_dir)
        self.assertEqual(got, [])

    def test_lookup_says_whether_a_peer_is_on_the_relay(self):
        client.register(self.base, self.b_dir)
        found = client.lookup(self.base, self.b_key)
        self.assertTrue(found.get("found"))
        self.assertTrue(found.get("online"))
        missing = client.lookup(self.base, "nobody-here")
        self.assertFalse(missing.get("found"))

    def test_the_client_refuses_a_plaintext_payload(self):
        # the relay will not carry it, and the client will not open it
        client.register(self.base, self.a_dir)
        import urllib.request
        req = urllib.request.Request(
            f"{self.base}/v1/relay/send",
            data=json.dumps({
                "msg_type": "public_search",
                "payload": "plain text, please",
                "to_key_id": self.b_key,
                "from_key_id": self.a_key,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            self.fail("the relay accepted a plaintext payload")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


if __name__ == "__main__":
    import urllib.error  # noqa: F401  (used in the plaintext test)
    unittest.main()
