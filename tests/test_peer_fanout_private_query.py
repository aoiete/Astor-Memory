"""A private query must not leave the node.

The peer fan-out sends the query text verbatim to every trusted friend.
A question that carries the private part of the ask ("what was my TFSA
balance", "who is my daughter's teacher") handed that to the whole
federation. The gate is astor's own detectors -- the same ones the write
path uses -- not a hand-rolled regex.
"""
from __future__ import annotations

import unittest
from unittest import mock

from astor_memory._internal import peer_recall


class TestPeerFanoutDoesNotShipAPrivateQuery(unittest.TestCase):

    def _dispatch(self, query: str) -> dict:
        """Run the dispatch with the network stubbed out."""
        with mock.patch(
                "astor_memory._internal.peer_relationships.list_peers",
                return_value=[]), \
             mock.patch(
                "astor_memory._internal.peer_identity.init_identity",
                return_value={"peer_id": "me"}):
            return peer_recall.dispatch_peer_fanout(query=query)

    def test_a_private_query_never_reaches_the_network(self):
        out = self._dispatch("what was my TFSA balance last March")
        self.assertEqual(out["peer_results"], [])
        self.assertEqual(out["peer_count"], 0)
        self.assertEqual(out.get("skipped"), "query_reads_as_private")

    def test_a_first_person_query_does_not_leave(self):
        out = self._dispatch("who is my daughter's teacher")
        self.assertEqual(out["peer_count"], 0)
        self.assertEqual(out.get("skipped"), "query_reads_as_private")

    def test_a_pii_query_does_not_leave(self):
        out = self._dispatch("look up 555-123-4567 for me")
        self.assertEqual(out["peer_count"], 0)
        self.assertEqual(out.get("skipped"), "query_reads_as_private")

    def test_a_method_question_still_fans_out(self):
        """The federation is for questions about methods and topics."""
        with mock.patch(
                "astor_memory._internal.peer_relationships.list_peers",
                return_value=[]), \
             mock.patch(
                "astor_memory._internal.peer_identity.init_identity",
                return_value={"peer_id": "me"}):
            out = peer_recall.dispatch_peer_fanout(
                query="how do you rotate a lockfile before installing")
        # It got as far as looking for peers (there are none), so the
        # privacy gate did NOT stop it.
        self.assertNotEqual(out.get("skipped"), "query_reads_as_private")


if __name__ == "__main__":
    unittest.main()
