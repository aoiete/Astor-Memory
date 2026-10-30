"""One relay going down should not take the node with it.

v1.17.25. The node keeps an ordered list of relays and tries them in
order. The peer's own endpoint comes first when it names one -- that is
the relay the two of them agreed on -- and the node's own list is the
fallback.

This test runs two relays: the first is dead (nothing listening), the
second works. The search has to go through and the answer has to come
back.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PEER_A = "astor:" + "a" * 32
PEER_B = "astor:" + "b" * 32


def _bootstrap_node(tmp: str, peer_id: str) -> dict:
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
    (iddir / _PEER_ID_FILE).write_text(peer_id, encoding="utf-8")
    return {"public_key": base64.b64encode(pub).decode(),
            "private_key": base64.b64encode(priv).decode()}


class TestRelayFailover(unittest.TestCase):

    def setUp(self):
        self.a_dir = tempfile.mkdtemp(prefix="astor_node_a_")
        self.b_dir = tempfile.mkdtemp(prefix="astor_node_b_")
        self.a_ident = _bootstrap_node(self.a_dir, PEER_A)
        self.b_ident = _bootstrap_node(self.b_dir, PEER_B)
        self._env_saved = dict(os.environ)
        os.environ["ASTOR_DIR"] = self.a_dir
        self.relays = []
        self.apps = []

    def tearDown(self):
        for srv in self.relays:
            try:
                srv.shutdown()
            except Exception:
                pass
        os.environ.clear()
        os.environ.update(self._env_saved)
        shutil.rmtree(self.a_dir, ignore_errors=True)
        shutil.rmtree(self.b_dir, ignore_errors=True)

    def _serve(self, state_dir: str) -> str:
        from astor_memory.relay_server import create_relay_app
        from werkzeug.serving import make_server
        app = create_relay_app(state_dir=state_dir)
        srv = make_server("127.0.0.1", 0, app, threaded=True)
        self.apps.append(app)
        self.relays.append(srv)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        time.sleep(0.2)
        return f"http://127.0.0.1:{srv.server_port}"

    def _register(self, node_dir: str, url: str, primary: bool = True):
        from astor_memory._internal import relay_client as client
        out = client.register(url, node_dir)
        client.write_relay_entry(url, out.get("key_id"), out.get("token"),
                                 astor_dir=node_dir, primary=primary)
        return out

    def test_a_dead_relay_falls_through_to_the_next_one(self):
        from astor_memory._internal import relay_crypto as rc
        first = self._serve(tempfile.mkdtemp(prefix="astor_relay_1_"))
        second = self._serve(tempfile.mkdtemp(prefix="astor_relay_2_"))

        # A knows both relays, the first one first; B knows the second
        self._register(self.a_dir, first, primary=True)
        self._register(self.a_dir, second, primary=False)
        self._register(self.b_dir, second)

        # and then the first relay goes away -- which is the whole
        # point: a relay that was alive when it was configured and is
        # not alive now
        self.relays[0].shutdown()
        time.sleep(0.3)
        good = second
        dead = first

        from astor_memory._internal.peer_relationships import (
            add_peer, set_peer_x25519_key, get_peer,
        )
        b_x_pub, _ = rc.node_x25519_keys(self.b_dir)
        a_x_pub, _ = rc.node_x25519_keys(self.a_dir)
        add_peer(PEER_B, kind="friend", trust=90,
                 public_key=self.b_ident["public_key"], endpoint=good,
                 metadata={"allow_search": True}, astor_dir=self.a_dir)
        set_peer_x25519_key(PEER_B, b_x_pub, astor_dir=self.a_dir)
        add_peer(PEER_A, kind="friend", trust=90,
                 public_key=self.a_ident["public_key"], endpoint=good,
                 metadata={"allow_search": True}, astor_dir=self.b_dir)
        set_peer_x25519_key(PEER_A, a_x_pub, astor_dir=self.b_dir)
        os.environ["ASTOR_DIR"] = self.a_dir

        from astor_memory._internal.peer_search import build_search_request
        from astor_memory._internal.peer_identity import get_identity
        ident = get_identity(self.a_dir)
        request = build_search_request(
            query="failover",
            requestor_peer_id=ident["peer_id"],
            requestor_pubkey=ident["public_key"],
            requestor_private_key=ident["private_key"],
        )
        peer_b = get_peer(PEER_B, astor_dir=self.a_dir)

        from astor_memory._internal.relay_node import (
            send_search_via_relay, poll_and_dispatch,
        )

        def b_tick():
            # the tick polls in a loop, like the real one: the sender
            # may still be working through a dead relay when the first
            # poll happens
            deadline = time.time() + 20
            while time.time() < deadline:
                os.environ["ASTOR_DIR"] = self.b_dir
                try:
                    summary = poll_and_dispatch(good, astor_dir=self.b_dir)
                finally:
                    os.environ["ASTOR_DIR"] = self.a_dir
                if summary.get("answered"):
                    return
                time.sleep(0.5)

        th = threading.Thread(target=b_tick)
        th.start()
        out = send_search_via_relay(dead, peer_b, request,
                                    astor_dir=self.a_dir, timeout=12)
        th.join()
        # the dead relay was tried first, the good one answered
        self.assertFalse(out.get("error"), out)

    def test_the_relay_list_keeps_its_order(self):
        from astor_memory._internal.relay_client import (
            read_relay_entries, write_relay_entry,
        )
        write_relay_entry("http://one", "k1", "t1",
                          astor_dir=self.a_dir, primary=True)
        write_relay_entry("http://two", "k2", "t2",
                          astor_dir=self.a_dir, primary=False)
        write_relay_entry("http://three", "k3", "t3",
                          astor_dir=self.a_dir, primary=False)
        entries = read_relay_entries(self.a_dir)
        self.assertEqual([e["base_url"] for e in entries],
                         ["http://one", "http://two", "http://three"])
        # and the credential travels with its relay
        self.assertEqual(entries[1]["token"], "t2")

    def test_making_a_new_primary_keeps_the_old_one_in_the_list(self):
        from astor_memory._internal.relay_client import (
            read_relay_entries, write_relay_entry,
        )
        write_relay_entry("http://one", "k1", "t1",
                          astor_dir=self.a_dir, primary=True)
        write_relay_entry("http://two", "k2", "t2",
                          astor_dir=self.a_dir, primary=True)
        entries = read_relay_entries(self.a_dir)
        self.assertEqual([e["base_url"] for e in entries],
                         ["http://two", "http://one"])


if __name__ == "__main__":
    unittest.main()
