"""Two secret shapes used to slip past the public-memory gate.

1. A URL whose secret parameter is not the FIRST query slot:
   `?a=1&api_key=SECRET` -- the old pattern only matched after `?`, so
   the whole URL read as clean text.

2. A PEM private key: the old pattern matched the header line only.
   After redaction the header was gone and the base64 body -- the
   actual key -- was stored in the public tier.
"""
from __future__ import annotations

import unittest

from astor_memory.nest.memory_defense import scan_facts


def _kinds(text: str) -> set:
    return {m.name for m in scan_facts(text)}


class TestSecretQueryParameterInAnySlot(unittest.TestCase):

    def test_a_secret_in_the_first_slot_is_caught(self):
        self.assertIn(
            "url_secret_param",
            _kinds("https://example.com/api?api_key=SECRETVALUE123"))

    def test_a_secret_in_a_later_slot_is_caught(self):
        self.assertIn(
            "url_secret_param",
            _kinds("https://example.com/api?a=1&api_key=SECRETVALUE123"))

    def test_a_secret_in_the_last_slot_is_caught(self):
        self.assertIn(
            "url_secret_param",
            _kinds("https://example.com/api?a=1&b=2&token=SECRETVALUE123"))

    def test_a_normal_url_is_not_flagged(self):
        self.assertNotIn(
            "url_secret_param",
            _kinds("https://example.com/api?a=1&b=2&page=3"))


class TestPemPrivateKeyIsCaughtWhole(unittest.TestCase):

    KEY = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAy8Dbv8prpJ/0kKhlGeJYozo2t60EG8L0561g13R29LvMR5hy\n"
        "vG4LL0Mh5i2FToP9j+AmQ7Qq0X0e3x8rGz0X0e3x8rGz0X0e3x8rGz0X0e3x8rGz\n"
        "-----END RSA PRIVATE KEY-----"
    )

    def test_a_pem_key_is_caught(self):
        self.assertIn("private_key_pem", _kinds(self.KEY))

    def test_the_body_of_the_key_is_covered_too(self):
        """The header alone is not enough -- the body is the secret."""
        kinds = _kinds(self.KEY)
        self.assertIn("private_key_pem", kinds)
        # the match has to span the body, not stop at the header
        m = [m for m in scan_facts(self.KEY)
             if m.name == "private_key_pem"]
        self.assertTrue(m)
        self.assertGreater(
            m[0].end - m[0].start, len("-----BEGIN RSA PRIVATE KEY-----"),
            "the pattern stopped at the header; the base64 body survived")

    def test_a_truncated_pem_key_is_caught(self):
        """A paste that lost its END line still leaks the body."""
        truncated = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEAy8Dbv8prpJ/0kKhlGeJYozo2t60EG8L0561g13R29LvMR5hy"
        )
        self.assertIn("private_key_pem", _kinds(truncated))

    def test_a_public_key_is_not_flagged(self):
        self.assertNotIn(
            "private_key_pem",
            _kinds(
                "-----BEGIN PUBLIC KEY-----\n"
                "MFwwDQYJKoZIhvcNAQEBBQADSwAwSAJBALQ\n"
                "-----END PUBLIC KEY-----"))


if __name__ == "__main__":
    unittest.main()
