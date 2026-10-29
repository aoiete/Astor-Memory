"""One fact, one hit.

v1.17.15. The read path finds facts two ways: the scored path (bm25
+ vector, which knows the similarity) and the injection channels
(meta-recall, meta-recall-lessons, trigger-aware, mental models,
knowledge pages). They are independent, so a fact both channels match
came back twice in one response.

That broke the Hermes adapter, which asserts every result says how it
was scored: the injected copy has no score_kind, so the assertion
failed on a response whose scored copy was right there. The adapter
test had been failing for a while for exactly this reason, and only
showed up once the corpus was big enough for both channels to match
the same fact.

The scored copy is the better one -- it carries the similarity and the
score_kind, and the injected copy adds no field the scored row does
not already have -- so the scored copy wins and the injected duplicate
is dropped.
"""
import os
import tempfile
import time
import unittest
from pathlib import Path


class TestReadReturnsOneHitPerFact(unittest.TestCase):

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

    def test_a_fact_the_injection_channels_also_match_comes_back_once(self):
        """The lesson the meta channel matches is also a scored hit."""
        marker = "astormem%d" % int(time.time() * 1000)
        text = "astor memory retrieval keeps durable facts findable " + marker
        w = self.client.post("/v1/write", json={
            "text": text, "tier": "public", "kind": "lesson", "user": "admin",
        })
        self.assertEqual(w.status_code, 200, w.get_data(as_text=True))
        fid = (w.get_json() or {}).get("fact_ids")[0]

        r = self.client.post("/v1/read", json={
            "query": "astor memory " + marker, "tier": "public", "top_k": 5,
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        hits = (r.get_json() or {}).get("results") or []
        ids = [h.get("fact_id") for h in hits]
        self.assertIn(fid, ids, "the fact should be recalled")
        self.assertEqual(
            len(ids), len(set(ids)),
            f"the same fact came back more than once: {[(h.get('hit_source'), h.get('fact_id')) for h in hits]}")

    def test_every_recalled_fact_says_how_it_was_scored(self):
        """The adapter's contract: score_kind on every result."""
        marker = "astormem%d" % int(time.time() * 1000)
        text = "astor memory retrieval keeps durable facts findable " + marker
        self.client.post("/v1/write", json={
            "text": text, "tier": "public", "kind": "lesson", "user": "admin",
        })
        r = self.client.post("/v1/read", json={
            "query": "astor memory " + marker, "tier": "public", "top_k": 5,
        })
        hits = (r.get_json() or {}).get("results") or []
        for h in hits:
            self.assertIn("score_kind", h,
                          f"a result without score_kind: {h.get('hit_source')}")
            self.assertIn(h["score_kind"],
                          ("hybrid", "cosine", "session_neighbor"))


if __name__ == "__main__":
    unittest.main()
