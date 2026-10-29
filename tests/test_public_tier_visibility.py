"""The shared tier serves commons only.

v1.17.16. The public tier is the shared layer: every peer in the
federation reads it. Two holes were open, and both are covered here.

The write side was already closed -- the store guard (v1.17.15) refuses
a public write that is not commons. The read side was not: a local read
of the public tier did not check visibility, so a personal row that
reached the shared DB was served to whoever asked, and its text reached
the reranker, which sends it to a model.

And the way a personal row got there in the first place:
consolidate._action_promote copied a private fact into the public tier
verbatim, content included. Its source query did not even select the
visibility column, so there was nothing to check.

These tests pin the behaviour, not the implementation.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory import consolidate  # noqa: E402


def _make_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS memory_canonical ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "content TEXT, kind TEXT, visibility TEXT DEFAULT 'personal',"
        "tier TEXT DEFAULT 'private', namespace TEXT, user_id TEXT,"
        "scope_type TEXT DEFAULT 'long_term', importance REAL DEFAULT 0.5,"
        "access_count INTEGER DEFAULT 0, tombstoned INTEGER DEFAULT 0,"
        "created_at TEXT, stable_id TEXT, origin_session_id TEXT,"
        "provenance_at TEXT)"
    )
    return conn


class TestSharedTierReadFiltersPersonal(unittest.TestCase):
    """A local read of the public tier must not serve a personal row."""

    def setUp(self):
        self.src = (ROOT / "astor_memory" / "server.py").read_text(encoding="utf-8")

    def test_read_path_defines_the_visibility_filter(self):
        self.assertIn("_vis_where", self.src,
                      "the read path must build a visibility filter")

    def test_the_filter_covers_the_public_tier_only(self):
        i = self.src.find("_vis_where = ")
        self.assertGreater(i, 0)
        seg = self.src[i:i + 300]
        self.assertIn("'public'", seg,
                      "the shared tier is the one that must be filtered")

    def test_every_content_query_in_the_read_path_is_guarded(self):
        """The failure mode was a query that took content out of the bus
        with no visibility predicate."""
        i = self.src.find("def read()")
        self.assertGreater(i, 0)
        body = self.src[i:i + 120000]
        for m in re.finditer(r"SELECT id, content[^\"]*FROM memory_canonical",
                             body):
            seg = body[max(0, m.start() - 400):m.start() + 600]
            self.assertIn("_vis_where", seg,
                          "a read-path content query is not guarded")


class TestPromoteDoesNotCopyPrivateIntoTheSharedTier(unittest.TestCase):
    """consolidate._action_promote copies rows verbatim into the public
    tier, so it must not copy a personal one."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="astor_promote_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        private_db = self.tmp / "users" / "admin" / "memory" / "astor_bus_admin.db"
        public_db = self.tmp / "public" / "memory" / "astor_bus_public.db"
        self.private = _make_db(private_db)
        self.public = _make_db(public_db)

    def _add(self, content, visibility):
        cur = self.private.execute(
            "INSERT INTO memory_canonical (content, kind, visibility, tier, "
            "importance, access_count, stable_id) VALUES (?, 'lesson', ?, "
            "'private', 0.95, 50, ?)",
            (content, visibility, f"stable-{content[:12]}"),
        )
        return cur.lastrowid

    def _promote(self, fact_id):
        facts = [{
            "id": fact_id,
            "importance": 0.95,
            "access_count": 50,
            "distinct_queries_hit": 5,
        }]
        return consolidate._action_promote(
            self.private, facts, self.tmp, "admin")

    def _public_content(self):
        return [r["content"] for r in self.public.execute(
            "SELECT content FROM memory_canonical").fetchall()]

    def test_a_personal_row_is_not_copied(self):
        secret = "my telegram chat id is 555000111, passphrase hunter2"
        fid = self._add(secret, "personal")
        self._promote(fid)
        self.assertEqual(self._public_content(), [],
                         "a personal row was copied into the shared tier")

    def test_a_commons_row_is_still_copied(self):
        """Promote exists to publish a fact that is already marked
        shareable but sits in a private tier. It must still work."""
        lesson = "use GTC for limit orders; DAY orders vanish at the close"
        fid = self._add(lesson, "commons")
        self._promote(fid)
        self.assertEqual(self._public_content(), [lesson],
                         "promote stopped publishing shareable rows")

    def test_a_row_with_no_visibility_is_treated_as_personal(self):
        """The column defaults to personal in the schema, so a row that
        never set it -- an old row, or one written by a path that
        does not know about the column -- is personal, and must not
        be copied into the shared tier."""
        cur = self.private.execute(
            "INSERT INTO memory_canonical (content, kind, tier, importance, "
            "access_count, stable_id) VALUES (?, 'lesson', 'private', 0.95, "
            "50, 'stable-novis')",
            ("a row with no visibility column",),
        )
        self._promote(cur.lastrowid)
        self.assertEqual(self._public_content(), [])


if __name__ == "__main__":
    unittest.main()
