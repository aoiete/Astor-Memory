"""The agent-plugin client must send a User-Agent and know the passcode.

Cloudflare's browser integrity check rejects urllib's default
"Python-urllib/3.x" with error 1010. The 403 never reaches the origin:
no server log, no explanation, and a caller who wrote their own
integration has no way to know that the User-Agent is the problem.

The official client is what other people copy. It had no User-Agent at
all, so every integration that started from it failed the same way.
"""
from __future__ import annotations

import importlib.util
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

TEMPLATE = (Path(__file__).resolve().parent.parent
            / "docs" / "agent_plugin_template" / "astor_memory_client.py")


@pytest.fixture(scope="module")
def client_mod():
    spec = importlib.util.spec_from_file_location(
        "astor_memory_client_tpl", TEMPLATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Handler(BaseHTTPRequestHandler):
    seen: list = []

    def _respond(self):
        _Handler.seen.append({
            "path": self.path,
            "ua": self.headers.get("User-Agent"),
            "passcode": self.headers.get("X-Astor-Passcode"),
        })
        if self.path.startswith("/v1/health"):
            body = json.dumps({"status": "ok", "facts": 42}).encode()
        elif self.path.startswith("/v1/peer/search"):
            body = json.dumps({"results": [{"fact_id": 1, "content": "x"}],
                               "count": 1, "targets": 1}).encode()
        else:
            body = json.dumps({"results": [], "count": 0}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _respond
    do_POST = _respond

    def log_message(self, *args):
        pass


@pytest.fixture()
def server():
    _Handler.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()


def test_health_request_carries_a_user_agent(client_mod, server):
    p = client_mod.AstorMemoryProvider()
    p._server_url = server
    assert p.is_available() is True
    assert _Handler.seen, "the server saw no request"
    ua = _Handler.seen[0]["ua"]
    assert ua, "no User-Agent header was sent"
    assert "Python-urllib" not in ua, (
        "the default urllib User-Agent is what Cloudflare rejects")


def test_passcode_is_sent_when_configured(client_mod, server):
    p = client_mod.AstorMemoryProvider()
    p._server_url = server
    p._passcode = "s3cret"
    p.peer_search("something")
    assert _Handler.seen
    assert _Handler.seen[-1]["passcode"] == "s3cret"


def test_no_passcode_header_when_none_configured(client_mod, server):
    p = client_mod.AstorMemoryProvider()
    p._server_url = server
    p._passcode = ""
    p.peer_search("something")
    assert _Handler.seen
    assert not _Handler.seen[-1]["passcode"]


def test_peer_search_returns_the_peer_results(client_mod, server):
    p = client_mod.AstorMemoryProvider()
    p._server_url = server
    d = p.peer_search("something", limit=5)
    assert d.get("count") == 1
    assert d.get("results")
    # The limit has to reach the server as a query parameter.
    assert "limit=5" in _Handler.seen[-1]["path"]


def test_peer_search_reports_an_error_instead_of_raising(client_mod):
    p = client_mod.AstorMemoryProvider()
    p._server_url = "http://127.0.0.1:1"  # nothing listens there
    d = p.peer_search("something")
    assert d.get("error")
    assert d.get("results") == []
