"""The passcode limiter must not be rotatable.

The failed-attempt bucket was keyed on CF-Connecting-IP / X-Forwarded-For
whenever the header was present. Both are caller-controlled, so a caller
that reaches the server directly could change the header and get a fresh
bucket every time -- the passcode could then be guessed without limit.

The forwarding headers are only trusted from loopback: that is where the
tunnel lands, and the tunnel overwrites CF-Connecting-IP with the real
client.
"""
from __future__ import annotations

import unittest

from astor_memory._internal import dashboard_auth as dauth


class TestClientKeyCannotBeRotated(unittest.TestCase):

    def test_a_direct_caller_cannot_rotate_the_bucket(self):
        # the LAN address is the same for both requests; only the
        # forwarded header changes
        k1 = dauth.client_key({"CF-Connecting-IP": "1.2.3.4"}, "192.168.1.50")
        k2 = dauth.client_key({"CF-Connecting-IP": "5.6.7.8"}, "192.168.1.50")
        self.assertEqual(k1, k2,
                         "a caller that reaches the server directly must not "
                         "get a new rate-limit bucket by changing a header")
        self.assertEqual(k1, "192.168.1.50")

    def test_x_forwarded_for_is_also_not_trusted_from_a_direct_caller(self):
        k1 = dauth.client_key({"X-Forwarded-For": "9.9.9.9"}, "192.168.1.50")
        k2 = dauth.client_key({"X-Forwarded-For": "8.8.8.8"}, "192.168.1.50")
        self.assertEqual(k1, k2)

    def test_the_tunnel_still_keys_on_the_real_client(self):
        # the tunnel connects from 127.0.0.1 and Cloudflare sets the
        # header itself -- this is the case the header exists for
        key = dauth.client_key({"CF-Connecting-IP": "1.2.3.4"}, "127.0.0.1")
        self.assertEqual(key, "1.2.3.4")

    def test_a_forwarded_list_takes_the_first_entry(self):
        key = dauth.client_key(
            {"X-Forwarded-For": "1.2.3.4, 5.6.7.8"}, "127.0.0.1")
        self.assertEqual(key, "1.2.3.4")

    def test_no_header_means_the_remote_address(self):
        self.assertEqual(dauth.client_key({}, "192.168.1.50"), "192.168.1.50")


if __name__ == "__main__":
    unittest.main()
