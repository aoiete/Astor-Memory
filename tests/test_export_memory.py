"""Tests for astor_memory.export_memory (v1.16.71 Ship B).

Run: PYTHONPATH=D:/AI/Astor-Memory-Runtime D:/AI/PY-311/Scripts/python.exe -m pytest D:/AI/astor-memory/tests/test_export_memory.py -v
Or:  D:/AI/PY-311/Scripts/python.exe -m pytest tests/test_export_memory.py
"""
import csv
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, r"D:/AI/Astor-Memory-Runtime")
sys.path.insert(0, r"D:/AI/astor-memory")

from astor_memory.export_memory import (
    _fetch_facts, _format_markdown, _format_json, _format_csv,
    _format_agent_context, main,
)


def _make_test_db(path):
    """Create a tiny test DB with the schema columns exporter reads."""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE memory_canonical (
            id INTEGER PRIMARY KEY,
            namespace TEXT NOT NULL DEFAULT 'admin',
            content TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'fact',
            importance REAL NOT NULL DEFAULT 0.5,
            confidence REAL NOT NULL DEFAULT 0.7,
            distinct_queries_hit INTEGER NOT NULL DEFAULT 0,
            access_count INTEGER NOT NULL DEFAULT 0,
            last_confirmed_at DATETIME,
            tags TEXT NOT NULL DEFAULT '[]',
            tombstoned INTEGER NOT NULL DEFAULT 0,
            user_id TEXT,
            tier TEXT
        );
    """)
    rows = [
        # High importance, low dq — passes T1 gate via importance
        ("[LESSON] high imp fact", "lesson", 1.0, 0, 5, "admin", "private", 0),
        # Low importance, high dq — passes T1 gate via dq
        ("high dq fact", "fact", 0.4, 5, 100, "admin", "private", 0),
        # Low importance, low dq — BLOCKED by T1 gate
        ("trash fact", "fact", 0.3, 0, 1, "admin", "private", 0),
        # High everything but tombstoned — BLOCKED
        ("tombstoned fact", "fact", 1.0, 10, 100, "admin", "private", 1),
        # user_id mismatch — BLOCKED
        ("other user fact", "fact", 1.0, 5, 100, "sunday", "private", 0),
    ]
    for content, kind, imp, dq, ac, user_id, tier, ts in rows:
        conn.execute(
            "INSERT INTO memory_canonical (content, kind, importance, "
            "distinct_queries_hit, access_count, user_id, tier, tombstoned) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (content, kind, imp, dq, ac, user_id, tier, ts),
        )
    conn.commit()
    return conn


class TestExportMemory(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = Path(self.tmpdir) / "test_bus.db"
        self.conn = _make_test_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_T1_gate_blocks_low_quality(self):
        facts = _fetch_facts(self.conn, "admin", None, include_all=False, top=100)
        contents = [f["content"] for f in facts]
        self.assertIn("T1 gate pass", contents) if False else None  # see below
        # Should have exactly 2: high-imp + high-dq
        self.assertEqual(len(facts), 2)
        self.assertIn("[LESSON] high imp fact", contents)
        self.assertIn("high dq fact", contents)
        # Should NOT include
        self.assertNotIn("trash fact", contents)
        self.assertNotIn("tombstoned fact", contents)
        self.assertNotIn("other user fact", contents)

    def test_include_all_bypasses_T1(self):
        facts = _fetch_facts(self.conn, "admin", None, include_all=True, top=100)
        # 3 non-tombstoned admin facts (high-imp / high-dq / trash), other-user excluded
        self.assertEqual(len(facts), 3)

    def test_top_limit(self):
        facts = _fetch_facts(self.conn, "admin", None, include_all=True, top=1)
        self.assertEqual(len(facts), 1)

    def test_markdown_format(self):
        facts = _fetch_facts(self.conn, "admin", None, include_all=True, top=100)
        md = _format_markdown(facts)
        self.assertIn("# Memory Export", md)
        self.assertIn("## Lesson", md)
        self.assertIn("## Fact", md)
        self.assertIn("[LESSON] high imp fact", md)

    def test_json_format(self):
        facts = _fetch_facts(self.conn, "admin", None, include_all=True, top=2)
        j = json.loads(_format_json(facts))
        self.assertEqual(j["count"], 2)
        self.assertIn("facts", j)
        self.assertIn("distinct_queries_hit", j["facts"][0])

    def test_csv_format(self):
        facts = _fetch_facts(self.conn, "admin", None, include_all=True, top=3)
        csv_str = _format_csv(facts)
        reader = csv.DictReader(io.StringIO(csv_str))
        rows = list(reader)
        self.assertEqual(len(rows), 3)
        self.assertIn("id", rows[0])
        self.assertIn("distinct_queries_hit", rows[0])

    def test_agent_context_caps_at_30(self):
        # Build 50 facts
        for i in range(50):
            self.conn.execute(
                "INSERT INTO memory_canonical (content, kind, importance, "
                "distinct_queries_hit, access_count, user_id, tier, tombstoned) "
                "VALUES (?, 'fact', 1.0, 0, 0, 'admin', 'private', 0)",
                (f"fact {i}",),
            )
        self.conn.commit()
        facts = _fetch_facts(self.conn, "admin", None, include_all=True, top=100)
        ctx = _format_agent_context(facts)
        # Only 30 numbered bullets
        bullet_count = sum(1 for line in ctx.split("\n") if line[:3] == "1. " or line[:3] == "2. " or (len(line) > 3 and line[0].isdigit() and line[1] == "."))
        self.assertLessEqual(bullet_count, 30)

    def test_cli_format_all_creates_directory(self):
        out_dir = Path(self.tmpdir) / "exports"
        # CLI looks at <astor-dir>/users/<user>/memory/astor_bus_<user>.db
        # or <astor-dir>/public/memory/astor_bus_public.db
        target_dir = Path(self.tmpdir) / "users" / "admin" / "memory"
        target_dir.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy(self.db_path, target_dir / "astor_bus_admin.db")
        old_argv = sys.argv
        sys.argv = ["export_memory",
                    "--format", "all",
                    "--user", "admin",
                    "--astor-dir", self.tmpdir,
                    "--output", str(out_dir),
                    "--top", "10"]
        try:
            rc = main()
            self.assertEqual(rc, 0, msg=("rc=" + str(rc) + " — exporter likely could not locate DB"))
            # Check 4 files
            self.assertTrue((out_dir / "memory.md").exists())
            self.assertTrue((out_dir / "memory.json").exists())
            self.assertTrue((out_dir / "memory.csv").exists())
            self.assertTrue((out_dir / "memory.txt").exists())
        finally:
            sys.argv = old_argv


if __name__ == "__main__":
    unittest.main()