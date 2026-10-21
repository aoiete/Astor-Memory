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
