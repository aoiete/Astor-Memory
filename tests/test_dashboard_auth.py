"""tests/test_dashboard_auth.py — the passcode gate.

The gate exists because astor can be reached from the public internet
(a tunnel points at it) and the dashboard had no authentication. These
tests pin the behaviour that came out of that incident, including the
three ways the first implementation was wrong:

  1. Judging "is this local?" by remote address alone. A Cloudflare
     tunnel on this machine forwards to 127.0.0.1, so a request from
     the public internet arrives looking local. The host header is part
     of the test.
  2. Gating the dashboard page itself. The page IS the passcode
     prompt; gating it locks the operator out with no way to enter the
     code. The shell is served, the data is not.
  3. Exempting the whole /v1/peer/ namespace. /v1/peer/list returns
     the relationship graph. The exemption is an explicit list.

They also pin the ordering requirement: the ACL binding hook must run
before the auth gate, or the health endpoint answers "astor_acl not
initialized" instead of its own payload.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


@pytest.fixture()
def auth(tmp_path, monkeypatch):
    """dashboard_auth with an isolated ASTOR_DIR."""
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    mod = importlib.import_module("astor_memory._internal.dashboard_auth")
    # The module reads ASTOR_DIR at call time, so reloading is not
    # needed -- but the module-level _open_to cache is shared, so
    # reset the parts these tests touch.
    yield mod


# --------------------------------------------------------------- passcode

def test_passcode_is_generated_and_persisted(auth):
    pc = auth.get_passcode()
    assert len(pc) >= 12
    # A second read returns the same value -- it is stored, not minted
    # fresh each time.
    assert auth.get_passcode() == pc
    assert auth.passcode_path().exists()
    # And it lives in the runtime directory, not the repo.
    assert "astor-memory" not in str(auth.passcode_path())


def test_check_passcode_rejects_wrong_value(auth):
    pc = auth.get_passcode()
    assert auth.check_passcode(pc) is True
    assert auth.check_passcode("wrong") is False
    assert auth.check_passcode("") is False
    assert auth.check_passcode(None) is False


def test_set_passcode_rejects_short_values(auth):
    with pytest.raises(ValueError):
        auth.set_passcode("abc")
    with pytest.raises(ValueError):
        auth.set_passcode("")


def test_set_passcode_revokes_existing_sessions(auth):
    auth.get_passcode()
    token = auth.issue_session()
    assert auth.valid_session(token) is True
    auth.set_passcode("a-brand-new-passcode")
    assert auth.valid_session(token) is False


# --------------------------------------------------------------- sessions

def test_session_round_trip(auth):
    token = auth.issue_session()
    assert auth.valid_session(token) is True
    assert auth.valid_session("not-a-real-token") is False
    assert auth.valid_session("") is False
    auth.revoke_session(token)
    assert auth.valid_session(token) is False


def test_expired_session_is_rejected(auth):
    token = auth.issue_session()
    # Rewrite the timestamp to be older than the TTL.
    p = auth._token_path()
    data = json.loads(p.read_text(encoding="utf-8"))
    data[token]["ts"] = 0.0
    p.write_text(json.dumps(data), encoding="utf-8")
    assert auth.valid_session(token) is False


# --------------------------------------------------------- local vs remote

def test_is_local_request_uses_the_host_header(auth):
    """The tunnel case: a public request arrives with a loopback
    remote address but a public Host header."""
    assert auth.is_local_request("127.0.0.1", "127.0.0.1:7803") is True
    assert auth.is_local_request("127.0.0.1", "localhost:7803") is True
    assert auth.is_local_request("127.0.0.1", "") is True
    # Same address, public host -- this is the request through the
    # tunnel, and it must NOT be treated as local.
    assert auth.is_local_request("127.0.0.1", "astor.example.com") is False
    assert auth.is_local_request("203.0.113.5", "127.0.0.1:7803") is False


def test_is_local_request_handles_ipv6_mapped(auth):
    assert auth.is_local_request("::ffff:127.0.0.1", "127.0.0.1:7803") is True
    assert auth.is_local_request("::1", "[::1]:7803") is True


# ------------------------------------------------------ authorization rules

class _H(dict):
    def get(self, k, default=None):
        return dict.get(self, k, default)


def test_local_requests_are_allowed(auth):
    ok, why = auth.request_is_authorized(_H(), "127.0.0.1", "GET",
                                          "/v1/dashboard", host="127.0.0.1:7803")
    assert ok is True


def test_public_request_without_credentials_is_refused(auth):
    ok, why = auth.request_is_authorized(_H(), "127.0.0.1", "GET",
                                          "/v1/dashboard", host="astor.example.com")
    assert ok is False
    assert why == "need_passcode"


def test_public_request_with_session_token_is_allowed(auth):
    auth.get_passcode()
    token = auth.issue_session()
    ok, _ = auth.request_is_authorized(
        _H({auth.HDR_TOKEN: token}), "127.0.0.1", "GET", "/v1/dashboard",
        host="astor.example.com")
    assert ok is True


def test_public_request_with_passcode_header_is_allowed(auth):
    pc = auth.get_passcode()
    ok, _ = auth.request_is_authorized(
        _H({auth.HDR_PASSCODE: pc}), "127.0.0.1", "GET", "/v1/dashboard",
        host="astor.example.com")
    assert ok is True


def test_write_requests_need_credentials(auth):
    ok, _ = auth.request_is_authorized(_H(), "127.0.0.1", "POST",
                                        "/v1/write", host="astor.example.com")
    assert ok is False


def test_peer_protocol_reads_are_exempt_but_the_graph_is_not(auth):
    """Only the protocol surface is open. /v1/peer/list returns the
    relationship graph and must not be exempt."""
    for path in ("/v1/peer/public_search", "/v1/peer/identity",
                 "/v1/peer/health", "/v1/peer/open-search"):
        ok, _ = auth.request_is_authorized(_H(), "127.0.0.1", "GET", path,
                                            host="astor.example.com")
        assert ok is True, path
    for path in ("/v1/peer/list", "/v1/peer/audit",
                 "/v1/peer/quarantine/list"):
        ok, _ = auth.request_is_authorized(_H(), "127.0.0.1", "GET", path,
                                            host="astor.example.com")
        assert ok is False, path


def test_peer_write_endpoints_are_not_exempt(auth):
    for path in ("/v1/peer/add", "/v1/peer/open-search"):
        ok, _ = auth.request_is_authorized(_H(), "127.0.0.1", "POST", path,
                                            host="astor.example.com")
        assert ok is False, path


# ------------------------------------------------------------- rate limit

def test_repeated_failures_are_throttled(auth):
    client = "203.0.113.9"
    for _ in range(auth._MAX_FAILS):
        allowed, _info = auth.check_attempt(client)
        assert allowed is True
        auth.record_failure(client)
    allowed, info = auth.check_attempt(client)
    assert allowed is False
    assert "retry_after_seconds" in info


def test_success_clears_the_failure_count(auth):
    client = "203.0.113.10"
    auth.record_failure(client)
    auth.record_success(client)
    allowed, _ = auth.check_attempt(client)
    assert allowed is True


# ------------------------------------------------------------ server wiring

def test_dashboard_page_is_served_without_a_passcode(auth):
    """The page IS the prompt. Gating it locks the operator out."""
    from astor_memory.server import create_app
    app = create_app()
    client = app.test_client()
    res = client.get("/dashboard/")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert "auth-gate" in body
    assert "auth-passcode" in body


def test_dashboard_routes_point_at_the_right_handlers():
    """A decorator inserted next to another route can silently steal
    it. That happened: /dashboard/ was served by the auth handler."""
    from astor_memory.server import create_app
    app = create_app()
    endpoints = {str(r): r.endpoint for r in app.url_map.iter_rules()}
    assert endpoints.get("/dashboard/") == "dashboard_page"
    assert endpoints.get("/") == "dashboard_page"
    assert endpoints.get("/dashboard/index.html") == "dashboard_page"
    assert endpoints.get("/v1/auth/passcode") == "auth_passcode"
    assert endpoints.get("/v1/auth/session") == "auth_session_status"
    assert endpoints.get("/v1/auth/logout") == "auth_logout"
    assert endpoints.get("/v1/dashboard") == "dashboard"


def test_acl_hook_runs_before_the_auth_gate():
    """The gate must not run first: the ACL binding happens in the
    other hook, and returning early from the gate would leave health
    answering 'astor_acl not initialized'."""
    src = (REPO / "astor_memory" / "server.py").read_text(encoding="utf-8")
    acl = src.find("def _astor_bind_request_acl")
    gate = src.find("def _astor_dashboard_auth_gate")
    assert acl > 0 and gate > 0
    assert acl < gate, "the auth gate must be registered after the ACL hook"


def test_static_assets_are_served():
    from astor_memory.server import create_app
    app = create_app()
    client = app.test_client()
    assert client.get("/dashboard/app.js").status_code == 200
    assert client.get("/dashboard/style.css").status_code == 200


# ---------------------------------------------------------- endpoint safety

def test_endpoint_validation_blocks_metadata_hosts():
    """The lookup endpoint makes the server fetch a URL the operator
    typed. The cloud metadata address is never a peer."""
    from astor_memory._internal import peer_connect as pc
    for host in ("http://169.254.169.254", "http://100.100.100.200/latest",
                 "http://metadata.google.internal"):
        with pytest.raises(pc.ConnectError):
            pc._validate_endpoint(host)


def test_endpoint_validation_normalizes():
    from astor_memory._internal import peer_connect as pc
    assert pc._validate_endpoint("example.com") == "https://example.com"
    assert pc._validate_endpoint("http://example.com/") == "http://example.com"
    assert pc._validate_endpoint("http://example.com:8080") == "http://example.com:8080"


def test_lookup_endpoint_requires_a_url():
    from astor_memory.server import create_app
    app = create_app()
    client = app.test_client()
    res = client.get("/v1/peer/lookup")
    assert res.status_code == 400


def test_lookup_endpoint_reports_an_unreachable_host():
    from astor_memory.server import create_app
    app = create_app()
    client = app.test_client()
    res = client.get("/v1/peer/lookup?url=http://127.0.0.1:1")
    assert res.status_code in (502, 504)
    body = res.get_data(as_text=True)
    assert "lookup_failed" in body
