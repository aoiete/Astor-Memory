"""One click that says where a peer search got to.

The peer panel shows theory: is the trust high enough, is there an
endpoint. The operator reports "I cannot search them", and then four
gates have to be checked one at a time by hand -- the address, the
connection, the signature, the authorization.

This runs the real search path instead of a ping, because a port that
answers is not a peer that answers: the signature and the
authorization still have to pass. The result is one stage name that
says which gate stopped it.

The address is never taken from the caller. The endpoint comes from
the peer row in the database, so there is no "connect to this URL"
input and therefore no SSRF to defend against. Redirects are not
followed either, and the timeout is short.

The query carries a unique token, so "matched nothing" can be told
apart from "did not arrive": a broad query would match something by
accident and make a broken link look alive.
"""
from __future__ import annotations

import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# The receiver recognises this prefix and leaves the statistics alone.
# It does NOT get out of the rate limit -- a peer that lets tests
# through without counting them can be used to hammer it for free.
TEST_QUERY_PREFIX = "__peertest__"

DEFAULT_TIMEOUT = 3.0
MAX_PER_PEER_PER_MINUTE = 6


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is a way to make us connect somewhere else."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _hits(peer_id: str) -> list[float]:
    return _HITS.setdefault(peer_id, [])


_HITS: dict[str, list[float]] = {}


def _rate_ok(peer_id: str) -> bool:
    now = time.time()
    hits = [t for t in _hits(peer_id) if t > now - 60]
    if len(hits) >= MAX_PER_PEER_PER_MINUTE:
        _HITS[peer_id] = hits
        return False
    hits.append(now)
    _HITS[peer_id] = hits
    return True


def _stage_from_error(code: str) -> tuple[str, str]:
    """Map the peer's refusal to a stage the operator can act on."""
    code = (code or "").lower()
    if code in ("bad_signature", "invalid_request", "bad_req_encoding",
                "missing_field"):
        return ("sign", "签名格式不对 —— 对方版本可能不兼容")
    if code in ("search_not_allowed",):
        return ("authorize", "对方还没允许你搜它（要在对方那侧打勾）")
    if code in ("trust_below_threshold",):
        return ("trust", "信任度不够")
    if code in ("unknown_peer", "peer_blacklisted"):
        return ("authorize", "对方那边还没有这条 peer 记录")
    if code in ("peer_rate_limit_exceeded",):
        return ("authorize", "对方那侧的搜索额度用完了")
    return ("connect", f"对方返回 {code}")


def _test_query() -> str:
    """A query that cannot match anything by accident."""
    return f"{TEST_QUERY_PREFIX}{secrets.token_hex(8)}"


