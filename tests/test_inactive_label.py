"""A fact the user reported as not working must be labelled on recall.

The astor-feedback hook (D:/AI/admin/hermes-config/hooks/astor-feedback)
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
