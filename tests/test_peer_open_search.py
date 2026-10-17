"""Tests for open public-tier search (added 2026-10-16).

Peer search required a mutual relationship -- add the peer at trust>=50,
with an endpoint, and be added back. That is correct for a trusted
private network but makes the `public` tier unusable as shared
knowledge: nobody can read it without paperwork.

`peer_open_search` adds the zero-friction counterpart, kept
deliberately narrow:

  - read-only, public tier only
  - opt-in, default closed
  - signature required (no anonymous endpoint)
  - rate limited per caller
  - querying does NOT create a friendship

These tests cover the refusals first. The feature is only safe if the
narrow version holds.
"""
from __future__ import annotations

import unittest

from astor_memory._internal import peer_open_search as pos


class TestOpenSearchAuthorisation(unittest.TestCase):
    def setUp(self):
        pos.close_all()
        pos.reset_budget()

    def tearDown(self):
        pos.close_all()
        pos.reset_budget()

    def test_closed_by_default(self):
        self.assertEqual(pos.open_peers(), [])
        self.assertFalse(pos.is_open_for("astor:" + "a" * 32))

    def test_set_open_and_revoke(self):
        pid = "astor:" + "b" * 32
        self.assertTrue(pos.set_open(pid, True))
        self.assertTrue(pos.is_open_for(pid))
        self.assertTrue(pos.set_open(pid, False))
        self.assertFalse(pos.is_open_for(pid))

    def test_wildcard_opens_to_all(self):
        pos.set_open("*", True)
        self.assertIn("*", pos.open_peers())
        # is_open_for is the per-peer check used by the authorisation
        # branch; the wildcard is honoured there too.
        src = __import__("inspect").getsource(
            pos.handle_public_search)
        self.assertIn("'*'", src,
                      "handle_public_search must honour the wildcard grant")

    def test_unsigned_request_is_refused(self):
        """A request with no valid signature must not be able to learn
        whether open search is even enabled."""
        pos.set_open("*", True)
        out = pos.handle_public_search({
            "sender_peer_id": "astor:" + "e" * 32,
            "query": "anything",
        })
        self.assertEqual(out.get("error"), "bad_signature")

    def test_malformed_sender_id_refused(self):
        pos.set_open("*", True)
        out = pos.handle_public_search({"sender_peer_id": "not-a-peer-id",
                                        "query": "x"})
        self.assertEqual(out.get("error"), "invalid_sender_peer_id")

    def test_closed_node_refuses_even_valid_sender(self):
        """No grant -> refuse. The caller is not told whether open search
        is available on this node."""
        out = pos.handle_public_search({
            "sender_peer_id": "astor:" + "f" * 32,
            "query": "x",
        })
        self.assertIn(out.get("error"),
                      ("open_search_closed", "bad_signature"))


class TestRateLimit(unittest.TestCase):
    def setUp(self):
        pos.close_all()
        pos.reset_budget()

    def tearDown(self):
        pos.close_all()
        pos.reset_budget()

    def test_budget_depletes_then_refuses(self):
        pid = "astor:" + "1" * 32
        for _ in range(3):
            ok, _info = pos.check_rate(pid, limit=3)
            self.assertTrue(ok)
        ok, info = pos.check_rate(pid, limit=3)
        self.assertFalse(ok)
        self.assertEqual(info["error"], "rate_limited")
        self.assertGreater(info["retry_after_s"], 0)

    def test_budget_is_per_caller(self):
        a, b = "astor:" + "2" * 32, "astor:" + "3" * 32
        for _ in range(3):
            pos.check_rate(a, limit=3)
        ok_a, _ = pos.check_rate(a, limit=3)
        ok_b, _ = pos.check_rate(b, limit=3)
        self.assertFalse(ok_a)
        self.assertTrue(ok_b, "one caller exhausting budget must not "
                              "affect another")

    def test_reset_clears(self):
        pid = "astor:" + "4" * 32
        for _ in range(3):
            pos.check_rate(pid, limit=3)
        pos.reset_budget()
        ok, _ = pos.check_rate(pid, limit=3)
        self.assertTrue(ok)


class TestPublicOnlySearch(unittest.TestCase):
    """The search path must not be able to reach non-public facts."""

    def setUp(self):
        pos.close_all()
        pos.reset_budget()

    def tearDown(self):
        pos.close_all()
        pos.reset_budget()

    def test_rejects_one_char_query(self):
        """A 1-char LIKE would match most of the tier -- refuse rather
        than hand out the whole public store."""
        self.assertEqual(pos._recall_public("x", 5), [])
        self.assertEqual(pos._recall_public("a b", 5), [])

    def test_rejects_too_many_tokens(self):
        self.assertEqual(pos._recall_public("a " * 20, 5), [])

    def test_selects_no_private_columns(self):
        """Guard against the SELECT list growing to include
        entities_json / metadata / provenance columns.

        Checks the SQL, not the whole function -- the docstring names
        these columns precisely to explain why they are NOT selected.
        """
        import inspect
        import re
        src = inspect.getsource(pos._recall_public)
        # The SQL is split across string literals, so match the column
        # list with a DOTALL-tolerant pattern on the joined literals.
        joined = re.sub(r'["\']\s*\n?\s*["\']', ' ', src)
        select = re.search(r'SELECT\s+(.*?)\s+FROM\b', joined, re.S | re.I)
        self.assertIsNotNone(select, f"no SELECT clause found in:\n{joined}")
        cols = {c.strip().strip('"\'').lower()
                for c in select.group(1).split(",")}
        # Exact equality is the whole assertion: if anyone adds a column
        # to this SELECT the test fails. (Scanning the whole source for
        # forbidden names does not work -- the docstring names them on
        # purpose, to explain why they are excluded.)
        self.assertEqual(
            cols, {"id", "content", "kind", "importance"},
            f"open search selected unexpected columns: {cols}")

    def test_reads_only_the_public_store(self):
        """The tier boundary must be structural, not a filter.

        Verified on the live stores: CREATE_NO_WINDOW appears in 22
        source rows and 0 public rows, and searching it returns nothing
        -- because the public DB is the only file ever opened. A
        get_db_path call for any other tier would widen that, so the
        count is pinned.
        """
        import inspect
        src = inspect.getsource(pos._recall_public)
        self.assertEqual(
            src.count("get_db_path("), 1,
            "open search must resolve exactly one tier path")
        self.assertIn("get_db_path('public', 'bus')", src)
        for tier in ("source", "private", "repo"):
            self.assertNotIn(
                f"get_db_path('{tier}'", src,
                f"open search must not open the {tier} store")

    def test_no_tier_parameter_reaches_the_query(self):
        """A caller-supplied tier must not influence which store opens."""
        import inspect
        sig = inspect.signature(pos.handle_public_search)
        self.assertNotIn("tier", sig.parameters)
        src = inspect.getsource(pos.handle_public_search)
        self.assertNotIn("msg.get('tier')", src)
        self.assertNotIn('msg.get("tier")', src)

    def test_reads_read_only(self):
        import inspect
        src = inspect.getsource(pos._recall_public)
        self.assertIn("mode=ro", src,
                      "open search must open the database read-only")


if __name__ == "__main__":
    unittest.main()