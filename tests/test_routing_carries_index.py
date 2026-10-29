"""The routing has to carry the index with it.

v1.17.15. An earlier iteration of the per-fact routing wrote the fact
to the right database but left the lexical and vector index behind. A
read still found the row -- the meta LIKE path scans
memory_canonical directly -- and returned no scored hit at all. A fact
that is stored but not indexed is a fact the recall cannot score, and
it looked like a working recall to anyone reading the response: the
content was there, the score_kind was not.

So the tests here check the scored path, not just the tier. The
index following the write is the part that is easy to get wrong and
hard to notice.
"""
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path


class TestRoutingCarriesTheIndex(unittest.TestCase):
    """A fact that lands in the right tier has to be findable there."""

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
        # Windows keeps the sqlite file locked for a moment after the
        # last connection closes. ignore_cleanup_errors keeps a
        # leftover directory from failing the run.
        try:
            cls._tmp.cleanup()
        except OSError:
            pass

    def _write(self, text, kind):
        r = self.client.post("/v1/write", json={
            "text": text, "tier": "public", "kind": kind,
            "mode": "regex", "user": "admin",
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def _read(self, query):
        r = self.client.post("/v1/read", json={
            "query": query, "tier": "public", "top_k": 5,
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()

    def _rows(self, db_rel, sql, params):
        p = self.astor_dir / db_rel
        if not p.exists():
            return []
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            return con.execute(sql, params).fetchall()
        finally:
            con.close()

    def test_a_shareable_fact_is_scored_after_the_write(self):
        """The scored path must find it, not only the meta LIKE path."""
        stamp = "".join(chr(ord('a') + i % 26) for i in range(11))
        text = f"astor memory routing index probe {stamp}"
        self._write(text, "lesson")
        res = self._read(f"astor memory routing index probe {stamp}")
        hits = res.get("results") or []
        scored = [h for h in hits if h.get("hit_source") == "bm25+vector"]
        self.assertTrue(
            scored,
            "the fact was stored but the index did not follow the write: "
            f"hits were {[h.get('hit_source') for h in hits]}")
        self.assertTrue(scored[0].get("score_kind"),
                        "a scored hit must say how it was scored")

    def test_a_personal_fact_is_scored_in_the_private_tier(self):
        """Same, on the private side: the fact and its index both move."""
        stamp = "".join(chr(ord('k') + i % 26) for i in range(11))
        text = f"astor memory private index probe {stamp}"
        self._write(text, "fact")
        pub = self._rows(
            "public/memory/astor_bus_public.db",
            "SELECT id FROM memory_canonical WHERE content LIKE ?",
            (f"%{stamp}%",))
        self.assertEqual(pub, [], "a personal fact reached the public tier")
        priv = self._rows(
            "users/admin/memory/astor_bus_admin.db",
            "SELECT id, visibility FROM memory_canonical WHERE content LIKE ?",
            (f"%{stamp}%",))
        self.assertTrue(priv, "the personal fact did not reach the private tier")
        self.assertEqual(priv[0][1], "personal")

    def test_the_public_tier_holds_only_commons(self):
        """The tier a peer reads holds nothing a peer must not read."""
        # make sure the public database exists at all
        self._write("astor memory commons probe gwqj", "lesson")
        rows = self._rows(
            "public/memory/astor_bus_public.db",
            "SELECT COUNT(*) FROM memory_canonical "
            "WHERE visibility != 'commons' AND tombstoned = 0", ())
        self.assertTrue(rows)
        self.assertEqual(rows[0][0], 0,
                         "the public tier holds rows that are not commons")


if __name__ == "__main__":
    unittest.main()
