"""A retired fact is not shared with peers.

`status='inactive'` means the operator reported that fact as not
working -- the feedback hook sets it when an answer drawn from it
failed.

Locally the fact stays visible and labelled, because the local agent
can reason about "this used to be true". A peer has none of that
context: it would receive the fact as knowledge and repeat the same
failure at federation scale. So the peer boundary stops serving
retired facts.

The fact is not destroyed -- it stays in the shared tier with its label
and its reason, and the local operator can still see it.
"""
from __future__ import annotations

import sqlite3
import unittest

from astor_memory._internal import peer_open_search as pos


class TestPeerDoesNotReceiveRetiredFacts(unittest.TestCase):

    def setUp(self):
        pos.close_all()
        pos.reset_budget()
        from astor_memory._internal.acl import astor_init_acl
        from astor_memory._internal.acl_layout import Tier, get_db_path
        from astor_memory.bus.schema import astor_init_schema
        from astor_memory.bus.store import astor_bus
        p = get_db_path(Tier.PUBLIC.value, "bus", None)
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p), timeout=30)
        conn.execute("PRAGMA busy_timeout = 30000")
        astor_init_schema(conn)
        conn.close()
        astor_init_acl(actor="admin:admin", role="admin", tier="public")
        self.bus = astor_bus(tier=Tier.PUBLIC.value, user_id=None)

    def tearDown(self):
        pos.close_all()
        pos.reset_budget()
        try:
            from astor_memory._internal.test_state import astor_close_all_test_state
            astor_close_all_test_state()
        except Exception:
            pass

    def _write(self, content):
        ev = self.bus.append_event(namespace="t", agent_id="t", source="t",
                                   action="write", content=content)
        cand = self.bus.insert_candidate(event_id=ev, namespace="t",
                                         content=content, kind="fact",
                                         confidence=0.9)
        return self.bus.promote_candidate(
            cand, promoted_by="test", user_id="admin", tier="public",
            visibility="commons")

    def test_a_retired_fact_is_not_served_to_a_peer(self):
        active = self._write("zebra crossing fact that is still current")
        retired = self._write("zebra crossing fact that was reported as broken")

        # retire the second one, the way the feedback hook does
        self.bus.conn.execute(
            "UPDATE memory_canonical SET status = 'inactive' WHERE id = ?",
            (retired,))
        self.bus.conn.commit()

        hits = pos._recall_public("zebra crossing fact", 10)
        ids = [h.get("fact_id") for h in hits]
        self.assertIn(active, ids, ids)
        self.assertNotIn(retired, ids,
                         "a fact the operator reported as not working "
                         "must not be handed to a peer as knowledge")

    def test_the_column_list_is_unchanged(self):
        """The peer contract selects four columns; the fix is a WHERE
        clause, not a new column. This guard already existed."""
        import inspect
        import re
        src = inspect.getsource(pos._recall_public)
        joined = re.sub(r'["\']\s*\n?\s*["\']', ' ', src)
        select = re.search(r'SELECT\s+(.*?)\s+FROM\b', joined, re.S | re.I)
        cols = {c.strip().strip('"\'').lower()
                for c in select.group(1).split(",")}
        self.assertEqual(cols, {"id", "content", "kind", "importance"}, cols)


if __name__ == "__main__":
    unittest.main()
