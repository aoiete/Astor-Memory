"""Mining a private fact into the shared tier must run the secret scan.

`am mine-public` promoted a private note into the public tier after
checking it with the classifier's PII detector -- names, places,
emotions. That detector does not know a bearer token from a name, so a
private note holding a token was published with the token in it. The
broad scan (memory_defense) is the one that knows, and `am write
--tier public` already went through it.
"""
from __future__ import annotations

import unittest

from astor_memory.cli.main import _scan_for_secrets


class TestMinePublicRunsTheSecretScan(unittest.TestCase):

    def test_a_bearer_token_stops_the_promotion(self):
        found = _scan_for_secrets(
            "the deploy token is Bearer abcdefghijklmnopqrstuvwxyz0123 "
            "and it must not reach the shared tier")
        self.assertTrue(
            found,
            "the broad scan did not catch the bearer token")

    def test_a_pem_private_key_stops_the_promotion(self):
        found = _scan_for_secrets(
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEAy8Dbv8prpJ0kKhlGeJYozo2t60EG8L0561g13R29LvMR5hy\n"
            "-----END RSA PRIVATE KEY-----")
        self.assertTrue(found, "the broad scan did not catch the PEM key")

    def test_a_clean_method_note_passes(self):
        found = _scan_for_secrets(
            "always re-read the lockfile before installing a package "
            "so the resolved versions are the ones you reviewed")
        self.assertEqual(
            found, set(),
            "a clean method note was flagged as a secret")

    def test_the_scan_cannot_be_skipped_by_a_broken_scanner(self):
        """If the scanner cannot run, nothing gets published."""
        import astor_memory.nest.memory_defense as md
        original = md.scan_policy

        def _boom(*_a, **_k):
            raise RuntimeError("scanner down")

        md.scan_policy = _boom
        try:
            found = _scan_for_secrets("anything at all")
        finally:
            md.scan_policy = original
        self.assertTrue(
            found,
            "a broken scanner let the content through")


if __name__ == "__main__":
    unittest.main()
