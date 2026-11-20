"""A peer must not be able to make us buffer an unbounded response.

The peer is remote and untrusted. `resp.read()` with no limit lets a
configured peer stream as many bytes as it likes into this process's
memory; the 20s timeout bounds time, not size. The fix reads at most
MAX_RESPONSE_BYTES + 1 bytes and refuses anything larger instead of
truncating it into a body that might still parse as something else.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# One byte past the cap is what the client is allowed to ask for: enough
# to notice the overflow, not enough to swallow the whole body.
OVERSIZE_BYTES = 4096


@pytest.fixture(autouse=True)
def isolated_astor_dir(tmp_path, monkeypatch):
    """Never touch the live runtime."""
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path / "astor"))


class _PeerHandler(BaseHTTPRequestHandler):
    """Answers every path with `payload`."""

    payload = b""

    def _reply(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        try:
            self.wfile.write(self.payload)
        except (BrokenPipeError, ConnectionResetError):
            # The client read its cap and hung up. That is the fix
            # working; without it the client would read all of this.
            pass

    do_GET = _reply
    do_POST = _reply

    def log_message(self, *args):
        pass


@pytest.fixture
def peer_server():
    """A peer that answers with one oversized JSON document."""
    from astor_memory._internal import peer_connect as pc
    payload = json.dumps({
        "peer_id": "astor:big",
        "public_key": "pk",
        "pad": "A" * (pc.MAX_RESPONSE_BYTES + OVERSIZE_BYTES),
    }).encode("utf-8")
    handler = type("H", (_PeerHandler,), {"payload": payload})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


class _FakeResp:
    """Records the size each read() was asked for."""

    def __init__(self, body):
        self._body = body
        self.read_sizes = []

    def read(self, size=None):
        self.read_sizes.append(size)
        return self._body if size is None else self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_an_oversized_peer_response_is_refused(peer_server):
    """The whole identity fetch has to fail, not fall back to a
    truncated body that still looks like an identity."""
    from astor_memory._internal import peer_connect as pc
    with pytest.raises(pc.ConnectError) as exc:
        pc.fetch_identity(peer_server)
    assert "larger than" in str(exc.value)


def test_an_oversized_peer_response_is_refused_on_post(peer_server):
    """The POST path reads a peer body too."""
    from astor_memory._internal import peer_connect as pc
    with pytest.raises(pc.ConnectError) as exc:
        pc._post_json(peer_server + "/v1/peer/recv", {"msg_type": "public_search"})
    assert "larger than" in str(exc.value)


def test_the_read_asks_for_at_most_max_plus_one(monkeypatch):
    """The cap is enforced by how much we ask for, not by what we do
    with what we got."""
    import urllib.request
    from astor_memory._internal import peer_connect as pc
    fake = _FakeResp(b"x" * (pc.MAX_RESPONSE_BYTES + OVERSIZE_BYTES))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: fake)
    with pytest.raises(pc.ConnectError):
        pc._get_json("http://peer.example.com/v1/peer/identity")
    assert fake.read_sizes == [pc.MAX_RESPONSE_BYTES + 1]


def test_a_normal_peer_response_still_works():
    """The cap must not break the ordinary case."""
    from astor_memory._internal import peer_connect as pc
    body = json.dumps({"peer_id": "astor:small", "public_key": "pk"}).encode()
    assert pc._extract_identity(json.loads(body))["peer_id"] == "astor:small"
    assert len(body) < pc.MAX_RESPONSE_BYTES
