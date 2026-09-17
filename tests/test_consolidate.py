"""Tests for astor_memory.consolidate (ADR-0007, v1.14.51).

Five tests covering each action + idempotency + dry-run safety.
Uses a temp SQLite DB mirroring memory_canonical schema.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Patch ASTOR_DIR env BEFORE importing consolidate
_TEST_TMP = tempfile.mkdtemp(prefix="astor_consolidate_test_")
os.environ["ASTOR_DIR"] = _TEST_TMP

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astor_memory.consolidate import (
    consolidate,
    ConsolidateReport,
    _classify_content,
    _action_dedup,
    _action_upgrade,
    _action_classify,
)


def _setup_fake_bus(tmp_path: Path) -> Path:
    """Create a temp ASTOR_DIR with admin private bus DB matching schema."""
    db_path = tmp_path / "users" / "admin" / "memory" / "astor_bus_admin.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    # Minimal schema (subset of real memory_canonical — enough for tests)
    conn.executescript("""
    CREATE TABLE memory_canonical (
        id INTEGER PRIMARY KEY,
        candidate_id INTEGER NOT NULL DEFAULT 0,
        event_id INTEGER NOT NULL DEFAULT 0,
        namespace TEXT,
        content TEXT,
        kind TEXT DEFAULT 'fact',
        confidence REAL DEFAULT 0.7,
        importance REAL DEFAULT 0.5,
        tags TEXT,
        metadata TEXT,
        keywords TEXT,
        context TEXT,
        promoted_at TEXT,
        promoted_by TEXT,
        last_confirmed_at TEXT,
        last_confirmed_session TEXT,
        access_count INTEGER DEFAULT 0,
        tombstoned INTEGER DEFAULT 0,
        tombstoned_at TEXT,
        expires_at TEXT,
        scene TEXT,
        revision INTEGER DEFAULT 0,
        parent_revision_id INTEGER,
        superseded_by INTEGER,
        origin_session_id TEXT,
        verdict TEXT,
        scope_type TEXT DEFAULT 'long_term',
        user_id TEXT DEFAULT 'admin',
        session_id TEXT,
        tier TEXT DEFAULT 'private',
        stable_id TEXT,
        embedding_version TEXT,
        publishable INTEGER DEFAULT 0,
        event_date TEXT,
        event_date_precision TEXT,
        parent_fact_ids TEXT,
        provenance_kind TEXT,
        provenance_agent TEXT,
        provenance_depth INTEGER DEFAULT 0,
        provenance_at TEXT,
        entities_json TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    conn.commit()
    conn.close()
    return db_path


def _insert_fact(
    db_path: Path,
    *,
    fact_id: int,
    content: str,
    importance: float = 0.5,
    access_count: int = 0,
    kind: str = "fact",
    tombstoned: int = 0,
    created_at: str = "2020-01-01T00:00:00Z",
):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """INSERT INTO memory_canonical
        (id, content, importance, access_count, kind, tombstoned, created_at, user_id, tier)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'admin', 'private')""",
        (fact_id, content, importance, access_count, kind, tombstoned, created_at),
    )
    conn.commit()
    conn.close()


class TestClassifyContent(unittest.TestCase):
    """Pure-function classification tests."""

    def test_classify_starts_with_success_pattern_marker(self):
        assert _classify_content("[success_pattern] verified path", "fact") == "success_pattern"

    def test_classify_starts_with_failure_pattern_marker(self):
        assert _classify_content("[failure_pattern] 走不通", "fact") == "failure_pattern"

    def test_classify_starts_with_LESSON_marker(self):
        assert _classify_content("[LESSON] 重要教训", "fact") == "lesson"

    def test_classify_already_classified_returns_none(self):
        # If kind != fact, no change
        assert _classify_content("[LESSON] foo", "lesson") is None

    def test_classify_content_with_marker_in_middle_returns_none(self):
        # Marker must be at the START (first line), not anywhere
        assert _classify_content("Some text\n\n[LESSON] marker", "fact") is None

    def test_classify_no_marker_returns_none(self):
        assert _classify_content("普通文本", "fact") is None


