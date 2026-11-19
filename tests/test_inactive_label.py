"""A fact the user reported as not working must be labelled on recall.

The astor-feedback hook (installed under the Hermes home)
watches for a user verdict -- "还是不行" -- and demotes the facts that
fed the previous answer by setting status='inactive'. Its contract, in
the hook's own docstring and in HOOK.yaml, is that the fact stays
recallable but is marked:

    "⚠[INACTIVE] [EVIDENCE] user likes coffee [/EVIDENCE] -- the
     fact stays recallable but is marked [INACTIVE]"

Nothing implemented that. The endpoint set the column and stopped; the
read path never looked at it, so a demoted fact came back byte
identical, at the same score, in the same position. The whole backward
channel was inert: the user could say a memory did not work as often as
they liked and retrieval would not change.

This test pins the label on both paths that can return a fact -- the
ranked path and the grep-verify path -- and pins that an active fact is
not labelled.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


MARKER = "quokka"

class TestInactiveLabel(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.astor_dir = Path(cls._tmp.name)
        os.environ["ASTOR_DIR"] = str(cls.astor_dir)
        from astor_memory.server import create_app
        cls.client = create_app().test_client()

    @classmethod
    def tearDownClass(cls):
        from astor_memory._internal.test_state import astor_close_all_test_state
        try:
            astor_close_all_test_state()
        except Exception:
            pass
        try:
            cls._tmp.cleanup()
        except OSError:
            pass

    def _write(self, text):
        w = self.client.post("/v1/write", json={
            "text": text, "tier": "private", "user": "admin",
            "mode": "regex", "kind": "fact",
        })
        self.assertEqual(w.status_code, 200, w.get_data(as_text=True))
        fids = (w.get_json() or {}).get("fact_ids") or []
        self.assertTrue(fids, w.get_data(as_text=True))
        return fids[0]

    def _read(self, query):
        r = self.client.post("/v1/read", json={
            "query": query, "tier": "private", "user": "admin", "top_k": 5,
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return (r.get_json() or {}).get("results") or []

    def test_an_active_fact_is_not_labelled(self):
        fid = self._write(f"active fact mentioning {MARKER} and a rare token")
        hits = self._read(f"{MARKER} rare token")
        hit = next((h for h in hits if h.get("fact_id") == fid), None)
        self.assertIsNotNone(hit, hits)
        self.assertNotIn("[INACTIVE]", hit.get("content") or "")

    def test_a_demoted_fact_is_labelled_on_the_ranked_path(self):
        fid = self._write(f"demoted fact mentioning {MARKER} and another rare token")
        st = self.client.post(f"/v1/fact/{fid}/set_status", json={
            "status": "inactive", "reason": "user reported this did not work",
        })
        self.assertEqual(st.status_code, 200, st.get_data(as_text=True))

        hits = self._read(f"{MARKER} another rare token")
        hit = next((h for h in hits if h.get("fact_id") == fid), None)
        self.assertIsNotNone(hit, hits)
        self.assertIn("[INACTIVE]", hit.get("content") or "",
                      "a demoted fact must say so in what the agent reads")
        # The label must not replace the fact.
        self.assertIn("demoted fact mentioning", hit.get("content") or "")

    def test_the_status_is_a_field_not_only_a_label(self):
        """A consumer must be able to tell without reading the text.

        Grepping the content for "[INACTIVE]" would also match a fact
        whose TEXT discusses the inactive mechanism -- and this corpus
        does contain such facts. So the status travels as a field.
        """
        fid = self._write(f"status field test mentioning {MARKER} and a fourth rare token")
        hits = self._read(f"{MARKER} fourth rare token")
        hit = next((h for h in hits if h.get("fact_id") == fid), None)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.get("status"), "active")

        self.client.post(f"/v1/fact/{fid}/set_status", json={
            "status": "inactive", "reason": "user reported this did not work",
        })
        hits = self._read(f"{MARKER} fourth rare token")
        hit = next((h for h in hits if h.get("fact_id") == fid), None)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.get("status"), "inactive")

    def test_a_demoted_fact_gets_a_boundary_and_ages_out(self):
        """ReHarness: an experience should carry its own boundary.

        astor cannot rewrite the fact's text here, but it can give the
        fact a bound: demote it into the temporary layer with an
        expiry. The read path already filters expired rows, so the fact
        stops being surfaced on its own instead of sitting in the store
        forever as "inactive but still retrieved".
        """
        fid = self._write(f"bounded fact mentioning {MARKER} and a fifth rare token")
        self.client.post(f"/v1/fact/{fid}/set_status", json={
            "status": "inactive", "reason": "user reported this did not work",
        })

        # still recallable, and still labelled
        hits = self._read(f"{MARKER} fifth rare token")
        hit = next((h for h in hits if h.get("fact_id") == fid), None)
        self.assertIsNotNone(hit, "a demoted fact stays recallable")
        self.assertIn("[INACTIVE]", hit.get("content") or "")

        # the row carries the boundary
        from astor_memory.bus.store import astor_bus
        bus = astor_bus(tier="private", user_id="admin")
        row = bus.conn.execute(
            "SELECT scope_type, expires_at FROM memory_canonical WHERE id = ?",
            (fid,)).fetchone()
        self.assertEqual(row[0], "short_term", row)
        self.assertIsNotNone(row[1], "a demoted fact must have an expiry")

        # once the boundary passes, the read path stops serving it.
        # A different query, so the read cache (60s) does not answer
        # from the entry written before the expiry.
        bus.conn.execute(
            "UPDATE memory_canonical SET expires_at = '2000-01-01T00:00:00Z' "
            "WHERE id = ?", (fid,))
        bus.conn.commit()
        hits = self._read(f"{MARKER} fifth rare token expired now")
        self.assertNotIn(fid, [h.get("fact_id") for h in hits],
                         "an expired fact must not be served")

    def test_reactivating_a_fact_restores_the_permanent_layer(self):
        fid = self._write(f"reactivated fact mentioning {MARKER} and a sixth rare token")
        self.client.post(f"/v1/fact/{fid}/set_status", json={
            "status": "inactive", "reason": "user reported this did not work",
        })
        self.client.post(f"/v1/fact/{fid}/set_status", json={
            "status": "active", "reason": "it works again",
        })
        from astor_memory.bus.store import astor_bus
        bus = astor_bus(tier="private", user_id="admin")
        row = bus.conn.execute(
            "SELECT scope_type, expires_at, status FROM memory_canonical WHERE id = ?",
            (fid,)).fetchone()
        self.assertEqual(row[0], "long_term", row)
        self.assertIsNone(row[1], row)
        self.assertEqual(row[2], "active", row)

    def test_demotion_does_not_hide_the_fact_or_change_its_score(self):
        """The hook's contract: the fact stays recallable.

        Only the label changes. If the fact disappeared from the
        response, or its score moved, the demotion would have become a
        deletion -- which the hook explicitly refuses to do.
        """
        fid = self._write(f"still here mentioning {MARKER} and a third rare token")
        before = next((h for h in self._read(f"{MARKER} third rare token")
                       if h.get("fact_id") == fid), None)
        self.assertIsNotNone(before)

        self.client.post(f"/v1/fact/{fid}/set_status", json={
            "status": "inactive", "reason": "user reported this did not work",
        })
        after = next((h for h in self._read(f"{MARKER} third rare token")
                      if h.get("fact_id") == fid), None)
        self.assertIsNotNone(after, "a demoted fact must stay recallable")
        self.assertEqual(before.get("similarity"), after.get("similarity"))


if __name__ == "__main__":
    unittest.main()
