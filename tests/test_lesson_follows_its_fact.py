"""A lesson that was built on a wrong fact must lose its heat.

The occurrence ladder raises an experience's importance every time it
is asserted: one occurrence 0.7, three 0.85, six 0.95. A fact that a
later write contradicted was invalidated -- but the lesson built on it
kept its importance and kept climbing. A wrong rule got hotter every
time it was repeated, and nothing ever took it back down.

Now a lesson whose trigger fact was invalidated is demoted with it.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from astor_memory.server import demote_lessons_for_invalidated


class _Bus:
    def __init__(self, db_path: Path):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS memory_experience ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT NOT NULL,"
            "outcome TEXT NOT NULL DEFAULT 'neutral',"
            "trigger_keywords TEXT NOT NULL DEFAULT '[]',"
            "trigger_fact_ids TEXT NOT NULL DEFAULT '[]',"
            "importance REAL NOT NULL DEFAULT 0.7)")
        self.conn.commit()

    def close(self):
        self.conn.close()


class TestLessonFollowsItsFact(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="astor_lesson_demote_")
        self.bus = _Bus(Path(self.tmp) / "bus.db")
        self.bus.conn.executemany(
            "INSERT INTO memory_experience "
            "(namespace, outcome, trigger_fact_ids, importance) "
            "VALUES (?, ?, ?, ?)",
            [("private:admin", "success_pattern", "[1]", 0.95),
             ("private:admin", "success_pattern", "[12]", 0.95),
             ("private:admin", "success_pattern", "[2,3]", 0.85),
             ("private:other", "success_pattern", "[1]", 0.95)])

    def tearDown(self):
        self.bus.close()

    def _importances(self):
        return dict(self.bus.conn.execute(
            "SELECT id, importance FROM memory_experience").fetchall())

    def test_the_lesson_on_the_invalidated_fact_is_demoted(self):
        before = self._importances()
        demote_lessons_for_invalidated(
            self.bus, "private:admin",
            [{"fact_id": 1, "invalidated": True, "reason": "x"}])
        after = self._importances()
        self.assertLess(
            after[1], before[1],
            "the lesson kept its importance after the fact it was built on "
            "was invalidated -- a wrong rule stays hot forever")

    def test_a_fact_that_was_not_invalidated_leaves_the_lesson_alone(self):
        before = self._importances()
        demote_lessons_for_invalidated(
            self.bus, "private:admin",
            [{"fact_id": 1, "invalidated": False, "reason": "x"}])
        self.assertEqual(self._importances(), before)

    def test_another_fact_id_is_not_matched(self):
        """Fact 12 is not fact 1."""
        before = self._importances()
        demote_lessons_for_invalidated(
            self.bus, "private:admin",
            [{"fact_id": 1, "invalidated": True, "reason": "x"}])
        after = self._importances()
        self.assertEqual(
            after[2], before[2],
            "a lesson triggered by fact 12 was demoted because fact 1 was "
            "invalidated")

    def test_a_lesson_in_another_namespace_is_untouched(self):
        before = self._importances()
        demote_lessons_for_invalidated(
            self.bus, "private:admin",
            [{"fact_id": 1, "invalidated": True, "reason": "x"}])
        after = self._importances()
        self.assertEqual(
            after[4], before[4], "another user's lesson was demoted")

    def test_a_lesson_on_a_fact_in_the_list_is_demoted(self):
        before = self._importances()
        demote_lessons_for_invalidated(
            self.bus, "private:admin",
            [{"fact_id": 3, "invalidated": True, "reason": "x"}])
        after = self._importances()
        self.assertLess(after[3], before[3])

    def test_nothing_happens_without_an_invalidated_fact(self):
        before = self._importances()
        self.assertEqual(
            demote_lessons_for_invalidated(self.bus, "private:admin", []), [])
        self.assertEqual(self._importances(), before)


if __name__ == "__main__":
    unittest.main()
