"""Two holes the memory_defense hardening left behind.

1. Overlapping matches shifted every later replacement. Replacing one
   span changes the length of the string, so the next replacement used
   a stale offset. With a whole-block match -- a PEM private key, whose
   body is also matched by the high-entropy and aws-secret patterns --
   the block's end landed past the block and ate the text after it.

2. The audit row's actor was built from the user id the request
   supplied, so the audit trail could be written under someone else's
   name.
"""
from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from astor_memory.nest import memory_defense as md


PEM = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ\n"
    "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOP\n"
    "-----END PRIVATE KEY-----"
)
TAIL = "  and this is ordinary text after the key that must survive"


class TestOverlappingMatches(unittest.TestCase):

    def test_the_text_after_a_block_match_survives(self):
        matches = md.scan_facts(PEM + TAIL)
        self.assertTrue(matches, "the PEM was not matched at all")
        out = md.redact_facts(PEM + TAIL, matches)
        self.assertIn(
            TAIL.strip(), out,
            "the redaction ate the text after the block -- the later "
            "replacement used an offset from the original string")
        self.assertNotIn(
            "ABCDEFGHIJKLMNOP", out,
            "the key body survived the redaction")

    def test_the_key_is_still_fully_redacted(self):
        matches = md.scan_facts(PEM + TAIL)
        out = md.redact_facts(PEM + TAIL, matches)
        self.assertNotIn("BEGIN PRIVATE KEY", out)
        self.assertNotIn("END PRIVATE KEY", out)

    def test_a_redaction_that_follows_a_block_is_still_applied(self):
        """Two separate secrets: both go, and the text between them stays."""
        text = ("mail me at ops@example.com or use key " + PEM +
                " -- ping ops@example.com later")
        matches = md.scan_facts(text)
        out = md.redact_facts(text, matches)
        self.assertNotIn("ops@example.com", out)
        self.assertNotIn("ABCDEFGHIJKLMNOP", out)
        self.assertIn("later", out)


class TestAuditActorIsNotSpoofable(unittest.TestCase):

    def _capture_audit(self, claimed_user: str) -> str:
        """Run audit_log_record with a bus stub; return the actor it wrote."""
        written = {}

        class _Row(dict):
            pass

        class _FakeBus:
            def write_audit(self, **kw):
                written.update(kw)

        class _FakeFactory:
            def __call__(self, *a, **kw):
                return _FakeBus()

        import astor_memory.bus.store as store
        import astor_memory._internal.acl as acl

        orig_bus = store.astor_bus
        orig_check = acl.astor_check_write
        orig_ctx = acl.astor_current_acl
        store.astor_bus = _FakeFactory()
        acl.astor_check_write = lambda **kw: None

        class _Ctx:
            actor = "admin:admin"
        acl.astor_current_acl = lambda: _Ctx()
        try:
            md.audit_log_record(
                fact_id=1, user=claimed_user, tier="public",
                matches=[md.PIIMatch(
                    name="email", severity="redact", start=0, end=5,
                    snippet="xxxxx")],
                policy_applied="redact")
        finally:
            store.astor_bus = orig_bus
            acl.astor_check_write = orig_check
            acl.astor_current_acl = orig_ctx
        return written.get("actor")

    def test_the_claimed_user_does_not_become_the_actor(self):
        actor = self._capture_audit("user:sunday")
        self.assertNotEqual(
            actor, "admin:user:sunday",
            "the audit actor was built from the user id the request "
            "supplied -- the audit trail can be written under any name")
        self.assertEqual(
            actor, "admin:admin",
            "the audit actor should be the identity bound to the request")


if __name__ == "__main__":
    unittest.main()
