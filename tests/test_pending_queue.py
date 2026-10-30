"""A fact that was written but never promoted must be visible.

v1.17.19. The store refuses to promote a personal fact into the public
tier. The candidate row is already inserted by then, so the refusal
left a row in memory_candidates that nobody could explain: the fact
looked written and was not.

The queue was never missing -- memory_candidates.review_state defaults
to 'pending' and only promote_candidate moves it. What was missing was
(a) the reason, and (b) anyone who could see the queue.

These tests pin the behaviour: the refusal leaves a reason, the queue
lists it, an operator can confirm it on purpose, and can drop it.
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


class TestPendingQueue(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="astor_pending_")
        os.environ["ASTOR_DIR"] = self.tmp
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bus(self, tier="public", user_id=None):
        from astor_memory._internal.acl import astor_init_acl
        from astor_memory.bus.store import astor_bus
        astor_init_acl(actor="admin:admin", role="admin", tier=tier)
        return astor_bus(tier=tier, user_id=user_id)

    def _refused_candidate(self):
        """A personal fact aimed at the public tier: inserted, refused."""
        bus = self._bus("public")
        ev = bus.append_event(namespace="test", agent_id="test",
                              source="test", action="write",
                              content="my telegram chat id is 555000111",
                              metadata={})
        cand = bus.insert_candidate(event_id=ev, namespace="test",
                                    content="my telegram chat id is 555000111",
                                    kind="fact", importance=0.5,
                                    metadata={})
        with self.assertRaises(ValueError):
            bus.promote_candidate(candidate_id=cand, promoted_by="test",
                                   user_id="admin", tier="public",
                                   scope_type="long_term",
                                   visibility="personal", verdict="settled")
        return bus, cand

    def test_the_refusal_leaves_a_reason(self):
        bus, cand = self._refused_candidate()
        row = bus.conn.execute(
            "SELECT review_state, rejected_reason FROM memory_candidates "
            "WHERE id = ?", (cand,)).fetchone()
        self.assertEqual(row[0], "pending",
                         "the candidate must stay pending, not rejected")
        self.assertTrue(row[1], "the refusal must leave a reason")

    def test_the_queue_lists_it_with_the_reason(self):
        self._refused_candidate()
        from astor_memory.cli.main import cmd_pending

        class _Args:
            limit = 20
            tier = "public"
            user = "admin"

        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_pending(_Args())
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("pending candidate", out)
        self.assertIn("why:", out)

    def test_confirming_without_force_is_refused(self):
        self._refused_candidate()
        from astor_memory.cli.main import cmd_confirm

        class _Args:
            candidate_id = 1
            force = False
            kind = ""
            tier = "public"
            user = "admin"

        import io
        import contextlib
        buf = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = cmd_confirm(_Args())
        self.assertEqual(rc, 2)
        self.assertIn("--force", err.getvalue())

    def test_confirming_into_public_needs_a_shareable_kind(self):
        # the public tier holds lessons/methods/workflows/models/patterns
        # -- a raw fact does not belong there, so the operator has to
        # name the kind it is published as
        self._refused_candidate()
        from astor_memory.cli.main import cmd_confirm

        class _Args:
            candidate_id = 1
            force = True
            kind = ""
            tier = "public"
            user = "admin"

        import io
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = cmd_confirm(_Args())
        self.assertEqual(rc, 2)
        self.assertIn("--kind", err.getvalue())

    def test_confirming_with_force_promotes_it(self):
        bus, cand = self._refused_candidate()
        from astor_memory.cli.main import cmd_confirm

        class _Args:
            candidate_id = cand
            force = True
            kind = "lesson"
            tier = "public"
            user = "admin"

        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_confirm(_Args())
        self.assertEqual(rc, 0, buf.getvalue())
        row = bus.conn.execute(
            "SELECT review_state, promoted_to FROM memory_candidates "
            "WHERE id = ?", (cand,)).fetchone()
        self.assertEqual(row[0], "promoted")
        self.assertIsNotNone(row[1], "the fact id must be recorded")

    def test_rejecting_drops_it(self):
        bus, cand = self._refused_candidate()
        from astor_memory.cli.main import cmd_reject

        class _Args:
            candidate_id = cand
            reason = "test residue"
            kind = ""
            tier = "public"
            user = "admin"

        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_reject(_Args())
        self.assertEqual(rc, 0, buf.getvalue())
        row = bus.conn.execute(
            "SELECT review_state, rejected_reason FROM memory_candidates "
            "WHERE id = ?", (cand,)).fetchone()
        self.assertEqual(row[0], "rejected")
        self.assertEqual(row[1], "test residue")


if __name__ == "__main__":
    unittest.main()
