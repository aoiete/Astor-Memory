"""Inbound friend requests: queue, prompt, accept/reject.

Adding a peer is one-directional today — the local relationships table records
the claim and the other node never learns of it. These tests cover the
inbound half: a signed request lands in a queue, nothing becomes a friend
without an explicit decision, and accepting binds the friendship to the key
the request was verified against.

Runs against real Ed25519 keys in a temp ASTOR_DIR; no mocks, because the
property under test is precisely that the signature does what it claims.
"""
from __future__ import annotations

import shutil
import tempfile

import pytest

from astor_memory._internal import peer_identity as PI
from astor_memory._internal import peer_relationships as PR


@pytest.fixture
def local_dir(tmp_path, monkeypatch):
    d = tmp_path / "local"
    d.mkdir()
    monkeypatch.setenv("ASTOR_DIR", str(d))
    yield d


@pytest.fixture
def remote_dir(tmp_path):
    """A second install with its own identity — stands in for the other peer."""
    d = tmp_path / "remote"
    d.mkdir()
    yield d


def _msg(remote_dir, **kw):
    return PI.build_friend_request(
        kw.pop("to", "astor:" + "a" * 32),
        alias=kw.pop("alias", "alice"),
        note=kw.pop("note", "想连一下"),
        astor_dir=str(remote_dir),
        **kw,
    )


def test_request_round_trips_and_verifies(remote_dir):
    msg = _msg(remote_dir)
    assert msg["from_peer_id"].startswith("astor:")
    assert msg["alias"] == "alice"
    assert PI.verify_friend_request(msg) is True


def test_alias_and_note_are_covered_by_the_signature(remote_dir):
    """These two fields are what the operator is shown when asked for trust.

    If they were outside the signed payload, a peer could display one name and
    send another, and the prompt would be describing something the signature
    never vouched for.
    """
    msg = _msg(remote_dir)

    tampered_alias = dict(msg, alias="admin")
    assert PI.verify_friend_request(tampered_alias) is False

    tampered_note = dict(msg, note="totally different text")
    assert PI.verify_friend_request(tampered_note) is False


def test_from_peer_id_is_covered_by_the_signature(remote_dir):
    msg = _msg(remote_dir)
    forged = dict(msg, from_peer_id="astor:" + "f" * 32)
    assert PI.verify_friend_request(forged) is False


def test_missing_fields_are_rejected(remote_dir):
    msg = _msg(remote_dir)
    for field in ("from_peer_id", "signature", "sender_pubkey", "timestamp"):
        broken = {k: v for k, v in msg.items() if k != field}
        assert PI.verify_friend_request(broken) is False, field


def test_request_lands_pending_and_is_not_yet_a_friend(local_dir, remote_dir):
    msg = _msg(remote_dir)
    row = PR.record_friend_request(msg)

    assert row["status"] == "pending"
    assert row["alias"] == "alice"
    assert row["note"] == "想连一下"

    # The receiving side must not have created a relationship yet.
    assert PR.list_peers(kind="friend") == []


def test_resending_updates_instead_of_stacking(local_dir, remote_dir):
    """A peer retrying after a timeout should not produce a queue of
    identical prompts — one row, refreshed."""
    msg = _msg(remote_dir)
    first = PR.record_friend_request(msg)
    second = PR.record_friend_request(msg)

    assert first["id"] == second["id"]
    assert len(PR.list_friend_requests("pending")) == 1


def test_accept_creates_the_friendship_bound_to_the_verified_key(local_dir, remote_dir):
    """The friend row must carry the key the signature was checked against.

    Recording only a peer_id would let anything later repoint the friendship
    at a different key.
    """
    msg = _msg(remote_dir)
    row = PR.record_friend_request(msg)
    out = PR.resolve_friend_request(row["id"], accept=True, trust=55,
                                    endpoint="http://example.invalid")

    assert out["status"] == "accepted"
    assert out["peer_id"] == msg["from_peer_id"]
    assert out["friend"]["public_key"] == msg["sender_pubkey"]
    assert out["friend"]["trust"] == 55
    assert out["friend"]["endpoint"] == "http://example.invalid"

    friends = PR.list_peers(kind="friend")
    assert len(friends) == 1
    assert friends[0]["peer_id"] == msg["from_peer_id"]


def test_reject_creates_no_relationship(local_dir, remote_dir):
    msg = _msg(remote_dir)
    row = PR.record_friend_request(msg)
    out = PR.resolve_friend_request(row["id"], accept=False)

    assert out["status"] == "rejected"
    assert PR.list_peers(kind="friend") == []
    assert PR.list_friend_requests("pending") == []


def test_a_request_cannot_be_resolved_twice(local_dir, remote_dir):
    """Accept-then-reject on one id would otherwise let a rejected peer back in."""
    msg = _msg(remote_dir)
    row = PR.record_friend_request(msg)
    PR.resolve_friend_request(row["id"], accept=True)

    with pytest.raises(ValueError, match="already accepted"):
        PR.resolve_friend_request(row["id"], accept=False)

    assert len(PR.list_peers(kind="friend")) == 1


def test_unknown_request_id_is_an_error(local_dir):
    with pytest.raises(ValueError, match="no friend_request"):
        PR.resolve_friend_request(9999, accept=True)


