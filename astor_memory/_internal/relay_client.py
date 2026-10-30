"""The node's side of the relay.

The node talks to the relay; the relay talks to nobody. This module
holds the client's half of the protocol:

  register  -- get a challenge, sign it, prove you hold the key
  send      -- seal the message to the recipient's key, hand the
               envelope to the relay
  poll      -- take the envelopes addressed to us, open them
  ack       -- tell the relay we have them, so it can drop them

The inner message is the existing peer protocol message -- signed with
the node's ed25519 identity key. The relay never sees that signature
either: it is inside the sealed envelope.

Unlike the relay, this module is allowed to make outbound requests. It
is the node: it has to be able to reach the relay, and the relay has
to be able to reach it.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import relay_crypto as rc

DEFAULT_TIMEOUT = 30

# Where this node keeps the address of the relay it uses. One file, in
# the node's own runtime directory, so a node that does not use a relay
# simply does not have one.
_CONFIG_NAME = "relay.json"


def config_path(astor_dir: str | None = None) -> Path:
    base = astor_dir or os.environ.get("ASTOR_DIR") or os.path.expanduser("~/.astor")
    return Path(base) / _CONFIG_NAME


def read_config(astor_dir: str | None = None) -> dict:
    """The relay this node uses, or {} when it does not use one."""
    path = config_path(astor_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def read_token(astor_dir: str | None = None) -> str:
    """This node's credential at the relay."""
    return str(read_config(astor_dir).get("token") or "")


def read_relay_entries(astor_dir: str | None = None) -> list[dict]:
    """The relays this node uses, in the order to try them.

    One relay is the usual case. The list is there for the case where
    one relay is down or blocked and another is not: the node tries
    them in order and uses the first that answers, so a single relay
    going away does not take the node's reachability with it.
    """
    cfg = read_config(astor_dir)
    out: list[dict] = []
    if cfg.get("base_url"):
        out.append({"base_url": str(cfg["base_url"]).rstrip("/"),
                    "key_id": cfg.get("key_id") or "",
                    "token": cfg.get("token") or ""})
    for entry in cfg.get("relays") or []:
        if isinstance(entry, dict) and entry.get("base_url"):
            out.append({"base_url": str(entry["base_url"]).rstrip("/"),
                        "key_id": entry.get("key_id") or "",
                        "token": entry.get("token") or ""})
    return out


def write_relay_entry(base_url: str, key_id: str, token: str,
                      astor_dir: str | None = None,
                      primary: bool = True) -> dict:
    """Remember a relay this node uses.

    primary=True makes it the first one tried; otherwise it joins the
    list behind the current one.
    """
    path = config_path(astor_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = read_config(astor_dir)
    entry = {"base_url": (base_url or "").rstrip("/"),
             "key_id": key_id or "", "token": token or ""}
    others = [r for r in (cfg.get("relays") or [])
              if isinstance(r, dict) and r.get("base_url")
              and r.get("base_url") != entry["base_url"]]
    if primary:
        old_primary = cfg.get("base_url")
        old_token = cfg.get("token")
        old_key = cfg.get("key_id")
        if old_primary and old_primary != entry["base_url"]:
            others = [r for r in others
                      if r.get("base_url") != old_primary]
            others.insert(0, {"base_url": old_primary,
                               "key_id": old_key or "",
                               "token": old_token or ""})
        cfg["base_url"] = entry["base_url"]
        cfg["key_id"] = entry["key_id"]
        cfg["token"] = entry["token"]
    else:
        others = [r for r in others if r.get("base_url") != entry["base_url"]]
        others.append(entry)
    cfg["relays"] = others
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    return cfg


def write_config(base_url: str, astor_dir: str | None = None,
                 **extra) -> dict:
    """Remember the relay this node uses."""
    path = config_path(astor_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = read_config(astor_dir)
    data["base_url"] = (base_url or "").rstrip("/")
    data.update(extra)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    return data


class RelayClientError(Exception):
    """The relay said no, or the network did."""


def _url(relay_base: str, path: str) -> str:
    base = (relay_base or "").rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise RelayClientError(
            f"relay base must be an http(s) url, got {relay_base!r}")
    return f"{base}{path}"


def _get_json(url: str, timeout: int = DEFAULT_TIMEOUT,
              token: str = "") -> dict:
    headers = {"X-Relay-Token": token} if token else {}
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:200]
        raise RelayClientError(f"relay returned {exc.code}: {body}") from exc
    except Exception as exc:
        raise RelayClientError(f"relay unreachable: {exc}") from exc


def _post_json(url: str, payload: dict, timeout: int = DEFAULT_TIMEOUT,
               token: str = "") -> dict:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Relay-Token"] = token
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:200]
        raise RelayClientError(f"relay returned {exc.code}: {body}") from exc
    except Exception as exc:
        raise RelayClientError(f"relay unreachable: {exc}") from exc


