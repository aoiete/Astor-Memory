"""A page the operator visits must not be able to write to astor.

The passcode gate trusts loopback, because the CLI, the plugin and the
cron jobs all talk to 127.0.0.1:7803. But a browser on that machine
can be made to POST to 127.0.0.1 by any page the operator visits, and
`/v1/write` parses JSON whatever the content type says -- so a
`text/plain` body is a simple request: no preflight, no CORS check.

DNS rebinding was already covered (a request with a foreign Host is
not local), but a page that targets 127.0.0.1 directly sends a
loopback Host, and it was accepted.

A browser always sends Origin on a cross-origin POST; our own clients
send none. So a foreign Origin on a state-changing method is refused.
"""
from __future__ import annotations

import unittest

from astor_memory._internal import dashboard_auth as dauth


def _headers(**kw):
    return {k.replace("_", "-"): v for k, v in kw.items()}


class TestCsrfOriginGate(unittest.TestCase):

    def test_a_foreign_origin_on_a_write_is_refused(self):
        allowed, reason = dauth.request_is_authorized(
            _headers(Origin="http://evil.example"), "127.0.0.1", "POST",
            "/v1/write", host="127.0.0.1:7803")
        self.assertFalse(allowed)
        self.assertEqual(reason, "csrf_origin")

    def test_a_foreign_origin_on_a_read_is_not_blocked(self):
        # A read leaks nothing on its own, and the passcode still
        # applies to a non-local caller.
        allowed, reason = dauth.request_is_authorized(
            _headers(Origin="http://evil.example"), "127.0.0.1", "GET",
            "/v1/read", host="127.0.0.1:7803")
        self.assertTrue(allowed, reason)

    def test_our_own_clients_send_no_origin_and_still_work(self):
        allowed, reason = dauth.request_is_authorized(
            {}, "127.0.0.1", "POST", "/v1/write", host="127.0.0.1:7803")
        self.assertTrue(allowed, reason)

    def test_the_dashboards_own_origin_is_allowed(self):
        allowed, reason = dauth.request_is_authorized(
            _headers(Origin="http://127.0.0.1:7803"), "127.0.0.1", "POST",
            "/v1/write", host="127.0.0.1:7803")
        self.assertTrue(allowed, reason)

    def test_the_tunnel_origin_is_not_treated_as_csrf(self):
        # Through the tunnel the Host is the public name, so the request
        # is not local and the passcode applies. What matters here is
        # that the CSRF rule did not fire: the Origin IS the one we
        # serve.
        allowed, reason = dauth.request_is_authorized(
            _headers(Origin="https://memory.example.com"), "127.0.0.1",
            "POST", "/v1/write", host="memory.example.com")
        self.assertFalse(allowed)
        self.assertEqual(reason, "need_passcode")

    def test_a_null_origin_is_refused(self):
        # Some browsers send "null" for sandboxed / file:// pages. It
        # is not our origin, so a state-changing request with it is a
        # forgery -- and it is refused before the passcode is even
        # considered.
        allowed, reason = dauth.request_is_authorized(
            _headers(Origin="null"), "203.0.113.9", "POST", "/v1/write",
            host="memory.example.com")
        self.assertFalse(allowed)
        self.assertEqual(reason, "csrf_origin")


if __name__ == "__main__":
    unittest.main()