def test_peer(peer_id: str, mode: str = "direct",
              astor_dir: str | None = None,
              timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Run a real search against one peer and say where it got to."""
    from .peer_relationships import get_peer, _read_connect_mode
    from .peer_identity import get_identity, init_identity

    started = time.time()
    out: dict[str, Any] = {
        "ok": False, "stage": "connect", "error": None, "detail": "",
        "latency_ms": 0, "relay_online": None, "peer_online": None,
        "mode": mode,
    }

    row = get_peer(peer_id, astor_dir=astor_dir)
    if not row:
        out["detail"] = f"没有这个 peer: {peer_id}"
        out["error"] = "unknown_peer"
        out["latency_ms"] = int((time.time() - started) * 1000)
        return out

    if not _rate_ok(peer_id):
        out["stage"] = "connect"
        out["error"] = "rate_limited"
        out["detail"] = f"这个 peer 一分钟最多测 {MAX_PER_PEER_PER_MINUTE} 次"
        out["latency_ms"] = int((time.time() - started) * 1000)
        return out

    # the mode picks which set of addresses on the peer row to use; it
    # never supplies an address of its own
    if mode == "relay":
        result = _test_via_relay(row, timeout=timeout)
    else:
        result = _test_direct(row, timeout=timeout)

    out.update(result)
    out["latency_ms"] = int((time.time() - started) * 1000)
    out["mode"] = mode
    return out


def _test_direct(row: dict, timeout: float) -> dict:
    """The direct path: a signed search request to the peer's endpoint."""
    from .peer_identity import get_identity, init_identity
    from .peer_search import _endpoints_for, build_search_request

    endpoints = _endpoints_for(row)
    if not endpoints:
        return {"stage": "connect", "error": "no_endpoint",
                "detail": "这个 peer 没有可用的地址", "ok": False}
    endpoint = endpoints[0]
    try:
        ident = get_identity() or init_identity()
    except Exception as exc:
        return {"stage": "sign", "error": "no_identity",
                "detail": f"本机没有身份: {exc}", "ok": False}

    request = build_search_request(
        query=_test_query(),
        requestor_peer_id=ident["peer_id"],
        requestor_pubkey=ident["public_key"],
        requestor_private_key=ident["private_key"],
        limit=1,
    )
    url = endpoint + "/v1/peer/public_search?req=" + urllib.parse.quote(
        request.to_query_string())
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "astor-memory/peer-test",
    }, method="GET")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = {"error": f"http_{exc.code}"}
        code = body.get("error") or f"http_{exc.code}"
        stage, detail = _stage_from_error(code)
        return {"stage": stage, "error": code, "detail": detail, "ok": False}
    except Exception as exc:
        return {"stage": "connect", "error": "unreachable",
                "detail": f"连不上 {endpoint}: {exc}", "ok": False}

    err = body.get("error")
    if err:
        stage, detail = _stage_from_error(err)
        return {"stage": stage, "error": err, "detail": detail, "ok": False}
    results = body.get("results") or []
    if not results:
        return {"stage": "match", "error": None,
                "detail": "通了 —— 只是没匹配到（不是故障）", "ok": True}
    return {"stage": "ok", "error": None,
            "detail": f"{len(results)} 条结果", "ok": True}


def _test_via_relay(row: dict, timeout: float) -> dict:
    """The relay path: the same search, sealed, through the relay."""
    from .peer_relationships import _read_x25519_public_key
    from .relay_client import lookup, read_config
    from .relay_node import send_search_via_relay
    from .peer_identity import get_identity, init_identity
    from .peer_search import build_search_request

    relay_url = (row.get("endpoint") or "").rstrip("/")
    if not relay_url:
        return {"stage": "connect", "error": "no_endpoint",
                "detail": "这个 peer 没有 relay 地址", "ok": False}

    try:
        ident = get_identity() or init_identity()
    except Exception as exc:
        return {"stage": "sign", "error": "no_identity",
                "detail": f"本机没有身份: {exc}", "ok": False}

    from .relay_crypto import key_id_for
    my_key = key_id_for(ident["public_key"])
    online = lookup(relay_url, my_key).get("found")
    if not online:
        return {"stage": "connect", "error": "relay_unreachable",
                "detail": f"relay 上没有本机: {relay_url}", "ok": False,
                "relay_online": False}

    request = build_search_request(
        query=_test_query(),
        requestor_peer_id=ident["peer_id"],
        requestor_pubkey=ident["public_key"],
        requestor_private_key=ident["private_key"],
        limit=1,
    )
    answer = send_search_via_relay(relay_url, row, request, timeout=timeout)
    if answer.get("error"):
        code = answer.get("error")
        if code in ("no_encryption_key", "relay_timeout"):
            return {"stage": "connect", "error": code,
                    "detail": answer.get("detail", ""), "ok": False,
                    "relay_online": True}
        stage, detail = _stage_from_error(code)
        return {"stage": stage, "error": code, "detail": detail, "ok": False,
                "relay_online": True}
    results = answer.get("results") or []
    if not results:
        return {"stage": "match", "error": None,
                "detail": "通了 —— 只是没匹配到（不是故障）", "ok": True,
                "relay_online": True}
    return {"stage": "ok", "error": None, "detail": f"{len(results)} 条结果",
            "ok": True, "relay_online": True}


def test_query_prefix() -> str:
    return TEST_QUERY_PREFIX