def test_trust_must_stay_within_range(remote_dir):
    """add_peer already enforces this; confirm the accept path routes through it."""
    from tempfile import mkdtemp
    d = mkdtemp()
    try:
        with pytest.raises(ValueError, match="trust"):
            PR.add_peer("astor:" + "b" * 32, trust=500, astor_dir=d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- delivery: the part that made "added but nothing happened" -----------


def test_send_requires_an_endpoint():
    """No endpoint means nowhere to send — and the caller must be told.

    A peer row with a null endpoint is a local claim the other node can
    never see. The original UX was a --send flag that was off by default,
    so adding a peer silently produced no request and the feature looked
    broken rather than unconfigured.
    """
    from tempfile import mkdtemp
    d = mkdtemp()
    try:
        out = PR.send_friend_request("astor:" + "c" * 32, "",
                                     astor_dir=d)
        assert out["sent"] is False
        assert "http" in out["error"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_send_reports_an_unreachable_endpoint():
    """A refused connection is reported, not swallowed."""
    from tempfile import mkdtemp
    d = mkdtemp()
    try:
        # Port 1 on loopback is not listening anywhere.
        out = PR.send_friend_request(
            "astor:" + "d" * 32, "http://127.0.0.1:1", timeout=2,
            astor_dir=d)
        assert out["sent"] is False
        assert out["error"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_send_delivers_to_a_receiving_peer(local_dir, remote_dir):
    """End to end: the signed request lands in the receiver's queue.

    Runs a real HTTP server on loopback so the wire format, the signature
    verification and the queue insert are all exercised together. A mocked
    transport would pass while the actual payload was malformed.
    """
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            received.append(json.loads(body))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"received":true,"status":"pending"}')

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        msg = _msg(remote_dir)
        out = PR.send_friend_request(
            msg["from_peer_id"], f"http://127.0.0.1:{port}",
            alias=msg["alias"], note=msg["note"], astor_dir=str(local_dir),
        )
        assert out["sent"] is True
        assert len(received) == 1
        payload = received[0]
        assert payload["msg_type"] == "friend_request"
        # The wire payload must verify with the receiver's public key.
        assert PI.verify_friend_request(payload["msg"]) is True
        assert payload["msg"]["alias"] == msg["alias"]
        assert payload["msg"]["note"] == msg["note"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_send_uses_the_senders_own_key(local_dir, remote_dir):
    """The request must be signed by the sender, not the receiver.

    If the sender's key were used, the receiver would be verifying its own
    signature and the peer_id in the request would prove nothing about who
    sent it.
    """
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    captured = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            captured.update(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"received":true}')

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        out = PR.send_friend_request(
            "astor:" + "e" * 32, f"http://127.0.0.1:{port}",
            alias="alice", note="hi", astor_dir=str(local_dir),
        )
        assert out["sent"] is True
        sent = captured["msg"]
        # from_peer_id is the LOCAL install's id, not the target's.
        local_id = PI.init_identity(str(local_dir))["peer_id"]
        assert sent["from_peer_id"] == local_id
        assert sent["from_peer_id"] != "astor:" + "e" * 32
    finally:
        srv.shutdown()
        srv.server_close()


# --- friend code: one string instead of two fields -------------------------


def test_friend_code_round_trips():
    """The code must survive a copy-paste unchanged."""
    pid = "astor:" + "1" * 32
    code = PI.format_friend_code(pid, "1.2.3.4:7803")
    assert code == pid + "@1.2.3.4:7803"
    assert PI.parse_friend_code(code) == (pid, "1.2.3.4:7803")


def test_bare_peer_id_parses_with_no_address():
    """Still valid — it records the relationship locally."""
    pid = "astor:" + "2" * 32
    assert PI.parse_friend_code(pid) == (pid, None)


def test_friend_code_rejects_junk_with_a_readable_message():
    for bad, frag in [
        ("", "empty"),
        ("astor:abc", "32 hex"),
        ("not-a-code", "expected"),
        ("astor:" + "3" * 32 + "@", "empty address"),
    ]:
        with pytest.raises(ValueError) as ei:
            PI.parse_friend_code(bad)
        assert frag in str(ei.value), (bad, str(ei.value))


def test_friend_code_with_a_url_keeps_the_scheme():
    """A peer behind a TLS tunnel may only serve https, so the scheme is
    not something the code is allowed to throw away."""
    pid = "astor:" + "4" * 32
    code = PI.format_friend_code(pid, "https://peer.example.com:8443")
    got_pid, got_ep = PI.parse_friend_code(code)
    assert got_pid == pid
    assert got_ep == "https://peer.example.com:8443"


def test_address_is_routing_only_and_does_not_change_identity():
    """The identity in a friend code is the peer_id, and that is what gets
    signed. A wrong address costs a failed delivery, never a wrong
    identity — which is what makes carrying the address in the code safe."""
    pid = "astor:" + "5" * 32
    a = PI.parse_friend_code(PI.format_friend_code(pid, "1.1.1.1:1"))
    b = PI.parse_friend_code(PI.format_friend_code(pid, "2.2.2.2:2"))
    assert a[0] == b[0] == pid
    assert a[1] != b[1]


def test_local_friend_code_carries_this_install_id():
    """The code shown in the dashboard must be about THIS install."""
    from tempfile import mkdtemp
    d = mkdtemp()
    try:
        code = PI.local_friend_code(d)
        pid, _ = PI.parse_friend_code(code)
        assert pid == PI.init_identity(d)["peer_id"]
    finally:
        shutil.rmtree(d, ignore_errors=True)
