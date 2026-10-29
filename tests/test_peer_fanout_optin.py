"""A peer you added does not start inside the broadcast range.

v1.17.16 (D11). The automatic fanout sends every query that misses
locally to the peers that select_search_targets picks. The pick was
trust >= 50, an endpoint, and not blacklisted -- plus outbound_search,
which has meant "may I search that peer" since v1.17.3.

Its default was True, so a peer added an hour ago was already a
broadcast target. That is the trap: a peer you added deliberately is
not necessarily someone you want reading every query you type, and the
node-level fanout switch is on by default, so nothing else stopped it.

The fix is not a new switch. New peers get an explicit
outbound_search=False; rows that predate it keep what they had, because
absent means on and the operator's choice is not ours to overwrite.

These tests pin the behaviour, not the implementation.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory._internal import peer_relationships as pr  # noqa: E402


class TestNewPeersAreNotBroadcastTargets(unittest.TestCase):
    """add_peer writes the flag; select_search_targets reads it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="astor_fanout_")
        self.astor_dir = self.tmp

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add(self, peer_id, **kw):
        return pr.add_peer(peer_id, astor_dir=self.astor_dir, **kw)

    def _meta(self, peer_id):
        row = pr.get_peer(peer_id, astor_dir=self.astor_dir)
        meta = row.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        return meta

    def test_a_new_peer_is_marked_not_receiving_broadcasts(self):
        self._add("astor:" + "a" * 32, trust=60, endpoint="https://x.example.com")
        self.assertIs(
            self._meta("astor:" + "a" * 32).get("outbound_search"), False,
            "a newly added peer must not start inside the broadcast range")

    def test_a_legacy_row_without_the_flag_still_counts_as_on(self):
        """Absent means on. Rows written before this change keep the
        behaviour the operator already had."""
        row = pr.add_peer("astor:" + "b" * 32, astor_dir=self.astor_dir,
                          trust=60, endpoint="https://y.example.com")
        # simulate a row written before the flag existed
        import astor_memory._internal.peer_relationships as _pr
        con = _pr._get_conn(self.astor_dir)
        with _pr._RELATIONSHIPS_LOCK:
            con.execute(
                "UPDATE peer_relationships SET metadata = ? WHERE peer_id = ?",
                (json.dumps({"topic_weights": {}}), "astor:" + "b" * 32))
            con.commit()
        self.assertNotIn("outbound_search", self._meta("astor:" + "b" * 32))
        self.assertTrue(pr._read_outbound_search_flag(
            "astor:" + "b" * 32, astor_dir=self.astor_dir),
            "a legacy row must still be a search target")
        del row

    def test_the_operator_can_turn_it_on(self):
        pid = "astor:" + "c" * 32
        self._add(pid, trust=60, endpoint="https://z.example.com")
        self.assertFalse(pr._read_outbound_search_flag(
            pid, astor_dir=self.astor_dir))
        ok = pr.set_outbound_search(pid, True, astor_dir=self.astor_dir)
        self.assertTrue(ok)
        self.assertTrue(pr._read_outbound_search_flag(
            pid, astor_dir=self.astor_dir),
            "turning it on must work -- the dashboard button calls this")

    def test_an_update_does_not_wipe_the_flag(self):
        """add_peer replaces the metadata column. A caller that passes a
        partial dict must not silently drop the flags inside it."""
        pid = "astor:" + "d" * 32
        self._add(pid, trust=60, endpoint="https://w.example.com")
        pr.set_outbound_search(pid, True, astor_dir=self.astor_dir)
        self._add(pid, trust=70, metadata={"note": "hello"})
        self.assertTrue(self._meta(pid).get("outbound_search"),
                        "an update wiped the outbound flag")
        self.assertEqual(self._meta(pid).get("note"), "hello")

    def test_an_update_does_not_wipe_the_inbound_flag(self):
        pid = "astor:" + "e" * 32
        self._add(pid, trust=60, endpoint="https://v.example.com")
        pr.set_allow_search(pid, True, astor_dir=self.astor_dir)
        self._add(pid, trust=70, metadata={"note": "hi"})
        self.assertTrue(self._meta(pid).get("allow_search"),
                        "an update wiped the allow_search flag")

    def test_the_flag_can_be_set_at_add_time(self):
        """A caller that knows the peer well can opt it in directly."""
        pid = "astor:" + "f" * 32
        self._add(pid, trust=60, endpoint="https://u.example.com",
                  outbound_search=True)
        self.assertIs(self._meta(pid).get("outbound_search"), True)


if __name__ == "__main__":
    unittest.main()
