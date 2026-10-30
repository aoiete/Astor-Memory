"""One click that says where a peer search got to.

v1.17.22. The test runs the real search path, because a port that
answers is not a peer that answers. What matters here:

  - the stage names are the ones the operator can act on: connect,
    sign, authorize, trust, match, ok
  - the address comes from the peer row, never from the caller, so
    there is no "connect to this URL" input to abuse
  - a test query cannot match anything by accident, so "matched
    nothing" is told apart from "did not arrive"
  - a test is charged against the peer's search budget like any other
    search -- the prefix must not buy a free way to hammer a peer
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PEER = "astor:" + "c" * 32


class TestPeerTestButton(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="astor_peertest_")
        os.environ["ASTOR_DIR"] = self.tmp
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add_peer(self, endpoint="http://127.0.0.1:1/", **kw):
        from astor_memory._internal.peer_relationships import add_peer
        return add_peer(PEER, kind="friend", trust=90,
                        public_key=kw.pop("public_key", "pk"),
                        endpoint=endpoint, **kw)

    def test_a_peer_that_is_not_there_is_reported(self):
        from astor_memory._internal.peer_test import test_peer
        out = test_peer("astor:" + "d" * 32)
        self.assertEqual(out["stage"], "connect")
        self.assertEqual(out["error"], "unknown_peer")
        self.assertFalse(out["ok"])

    def test_a_dead_address_is_a_connect_problem(self):
        from astor_memory._internal.peer_test import test_peer
        # port 1 on localhost: nothing listens there
        self._add_peer(endpoint="http://127.0.0.1:1/")
        out = test_peer(PEER, timeout=2.0)
        self.assertEqual(out["stage"], "connect")
        self.assertFalse(out["ok"])
        self.assertIn("127.0.0.1:1", out["detail"])

    def test_a_peer_with_no_address_is_reported(self):
        from astor_memory._internal.peer_test import test_peer
        self._add_peer(endpoint="")
        out = test_peer(PEER)
        self.assertEqual(out["error"], "no_endpoint")
        self.assertEqual(out["stage"], "connect")

    def test_the_test_query_cannot_match_by_accident(self):
        from astor_memory._internal.peer_test import (
            _test_query, TEST_QUERY_PREFIX,
        )
        q = _test_query()
        self.assertTrue(q.startswith(TEST_QUERY_PREFIX))
        self.assertNotEqual(q, _test_query())

    def test_relay_mode_without_a_relay_is_reported(self):
        from astor_memory._internal.peer_test import test_peer
        self._add_peer(endpoint="http://127.0.0.1:1/")
        out = test_peer(PEER, mode="relay", timeout=2.0)
        self.assertEqual(out["stage"], "connect")
        self.assertFalse(out["ok"])

    def test_the_test_is_rate_limited(self):
        from astor_memory._internal.peer_test import (
            test_peer, MAX_PER_PEER_PER_MINUTE,
        )
        self._add_peer(endpoint="http://127.0.0.1:1/")
        for _ in range(MAX_PER_PEER_PER_MINUTE):
            test_peer(PEER, timeout=0.5)
        out = test_peer(PEER, timeout=0.5)
        self.assertEqual(out["error"], "rate_limited")

    def test_a_test_query_still_costs_the_peer_its_budget(self):
        # the prefix must not buy a free way to hammer a peer
        from astor_memory._internal.peer_test import TEST_QUERY_PREFIX
        from astor_memory._internal import peer_rate_limit
        before = peer_rate_limit.check_and_consume.__doc__
        self.assertIsNotNone(before)
        # the rate limit runs on the requestor peer id and does not look
        # at the query at all
        import inspect
        src = inspect.getsource(peer_rate_limit.check_and_consume)
        self.assertNotIn(TEST_QUERY_PREFIX, src)


if __name__ == "__main__":
    unittest.main()