class TestConsolidateDryRun(unittest.TestCase):
    """Dry-run must not modify DB."""

    def setUp(self):
        # Each test gets its own subdir so consolidate() looks at
        # <subdir>/users/admin/memory/astor_bus_admin.db
        self.tmp_path = Path(_TEST_TMP) / self.id()
        self.tmp_path.mkdir(parents=True, exist_ok=True)
        self.astor_dir = self.tmp_path  # <-- this IS the astor_dir
        self.db_path = _setup_fake_bus(self.astor_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp_path), ignore_errors=True)

    def test_dry_run_makes_no_changes(self):
        # Add some dedup candidates
        _insert_fact(self.db_path, fact_id=1, content="[LESSON] same prefix repeated observation here")
        _insert_fact(self.db_path, fact_id=2, content="[LESSON] same prefix repeated observation again")
        _insert_fact(self.db_path, fact_id=3, content="[LESSON] same prefix repeated observation once more")
        report = consolidate(
            actions=["dedup", "upgrade", "classify"],
            dry_run=True,
            astor_dir=str(self.astor_dir),
            user_id="admin",
            tier="private",
            cap=100,
        )
        assert report.total_applied == 0
        assert report.total_proposed >= 1
        # Verify DB unchanged
        conn = sqlite3.connect(str(self.db_path))
        n_tombstoned = conn.execute(
            "SELECT COUNT(*) FROM memory_canonical WHERE tombstoned = 1"
        ).fetchone()[0]
        conn.close()
        assert n_tombstoned == 0

    def test_idempotent_second_run_yields_zero_changes(self):
        """First commit, second run produces 0 changes."""
        # Seed
        _insert_fact(self.db_path, fact_id=1, content="[LESSON] same prefix repeated observation here")
        _insert_fact(self.db_path, fact_id=2, content="[LESSON] same prefix repeated observation again")
        _insert_fact(self.db_path, fact_id=3, content="[LESSON] same prefix repeated observation once more")
        # First commit
        first = consolidate(
            actions=["dedup", "upgrade", "classify"],
            dry_run=False,
            astor_dir=str(self.astor_dir),
            user_id="admin",
            tier="private",
            cap=100,
        )
        applied_first = first.total_applied
        # Second run — should find 0 changes
        second = consolidate(
            actions=["dedup", "upgrade", "classify"],
            dry_run=False,
            astor_dir=str(self.astor_dir),
            user_id="admin",
            tier="private",
            cap=100,
        )
        assert second.total_applied == 0, f"second run should be idempotent; got {second.total_applied}"
        assert applied_first >= 1


class TestConsolidateDedup(unittest.TestCase):
    """Dedup action: tombstones near-duplicates."""

    def setUp(self):
        self.tmp_path = Path(_TEST_TMP) / self.id()
        self.tmp_path.mkdir(parents=True, exist_ok=True)
        self.astor_dir = self.tmp_path
        self.db_path = _setup_fake_bus(self.astor_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp_path), ignore_errors=True)

    def test_dedup_tombstones_similar_facts_keeps_one(self):
        # 3 facts with identical prefix
        _insert_fact(self.db_path, fact_id=10, content="[LESSON] cp /d/AI/scripts/admin/maker_pocket.py backup before edit")
        _insert_fact(self.db_path, fact_id=11, content="[LESSON] cp /d/AI/scripts/admin/maker_pocket.py backup before modify")
        _insert_fact(self.db_path, fact_id=12, content="[LESSON] cp /d/AI/scripts/admin/maker_pocket.py backup before change")
        report = consolidate(
            actions=["dedup"], dry_run=True,
            astor_dir=str(self.astor_dir), user_id="admin", tier="private", cap=100,
        )
        dedup_actions = [a for a in report.actions_proposed if a.action == "dedup"]
        assert len(dedup_actions) >= 1
        # Tombstone 2, keep 1
        assert "tombstoned" in dedup_actions[0].detail
        # Detail should mention 2 tombstoned IDs (11, 12)
        import re
        tomb_ids = re.findall(r"\d+", dedup_actions[0].detail.split("tombstoned [")[1].rstrip("]"))
        assert "11" in tomb_ids and "12" in tomb_ids

    def test_dedup_skips_unique_facts(self):
        # 2 facts with totally different prefixes
        _insert_fact(self.db_path, fact_id=20, content="AAA unique observation about alpha")
        _insert_fact(self.db_path, fact_id=21, content="ZZZ totally different observation about beta")
        report = consolidate(
            actions=["dedup"], dry_run=True,
            astor_dir=str(self.astor_dir), user_id="admin", tier="private", cap=100,
        )
        assert report.total_proposed == 0


class TestConsolidateUpgrade(unittest.TestCase):
    """Upgrade action: bumps importance for frequently-accessed facts."""

    def setUp(self):
        self.tmp_path = Path(_TEST_TMP) / self.id()
        self.tmp_path.mkdir(parents=True, exist_ok=True)
        self.astor_dir = self.tmp_path
        self.db_path = _setup_fake_bus(self.astor_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.tmp_path), ignore_errors=True)

    def test_upgrade_bumps_0_5_to_0_7_when_access_5(self):
        _insert_fact(self.db_path, fact_id=30, content="foo bar baz",
                     importance=0.5, access_count=5)
        consolidate(
            actions=["upgrade"], dry_run=False,
            astor_dir=str(self.astor_dir), user_id="admin", tier="private", cap=100,
        )
        conn = sqlite3.connect(str(self.db_path))
        new_imp = conn.execute(
            "SELECT importance FROM memory_canonical WHERE id = 30"
        ).fetchone()[0]
        conn.close()
        assert new_imp == 0.7

    def test_upgrade_skips_facts_below_threshold(self):
        _insert_fact(self.db_path, fact_id=31, content="foo bar baz",
                     importance=0.5, access_count=2)  # below 5
        consolidate(
            actions=["upgrade"], dry_run=False,
            astor_dir=str(self.astor_dir), user_id="admin", tier="private", cap=100,
        )
        conn = sqlite3.connect(str(self.db_path))
        new_imp = conn.execute(
            "SELECT importance FROM memory_canonical WHERE id = 31"
        ).fetchone()[0]
        conn.close()
        assert new_imp == 0.5  # unchanged


if __name__ == "__main__":
    unittest.main()