# --- register ---------------------------------------------------------

def register(relay_base: str, astor_dir: str | None = None,
             ttl_seconds: int = 3600, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Prove to the relay that we hold the key behind our key_id."""
    from .peer_identity import get_identity, sign
    identity = get_identity(astor_dir)
    if not identity:
        raise RelayClientError("this node has no identity yet")
    public_key = identity["public_key"]
    key_id = rc.key_id_for(public_key)

    # POST, not GET: asking for a challenge creates one
    challenge = _post_json(_url(relay_base, "/v1/relay/challenge"), {},
                            timeout)
    nonce = challenge.get("nonce")
    if not nonce:
        raise RelayClientError("relay did not return a nonce")

    signature = sign(rc.registration_payload(key_id, public_key, nonce),
                      astor_dir)
    body = _post_json(_url(relay_base, "/v1/relay/register"), {
        "peer_id": identity.get("peer_id") or "",
        "key_id": key_id,
        "public_key": public_key,
        "nonce": nonce,
        "signature": signature,
        "ttl_seconds": ttl_seconds,
    }, timeout)
    if not body.get("ok"):
        raise RelayClientError(
            f"relay refused the registration: {body.get('error')}")
    body["key_id"] = key_id
    return body


# --- send -------------------------------------------------------------

def send(relay_base: str, to_key_id: str, to_x25519_pub: str,
         from_key_id: str, message: Any, msg_type: str = "public_search",
         reply_to: str | None = None,
         timeout: int = DEFAULT_TIMEOUT,
         astor_dir: str | None = None, token: str = "") -> dict:
    """Seal a message for a peer and hand it to the relay."""
    plaintext = json.dumps(message, ensure_ascii=False).encode("utf-8")
    envelope = rc.seal(to_x25519_pub, plaintext)
    body = _post_json(_url(relay_base, "/v1/relay/send"), {
        "msg_type": msg_type,
        "payload": envelope,
        "to_key_id": to_key_id,
        "from_key_id": from_key_id,
        "reply_to": reply_to,
    }, timeout, token=token or read_token(astor_dir))
    if not body.get("ok"):
        raise RelayClientError(
            f"relay refused the message: {body.get('error')}")
    return body


# --- poll / ack -------------------------------------------------------

def poll(relay_base: str, key_id: str, x25519_priv: str,
         wait: int = 0, timeout: int | None = None,
         astor_dir: str | None = None, token: str = "") -> list[dict]:
    """Take the messages addressed to us and open them.

    A message that does not open is dropped, not returned: an
    envelope we cannot decrypt is either not for us or was tampered
    with, and either way it is not a message.
    """
    if timeout is None:
        timeout = DEFAULT_TIMEOUT + wait + 5
    body = _get_json(
        _url(relay_base, f"/v1/relay/poll/{key_id}") + f"?wait={int(wait)}",
        timeout, token=token or read_token(astor_dir))
    out: list[dict] = []
    for msg in body.get("messages") or []:
        envelope = msg.get("payload")
        if not rc.is_sealed(envelope):
            # a plaintext payload is a downgrade attempt
            continue
        try:
            raw = rc.open_envelope(envelope, x25519_priv)
        except rc.RelayCryptoError:
            continue
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            parsed = {"raw": raw.decode("utf-8", "replace")}
        out.append({
            "msg_id": msg.get("msg_id"),
            "msg_type": msg.get("msg_type"),
            "from_key_id": msg.get("from_key_id"),
            "reply_to": msg.get("reply_to"),
            "received_at": msg.get("received_at"),
            "message": parsed,
        })
    return out


def ack(relay_base: str, key_id: str, msg_ids: list[str],
        timeout: int = DEFAULT_TIMEOUT,
        astor_dir: str | None = None, token: str = "") -> dict:
    """Tell the relay we have these, so it can drop them."""
    return _post_json(_url(relay_base, "/v1/relay/ack"), {
        "key_id": key_id, "msg_ids": list(msg_ids)}, timeout,
        token=token or read_token(astor_dir))


def lookup(relay_base: str, key_id: str,
           timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Is this peer on the relay right now?"""
    try:
        return _get_json(_url(relay_base, f"/v1/relay/lookup/{key_id}"),
                         timeout)
    except RelayClientError:
        return {"found": False}


def node_keys(astor_dir: str | None = None) -> tuple[str, str]:
    """This node's (public, private) x25519 pair."""
    return rc.node_x25519_keys(astor_dir)
