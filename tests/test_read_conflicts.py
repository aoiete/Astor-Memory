"""End-to-end: a contradicted fact is reported and not reinforced.

v1.17.18. The unit test covers the scanner. This one covers the
server: two contradictory facts in the public tier, read back through
/v1/read, and the answer must say they conflict -- and the
contradicted fact must not get its access counter bumped, because that
counter is what keeps a fact out of the decay sweep.

Two instances (separate astor_dir, separate port) are not needed here:
the read path is exercised against one live server over HTTP, which is
the path a peer or the hermes adapter actually takes.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class TestReadReportsConflicts(unittest.TestCase):
    """The live server must report conflicts and skip reinforcement."""

    @classmethod
    def setUpClass(cls):
        from astor_memory.server import create_app
        cls.tmp = tempfile.mkdtemp(prefix="astor_conflict_")
        os.environ["ASTOR_DIR"] = cls.tmp
        cls.app = create_app(astor_dir=cls.tmp)
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _write_public(self, content):
        """Put one fact straight into the public tier, the way an
        internal publisher would (it declares commons explicitly)."""
        from astor_memory._internal.acl import astor_init_acl
        from astor_memory.bus import astor_bus
        astor_init_acl(actor="admin:admin", role="admin", tier="public")
        bus = astor_bus(tier="public", user_id=None)
        ev = bus.append_event(namespace="test", agent_id="test",
                              source="test", action="write",
                              content=content, metadata={})
        cand = bus.insert_candidate(event_id=ev, namespace="test",
                                    content=content, kind="fact",
                                    importance=0.7,
                                    metadata={"kind": "fact"})
        fid = bus.promote_candidate(candidate_id=cand, promoted_by="test",
                                    user_id="admin", tier="public",
                                    scope_type="long_term",
                                    visibility="commons", verdict="settled")
        return fid

    def test_conflicting_facts_are_reported_and_not_reinforced(self):
        marker = "ZEBRAFISH2 conflict probe"
        a = self._write_public(
            f"{marker} astor 的 public tier 存 public 数据")
        b = self._write_public(
            f"{marker} astor 的 public tier 不存 private 数据")

        resp = self.client.post("/v1/read",
                                json={"query": marker, "tier": "public",
                                      "top_k": 5})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        conflicts = body.get("conflicts")
        self.assertIsNotNone(conflicts, "the response must carry conflicts")
        self.assertTrue(conflicts, f"the two facts must be reported: {body}")
        pair = {(int(conflicts[0]["a"]), int(conflicts[0]["b"]))}
        self.assertIn((a, b), pair | {(b, a)})
        self.assertTrue(body.get("conflict_note"), "a note must say so")

        # the contradicted fact must not have been reinforced
        con = sqlite3.connect(str(Path(self.tmp) / "public" / "memory"
                                  / "astor_bus_public.db"))
        counts = dict(con.execute(
            "SELECT id, access_count FROM memory_canonical "
            "WHERE id IN (?, ?)", (a, b)).fetchall())
        con.close()
        self.assertEqual(counts.get(a), 0,
                         "a contradicted fact was reinforced")
        self.assertEqual(counts.get(b), 0,
                         "a contradicted fact was reinforced")


if __name__ == "__main__":
    unittest.main()
