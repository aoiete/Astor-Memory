"""astor_memory/_internal/peer_connect.py — one-command peer wiring.

The problem this solves: a peer sitting behind NAT cannot be reached,
so the only direction that works is outbound from their side. The
existing flow makes that awkward in three ways:

  - `am peer friend code` prints a bare peer_id, but the design says a
    friend code is `astor:<32hex>@<addr>` -- the address is the part
    you hand to someone, and it was missing.
  - `am peer allow-search <id>` only opens. There is no way to close
    it from the CLI, so a mistake is only fixable through SQL.
  - `am peer friend add` demands a peer_id you already know plus a
    pubkey you had to copy by hand. A stranger should not have to do
    that.

This module makes the whole thing one command each side:

    # on the reachable node
    am peer open-search --allow --peer-id '*'

    # on the node behind NAT
    am peer connect https://peer.example.com

`connect` reads the other side's identity from its own endpoint,
records it, and (optionally) makes the first friend request. Nothing
here needs the other node to be able to reach back.

Everything is read-only against the remote side: connect fetches a
public identity document, and read issues a signed search. Neither
writes to the remote node.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_TIMEOUT = 20


class ConnectError(Exception):
    """A failure that the operator needs to read, not a stack trace."""


def _get_json(url: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    req = urllib.request.Request(url, headers={
        # Some deployments sit behind a proxy that rejects the default
        # urllib agent, and a bare "Python-urllib" is a signal we do
        # not need to send.
        "User-Agent": "astor-peer-connect/1.0",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        raise ConnectError("HTTP %s from %s %s" % (e.code, url, body))
    except urllib.error.URLError as e:
        raise ConnectError("cannot reach %s: %s" % (url, e.reason))
    except Exception as e:  # socket timeouts land here too
        raise ConnectError("cannot reach %s: %s" % (url, e))
    try:
        return json.loads(raw)
    except Exception:
        raise ConnectError("response from %s is not JSON" % url)


# Addresses that are never a peer. The lookup endpoint takes a URL from
# the operator, and the server fetches it -- so without this, the
# server can be pointed at the cloud metadata service or at a service
# on the local network. The endpoint is behind the passcode, so the
# operator could already reach those from a shell; this keeps the
# accidental case (a pasted link that happens to be something else)
# from turning into a request to an unintended host.
_BLOCKED_HOSTS = frozenset({
    "169.254.169.254",      # cloud instance metadata
    "metadata.google.internal",
    "metadata.goog",
    "100.100.100.200",      # alibaba cloud metadata
})


def _validate_endpoint(endpoint: str) -> str:
    """Normalise an endpoint and refuse the hosts that are never peers."""
    endpoint = (endpoint or "").strip().rstrip("/")
    if not endpoint:
        raise ConnectError("no endpoint given")
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "https://" + endpoint
    host = endpoint.split("://", 1)[1].split("/", 1)[0].split("@")[-1]
    # strip the port
    if host.startswith("["):
        host = host.split("]", 1)[0].lstrip("[")
    else:
        host = host.split(":", 1)[0]
    if host.lower() in _BLOCKED_HOSTS:
        raise ConnectError("%s is not a peer host" % host)
    return endpoint


def fetch_identity(endpoint: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Read a node's public identity.

    Tries the dedicated endpoint first, then falls back to the peer
    status route, because a node that predates the dedicated endpoint
    is still a legitimate node.
    """
    endpoint = _validate_endpoint(endpoint)

    errors = []
    for path in ("/v1/peer/identity", "/v1/peer/status"):
        url = endpoint + path
        try:
            data = _get_json(url, timeout=timeout)
        except ConnectError as e:
            errors.append(str(e))
            continue
        ident = _extract_identity(data)
        if ident:
            ident["endpoint"] = endpoint
            return ident
        errors.append("%s returned no peer_id" % url)
    raise ConnectError("; ".join(errors))


def _extract_identity(data: dict) -> dict:
    """Pull peer_id / public_key out of whichever shape came back."""
    if not isinstance(data, dict):
        return {}
    # A flat identity document.
    pid = data.get("peer_id")
    if pid:
        return {
            "peer_id": pid,
            "public_key": data.get("public_key") or data.get("pubkey") or "",
            "first_run_ts": data.get("first_run_ts") or "",
        }
    # A status-shaped response.
    inner = data.get("identity") if isinstance(data.get("identity"), dict) else None
    if inner and inner.get("peer_id"):
        return {
            "peer_id": inner["peer_id"],
            "public_key": inner.get("public_key") or "",
            "first_run_ts": inner.get("first_run_ts") or "",
        }
    # A {peer_id: {...}} map.
    for key, val in (data.items() if isinstance(data, dict) else []):
        if key.startswith("astor:") and isinstance(val, dict):
            return {
                "peer_id": key,
                "public_key": val.get("public_key") or "",
                "first_run_ts": val.get("first_run_ts") or "",
            }
    return {}


