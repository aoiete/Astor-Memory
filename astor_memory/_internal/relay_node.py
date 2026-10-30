"""The node's end of the relay: ask through it, and answer what comes.

Two directions:

  send_search_via_relay  -- put a signed search request into the relay,
    addressed to a peer we cannot reach directly, and wait for the
    answer to come back the same way.

  poll_and_dispatch       -- take what the relay has for us, answer the
    search requests in it, and tell the relay we have them.

The relay carries sealed envelopes, so the search request inside is
the same document the direct path sends -- signed by the requestor's
identity key, and answered by exactly the code that answers a direct
request. The relay changes how the bytes travel, not what they mean.

One rule worth stating: the answer is sealed to the encryption key we
already hold for that peer, never to a key the request carries. If the
reply went to a key from the request, the relay could swap that key
and read the answer on the way past.
"""
from __future__ import annotations

import time
from typing import Any

from . import relay_client as client
from . import relay_crypto as rc

MSG_SEARCH = "public_search"
MSG_SEARCH_REPLY = "public_search_reply"
MSG_FRIEND_REQUEST = "friend_request"


def _my_keys(astor_dir: str | None = None) -> tuple[str, str, str]:
    """(key_id, x25519_public, x25519_private) for this node."""
    from .peer_identity import get_identity
    ident = get_identity(astor_dir)
    if not ident:
        raise client.RelayClientError("this node has no identity yet")
    key_id = rc.key_id_for(ident["public_key"])
    x_pub, x_priv = rc.node_x25519_keys(astor_dir)
    return key_id, x_pub, x_priv


def _peer_key_id(peer: dict) -> str:
    pk = peer.get("public_key") or ""
    if not pk:
        raise client.RelayClientError(
            f"peer {peer.get('peer_id')} has no signing key, so it has no "
            f"relay address")
    return rc.key_id_for(pk)


def send_search_via_relay(relay_url: str, peer: dict, request,
                          astor_dir: str | None = None,
                          timeout: float = 25.0) -> dict:
    """Ask a peer through the relay and wait for the answer.

    Returns the peer's response document, or a dict with an `error`
    key. The relay is asynchronous, so this is a send followed by a
    wait -- there is no request/response to hang on.
    """
    from .peer_relationships import _read_x25519_public_key

    my_key, _x_pub, x_priv = _my_keys(astor_dir)
    peer_x = _read_x25519_public_key(peer)
    if not peer_x:
        return {"error": "no_encryption_key",
                "detail": "this peer's encryption key is not recorded; the "
                          "relay needs it to seal the request"}
    try:
        to_key = _peer_key_id(peer)
    except client.RelayClientError as exc:
        return {"error": "no_peer_key", "detail": str(exc)}

    try:
        sent = client.send(relay_url, to_key_id=to_key,
                            to_x25519_pub=peer_x, from_key_id=my_key,
                            message=request.to_dict(), msg_type=MSG_SEARCH,
                            astor_dir=astor_dir)
    except client.RelayClientError as exc:
        return {"error": "relay_send_failed", "detail": str(exc)}

    msg_id = sent.get("msg_id")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            messages = client.poll(relay_url, my_key, x_priv, wait=2,
                                   astor_dir=astor_dir)
        except client.RelayClientError:
            messages = []
        for msg in messages:
            if msg.get("reply_to") == msg_id:
                return msg.get("message") or {}
        if not messages:
            time.sleep(0.2)
    return {"error": "relay_timeout",
            "detail": f"no answer through {relay_url} within {timeout:.0f}s"}


def poll_and_dispatch(relay_url: str, astor_dir: str | None = None,
                      wait: int = 0) -> dict:
    """Take what the relay has for us and answer what needs answering.

    Returns a summary: how many messages arrived, what was answered,
    what was dropped and why.
    """
    from .peer_relationships import (
        get_peer, _read_x25519_public_key, record_friend_request,
    )
    from .peer_search_service import answer_search_request

    # Answering a search reads the public tier, and the tier refuses to
    # open without an ACL bound. A tick that runs on its own (a cron
    # job, a CLI call) has none, so bind one -- but only when nothing
    # is bound yet: inside the server the process is already acting as
    # whoever it is, and rebinding it to admin here would change the
    # identity the whole server runs under.
    from .acl import astor_current_acl, astor_init_acl
    try:
        astor_current_acl()
    except Exception:
        astor_init_acl(actor='admin:admin', role='admin', tier='public')

    my_key, _x_pub, x_priv = _my_keys(astor_dir)
    try:
        messages = client.poll(relay_url, my_key, x_priv, wait=wait)
    except client.RelayClientError as exc:
        return {"error": "relay_unreachable", "detail": str(exc)}

    summary: dict[str, Any] = {"received": len(messages), "answered": 0,
                                "friend_requests": 0, "dropped": [],
                                "acked": 0}
    ack_ids: list[str] = []
    for msg in messages:
        msg_id = msg.get("msg_id")
        kind = msg.get("msg_type")
        body = msg.get("message") or {}
        try:
            if kind == MSG_SEARCH:
                # authorize first: whether the answer is allowed does not
                # depend on whether we happen to hold the key to seal it
                status, answer = answer_search_request(body)
                if status != 200:
                    summary["dropped"].append({
                        "msg_id": msg_id, "why": answer.get("error"),
                        "detail": answer.get("detail", "")})
                    ack_ids.append(msg_id)
                    continue
                peer_row = get_peer(body.get("requestor_peer_id") or "")
                peer_x = _read_x25519_public_key(peer_row or {})
                if not peer_x:
                    summary["dropped"].append({
                        "msg_id": msg_id,
                        "why": "no_encryption_key",
                        "detail": "we have no encryption key for this peer, "
                                  "so the answer cannot be sealed"})
                else:
                    client.send(
                        relay_url,
                        to_key_id=_peer_key_id(peer_row),
                        to_x25519_pub=peer_x,
                        from_key_id=my_key,
                        message=answer,
                        msg_type=MSG_SEARCH_REPLY,
                        reply_to=msg_id,
                        astor_dir=astor_dir,
                    )
                    summary["answered"] += 1
            elif kind == MSG_FRIEND_REQUEST:
                record_friend_request(body)
                summary["friend_requests"] += 1
            else:
                summary["dropped"].append({
                    "msg_id": msg_id, "why": "unknown_msg_type",
                    "detail": str(kind)})
            ack_ids.append(msg_id)
        except Exception as exc:  # one bad message must not stop the rest
            summary["dropped"].append({"msg_id": msg_id,
                                        "why": "handler_error",
                                        "detail": str(exc)})

    if ack_ids:
        try:
            client.ack(relay_url, my_key, ack_ids, astor_dir=astor_dir)
            summary["acked"] = len(ack_ids)
        except client.RelayClientError as exc:
            summary["dropped"].append({"why": "ack_failed",
                                        "detail": str(exc)})
    return summary
