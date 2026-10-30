"""Two nodes behind NAT, finding each other through a relay.

v1.17.21. This is the case the relay exists for: neither node can be
reached directly, so the only path between them is the relay.

What has to come out is a real answer: the searching node gets the
other node's public facts, and only the shareable ones. And the two
rules that make the relay worth having have to hold while it is in
the middle: a personal fact does not travel, and a peer that has not
opted in is not answered at all.

Each node has its own runtime directory -- its own identity, its own
public tier, its own peer list. The receiver reads the peer list from
the environment, the way the server does, so the receiving side runs
with ASTOR_DIR pointed at its own directory.
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
    """An identity for one node."""
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


class TestRelaySearchEndToEnd(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from astor_memory.relay_server import create_relay_app
        cls.relay_tmp = tempfile.mkdtemp(prefix="astor_relay_search_")
        app = create_relay_app(state_dir=cls.relay_tmp)
        from werkzeug.serving import make_server
        cls.srv = make_server("127.0.0.1", 0, app, threaded=True)
        cls.port = cls.srv.server_port
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.srv.serve_forever,
                                      daemon=True)
        cls.thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.relay_tmp, ignore_errors=True)

    def setUp(self):
        self.a_dir = tempfile.mkdtemp(prefix="astor_node_a_")
        self.b_dir = tempfile.mkdtemp(prefix="astor_node_b_")
        self.a_ident = _bootstrap_node(self.a_dir, PEER_A)
        self.b_ident = _bootstrap_node(self.b_dir, PEER_B)
        self._register(self.a_dir)
        self._register(self.b_dir)
        self._env_saved = dict(os.environ)
        os.environ["ASTOR_DIR"] = self.a_dir

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_saved)
        shutil.rmtree(self.a_dir, ignore_errors=True)
        shutil.rmtree(self.b_dir, ignore_errors=True)

    # --- helpers ---

    def _seed_fact(self, node_dir: str, content: str, visibility: str,
                   kind: str = "lesson") -> int:
        """Put a fact in the node's public tier.

        A personal fact cannot be written into the public tier through
        the store -- that guard is the point of v1.17.15 -- so a
        personal row there has to be put there the way the 509 rows
        that predate the guard were: directly in the table. That is
        exactly the state the read-side filter has to survive.
        """
        import sqlite3
        from astor_memory._internal.acl import astor_init_acl
        from astor_memory.bus.store import astor_bus
        os.environ["ASTOR_DIR"] = node_dir
        astor_init_acl(actor="admin:admin", role="admin", tier="public")
        bus = astor_bus(tier="public", user_id=None)
        ev = bus.append_event(namespace="t", agent_id="t", source="t",
                              action="write", content=content, metadata={})
        cand = bus.insert_candidate(event_id=ev, namespace="t",
                                    content=content, kind=kind,
                                    importance=0.5, metadata={})
        fid = bus.promote_candidate(
            candidate_id=cand, promoted_by="t", user_id="admin",
            tier="public", scope_type="long_term", visibility="commons",
            verdict="settled")
        if visibility != "commons":
            db = (Path(node_dir) / "public" / "memory"
                  / "astor_bus_public.db")
            con = sqlite3.connect(str(db), timeout=15)
            con.execute(
                "UPDATE memory_canonical SET visibility = ? WHERE id = ?",
                (visibility, fid))
            con.commit()
            con.close()
        return fid

    def _be_friends(self, owner_dir: str, other: dict, other_peer_id: str,
                    other_x_pub: str, allow: bool = True) -> dict:
        """Record the other node as a peer we may search."""
        from astor_memory._internal.peer_relationships import (
            add_peer, set_peer_x25519_key, get_peer,
        )
        meta = {"allow_search": True} if allow else {}
        add_peer(other_peer_id, kind="friend", trust=90,
                 public_key=other["public_key"],
                 endpoint=self.base, metadata=meta,
                 astor_dir=owner_dir)
        set_peer_x25519_key(other_peer_id, other_x_pub,
                            astor_dir=owner_dir)
        return get_peer(other_peer_id, astor_dir=owner_dir)

    def _register(self, node_dir: str):
        """Register a node with the relay and keep its credential."""
        from astor_memory._internal import relay_client as client
        out = client.register(self.base, node_dir)
        client.write_config(self.base, astor_dir=node_dir,
                            key_id=out.get('key_id'),
                            token=out.get('token'))
        return out

    def _request(self, node_dir: str, query: str):
        from astor_memory._internal.peer_search import build_search_request
        from astor_memory._internal.peer_identity import get_identity
        ident = get_identity(node_dir)
        return build_search_request(
            query=query,
            requestor_peer_id=ident["peer_id"],
            requestor_pubkey=ident["public_key"],
            requestor_private_key=ident["private_key"],
        )

    def _b_tick(self, delay: float = 0.5):
        """Run the receiver's tick in its own runtime directory.

        Returns (thread, box) -- the box holds the tick's summary, so
        a test can look at what the receiver did with the message.
        """
        box = {}

        def _run():
            time.sleep(delay)
            os.environ["ASTOR_DIR"] = self.b_dir
            try:
                from astor_memory._internal.relay_node import (
                    poll_and_dispatch,
                )
                box["summary"] = poll_and_dispatch(self.base,
                                                   astor_dir=self.b_dir)
            except Exception as exc:
                box["error"] = str(exc)
            finally:
                os.environ["ASTOR_DIR"] = self.a_dir
        th = threading.Thread(target=_run)
        th.start()
        return th, box

    # --- the tests ---

    def test_a_search_goes_through_the_relay_and_comes_back(self):
        from astor_memory._internal import relay_crypto as rc
        # B has one shareable fact and one personal one
        self._seed_fact(self.b_dir, "the relay carries a lesson about seals",
                        "commons")
        self._seed_fact(self.b_dir, "my personal diary entry about seals",
                        "personal")
        os.environ["ASTOR_DIR"] = self.a_dir

        b_x_pub, _ = rc.node_x25519_keys(self.b_dir)
        a_x_pub, _ = rc.node_x25519_keys(self.a_dir)
        # A knows B (and may search it); B knows A (and may search it)
        peer_b = self._be_friends(self.a_dir, self.b_ident, PEER_B, b_x_pub)
        self._be_friends(self.b_dir, self.a_ident, PEER_A, a_x_pub)
        os.environ["ASTOR_DIR"] = self.a_dir

        request = self._request(self.a_dir,
                                 "relay carries a lesson about seals")
        th, box = self._b_tick(delay=0.4)

        from astor_memory._internal.relay_node import send_search_via_relay
        out = send_search_via_relay(self.base, peer_b, request,
                                    astor_dir=self.a_dir, timeout=20)
        th.join()
        self.assertFalse(out.get("error"), out)
        results = out.get("results") or []
        self.assertTrue(results, f"the relay path returned nothing: {out}")
        contents = " ".join(r.get("content", "") for r in results)
        self.assertIn("relay carries a lesson", contents)
        self.assertNotIn("personal diary", contents)

    def test_a_peer_who_did_not_opt_in_is_not_answered(self):
        from astor_memory._internal import relay_crypto as rc
        self._seed_fact(self.b_dir, "a lesson B never agreed to share",
                        "commons")
        os.environ["ASTOR_DIR"] = self.a_dir
        b_x_pub, _ = rc.node_x25519_keys(self.b_dir)
        peer_b = self._be_friends(self.a_dir, self.b_ident, PEER_B, b_x_pub)
        # B does not allow A to search
        self._be_friends(self.b_dir, self.a_ident, PEER_A, b_x_pub,
                         allow=False)

        request = self._request(self.a_dir,
                                 "a lesson B never agreed to share")
        from astor_memory._internal.relay_node import (
            send_search_via_relay, poll_and_dispatch,
        )
        # A waits; B's tick refuses the request and says why
        th, box = self._b_tick(delay=0.3)
        out = send_search_via_relay(self.base, peer_b, request,
                                    astor_dir=self.a_dir, timeout=4)
        th.join()
        # A got no answer, and B says why it did not answer
        self.assertTrue(out.get("error"), out)
        summary = box.get("summary") or {}
        self.assertNotIn("error", summary, summary)
        self.assertEqual(summary.get("answered"), 0, summary)
        self.assertTrue(summary.get("dropped"), summary)
        self.assertEqual(summary["dropped"][0]["why"], "search_not_allowed")

    def test_the_receive_tick_answers_a_permitted_search(self):
        from astor_memory._internal import relay_crypto as rc
        self._seed_fact(self.b_dir, "a shareable lesson about the relay",
                        "commons")
        os.environ["ASTOR_DIR"] = self.a_dir
        b_x_pub, _ = rc.node_x25519_keys(self.b_dir)
        a_x_pub, _ = rc.node_x25519_keys(self.a_dir)
        peer_b = self._be_friends(self.a_dir, self.b_ident, PEER_B, b_x_pub)
        self._be_friends(self.b_dir, self.a_ident, PEER_A, a_x_pub)
        os.environ["ASTOR_DIR"] = self.a_dir

        request = self._request(self.a_dir,
                                 "a shareable lesson about the relay")
        th, box = self._b_tick(delay=0.4)
        from astor_memory._internal.relay_node import send_search_via_relay
        out = send_search_via_relay(self.base, peer_b, request,
                                    astor_dir=self.a_dir, timeout=20)
        th.join()
        self.assertFalse(out.get("error"), out)
        self.assertTrue(out.get("results"))


if __name__ == "__main__":
    unittest.main()