def search_peer_open(endpoint: str, query: str, peer_id: str, public_key: str,
                     private_key: str, topic: str | None = None,
                     limit: int = 10, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Query a peer's public tier through the open-search path.

    This is the path that works when the peer has opened their public
    tier to signature-verified nodes but has never met us: POST to
    /v1/peer/recv with msg_type=public_search.

    Two shapes of the same idea live in this codebase, and they are not
    interchangeable:

      - open search  -- POST /v1/peer/recv, msg_type=public_search.
        Needs the operator to have run `am peer open-search`. Does not
        need friendship, does not need trust, does not need an
        allow_search flag. The signature is verified with the rekey
        verifier, so the message must carry the rekey field set
        (old_peer_id / new_peer_id / new_public_key / timestamp /
        signature / signer_pubkey) as well as the query fields.

      - friend PPS -- GET /v1/peer/public_search?req=...
        Needs the requester to be a known friend with trust >= 50 and
        allow_search set on our side.

    We try open search first because it is the broader grant: a node
    that opened its public tier wants to be read.
    """
    from .peer_identity import sign
    import datetime as _dt

    endpoint = (endpoint or "").rstrip("/")
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "https://" + endpoint

    now = _dt.datetime.now(_dt.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    # The rekey verifier rebuilds exactly these bytes, so the signature
    # has to cover the same thing on both sides.
    payload = (
        peer_id.encode("utf-8") + b"\n" +
        peer_id.encode("utf-8") + b"\n" +
        public_key.encode("utf-8") + b"\n" +
        now.encode("utf-8")
    )
    sig = sign(payload)
    msg = {
        "msg_type": "public_search",
        "msg": {
            "sender_peer_id": peer_id,
            "query": query,
            "limit": int(limit),
            "topic": topic,
            "ts": now,
            # The rekey field set -- the verifier requires all six.
            "old_peer_id": peer_id,
            "new_peer_id": peer_id,
            "new_public_key": public_key,
            "timestamp": now,
            "signature": sig,
            "signer_pubkey": public_key,
        },
    }
    return _post_json(endpoint + "/v1/peer/recv", msg, timeout=timeout)


def search_peer(endpoint: str, query: str, peer_id: str, public_key: str,
                private_key: str, topic: str | None = None, limit: int = 10,
                timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run a signed public-tier search against a peer.

    Tries the open-search path first, then falls back to the friend
    PPS path. Both are read-only: neither writes to the remote node.
    """
    open_err = ''
    open_detail = ''
    try:
        out = search_peer_open(endpoint, query, peer_id, public_key,
                               private_key, topic=topic, limit=limit,
                               timeout=timeout)
        if not out.get("error"):
            return out
        open_err = out.get("error")
        open_detail = out.get("detail") or ""
    except ConnectError as e:
        open_err = str(e)

    friend_err = ''
    friend_detail = ''
    try:
        out = search_peer_friend(endpoint, query, peer_id, public_key,
                                 private_key, topic=topic, limit=limit,
                                 timeout=timeout)
        if not out.get("error"):
            return out
        friend_err = out.get("error")
        friend_detail = out.get("detail") or ""
    except ConnectError as e:
        friend_err = str(e)

    # Keep the remote's own explanation. The bare code
    # "open_search_closed" says nothing to the operator; the detail
    # says "this node does not accept unsolicited queries", which is
    # the difference between "I did something wrong" and "they have
    # it closed". The previous version also raised inside the try and
    # caught it with its own except, so the friend-path message was
    # never the one that reached the caller.
    detail = friend_detail or open_detail
    parts = [p for p in (friend_err, open_err) if p]
    raise ConnectError("%s%s" % (
        "; ".join(parts),
        " -- " + detail if detail else ""))


def search_peer_friend(endpoint: str, query: str, peer_id: str, public_key: str,
                       private_key: str, topic: str | None = None,
                       limit: int = 10, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Query a peer's public tier through the friend PPS path.

    The receiver must have us as a known friend with trust >= 50 and
    allow_search set. The request travels as a base64url `req` query
    parameter carrying a signed PeerSearchRequest.
    """
    from .peer_identity import sign
    import datetime as _dt

    endpoint = (endpoint or "").rstrip("/")
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "https://" + endpoint

    now = _dt.datetime.now(_dt.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")
    topic_str = topic or ""
    # The canonical payload is the same bytes the receiver rebuilds --
    # peer_id, query, topic, limit, timestamp, in that order.
    payload = (
        peer_id.encode("utf-8") + b"\n" +
        query.encode("utf-8") + b"\n" +
        topic_str.encode("utf-8") + b"\n" +
        str(int(limit)).encode("utf-8") + b"\n" +
        now.encode("utf-8")
    )
    sig = sign(payload)
    req = {
        "requestor_peer_id": peer_id,
        "requestor_pubkey": public_key,
        "query": query,
        "topic": topic,
        "limit": int(limit),
        "timestamp": now,
        "signature": sig,
    }
    raw = base64.urlsafe_b64encode(json.dumps(req).encode("utf-8")).decode("ascii")
    url = endpoint + "/v1/peer/public_search?req=" + urllib.parse.quote(raw)
    return _get_json(url, timeout=timeout)


def _post_json(url: str, payload: dict, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """POST JSON and return the decoded body.

    A 403 is a real answer here -- it means the peer is refusing --
    so the body is returned rather than raised, and the caller decides.
    """
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "astor-peer-connect/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        try:
            body = json.loads(raw)
        except Exception:
            body = {"error": "http_%s" % e.code, "detail": raw[:200]}
        # A refusal is a legitimate answer, not a transport failure.
        if isinstance(body, dict) and "error" in body:
            return body
        return {"error": "http_%s" % e.code, "detail": raw[:200]}
    except urllib.error.URLError as e:
        raise ConnectError("cannot reach %s: %s" % (url, e.reason))
    except Exception as e:
        raise ConnectError("cannot reach %s: %s" % (url, e))
    try:
        return json.loads(raw)
    except Exception:
        raise ConnectError("response from %s is not JSON" % url)
