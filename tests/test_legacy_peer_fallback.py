"""A peer that predates the signed nonce must still be reachable.

The signed payload gained a nonce in v1.17.64. A peer that rebuilds
the old five-part payload cannot verify a six-part signature, so the
search failed with bad_signature against every peer that had not been
upgraded yet. The client now retries in the legacy form.

The fake peer below verifies only the five-part payload, exactly like
a peer older than v1.17.64.
"""
from __future__ import annotations

import base64
import json
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from astor_memory._internal import peer_connect
from astor_memory._internal.peer_identity import init_identity, verify
from astor_memory._internal.peer_search import canonical_payload_bytes


class _OldPeer(BaseHTTPRequestHandler):
    """Verifies only the five-part payload, like astor before v1.17.64."""

    seen_nonces = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        raw = (params.get("req") or [""])[0]
        if not raw:
            self.send_response(400)
            self.end_headers()
            return
        req = json.loads(base64.urlsafe_b64decode(raw + "==="))
        _OldPeer.seen_nonces.append(req.get("nonce"))
        legacy = canonical_payload_bytes(
            req["requestor_peer_id"], req["query"], req.get("topic"),
            int(req["limit"]), req["timestamp"])
        if not verify(legacy, req["signature"], req["requestor_pubkey"]):
            body = json.dumps({"error": "bad_signature"}).encode()
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps({"results": [{"fact_id": 1, "content": "ok"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestLegacyPeerFallback(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _OldPeer)
        cls.port = cls.srv.server_address[1]
        cls.t = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_a_peer_that_only_knows_the_old_payload_is_reachable(self):
        ident = init_identity()
        _OldPeer.seen_nonces.clear()
        out = peer_connect.search_peer_friend(
            endpoint=f"http://127.0.0.1:{self.port}",
            query="something", peer_id=ident["peer_id"],
            public_key=ident["public_key"],
            private_key=ident["private_key"], limit=5)
        self.assertNotIn(
            "error", out,
            "the search against a peer that only verifies the legacy "
            "payload failed: %r" % out)
        self.assertEqual(out.get("results", [{}])[0].get("content"), "ok")

    def test_the_first_attempt_still_carries_a_nonce(self):
        """The current form is tried first -- only old peers pay the
        second request."""
        ident = init_identity()
        _OldPeer.seen_nonces.clear()
        peer_connect.search_peer_friend(
            endpoint=f"http://127.0.0.1:{self.port}",
            query="something", peer_id=ident["peer_id"],
            public_key=ident["public_key"],
            private_key=ident["private_key"], limit=5)
        self.assertTrue(_OldPeer.seen_nonces[0],
                        "the first request did not carry a nonce")


    def test_the_peer_test_command_falls_back_too(self):
        """`am peer test` builds its request through build_search_request,
        which is a different path from search_peer_friend."""
        from astor_memory._internal import peer_test
        _OldPeer.seen_nonces.clear()
        out = peer_test._test_direct(
            {"peer_id": "astor:legacy", "endpoint": f"http://127.0.0.1:{self.port}"},
            timeout=10)
        self.assertTrue(
            out.get("ok"),
            "the peer test failed against a peer that only verifies the "
            "legacy payload: %r" % out)


if __name__ == "__main__":
    unittest.main()
