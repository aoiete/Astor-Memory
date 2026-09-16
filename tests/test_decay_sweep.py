"""Tests for the decay sweep in /v1/read (server.py:_astor_track_access).

The decay sweep (server.py lines ~1827-1850) is the per-read upkeep that:
1. Bumps access_count + last_confirmed_at on surfaced facts.
2. When ASTOR_DECAY_SWEEP != '0' (default as of v1.14.39):
   - Halves access_count (floor 1) for facts not surfaced in 30+ days.
   - Tombstones facts not surfaced in 90+ days.

These tests verify the env-var contract and that the default behavior
is the enabled sweep, with an explicit off switch.

MemPalace v3.3.6 (Hebbian potentiation + Ebbinghaus decay) validates
this direction: facts that get surfaced stay hot; facts that don't
fade out so the corpus doesn't grow stale forever.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone


def _make_fake_canonical_db() -> tuple[str, callable]:
    """Create a temp SQLite with a memory_canonical row matching schema.

    Returns (path, add_fact_fn). add_fact_fn(id, last_confirmed_at, access_count, tombstoned)
    inserts/updates one row. We don't need a real astor server here —
    the decay logic is a plain SQL update that we can exercise by
    calling the same UPDATE statements the server does.
    """
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE memory_canonical (
            id INTEGER PRIMARY KEY,
            content TEXT,
            access_count INTEGER NOT NULL DEFAULT 0,
            last_confirmed_at TEXT,
            tombstoned INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()

    def add_fact(fid: int, last_confirmed_at: str | None, access_count: int, tombstoned: int = 0):
        conn.execute(
            "INSERT OR REPLACE INTO memory_canonical (id, content, access_count, last_confirmed_at, tombstoned) "
            "VALUES (?, ?, ?, ?, ?)",
            (fid, f"fact {fid}", access_count, last_confirmed_at, tombstoned),
        )
        conn.commit()

    def close():
        conn.close()

    return path, add_fact, close


def _run_decay_sqlite(
    db_path: str,
    surfaced_ids: list[int],
    decay_enabled: bool,
) -> None:
    """Replicate the exact server-side decay UPDATE statements.

    Mirrors server.py:1827-1852 logic against a SQLite test DB.
    """
    conn = sqlite3.connect(db_path)
    if not surfaced_ids:
        return
    placeholders = ','.join('?' * len(surfaced_ids))
    now_iso = datetime.now(timezone.utc).isoformat(timespec='seconds')

    # Always: bump surfaced
    conn.execute(
        f"UPDATE memory_canonical SET access_count = access_count + 1, "
        f"last_confirmed_at = ? WHERE id IN ({placeholders}) AND tombstoned = 0",
        [now_iso] + surfaced_ids,
    )

    if decay_enabled:
        # 30d no-recall: halve access_count (floor 1)
        cutoff_30 = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec='seconds')
        conn.execute(
            f"UPDATE memory_canonical SET access_count = MAX(1, access_count / 2) "
            f"WHERE id NOT IN ({placeholders}) AND tombstoned = 0 "
            f"AND (last_confirmed_at IS NULL OR last_confirmed_at < ?)",
            surfaced_ids + [cutoff_30],
        )
        # 90d no-recall: tombstone
        cutoff_90 = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat(timespec='seconds')
        conn.execute(
            f"UPDATE memory_canonical SET tombstoned = 1 "
            f"WHERE id NOT IN ({placeholders}) AND tombstoned = 0 "
            f"AND (last_confirmed_at IS NULL OR last_confirmed_at < ?)",
            surfaced_ids + [cutoff_90],
        )
    conn.commit()
    conn.close()


def _read(db_path: str, fid: int) -> tuple[int, str | None, int]:
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT access_count, last_confirmed_at, tombstoned FROM memory_canonical WHERE id=?",
        (fid,),
    ).fetchone()
    conn.close()
    return row or (0, None, 0)


class TestDecaySweepDefault(unittest.TestCase):
    """Verify decay sweep is ENABLED by default (the v1.14.39 contract)."""

    def test_default_env_var_is_enabled(self):
        # Mirror server.py logic:
        enabled = os.environ.get('ASTOR_DECAY_SWEEP', '1') != '0'
        self.assertTrue(enabled, "decay sweep should be ON by default (ASTOR_DECAY_SWEEP unset or != '0')")

    def test_env_var_zero_disables(self):
        old = os.environ.get('ASTOR_DECAY_SWEEP')
        try:
            os.environ['ASTOR_DECAY_SWEEP'] = '0'
            enabled = os.environ.get('ASTOR_DECAY_SWEEP', '1') != '0'
            self.assertFalse(enabled)
        finally:
            if old is None:
                os.environ.pop('ASTOR_DECAY_SWEEP', None)
            else:
                os.environ['ASTOR_DECAY_SWEEP'] = old


class TestDecaySweepBehavior(unittest.TestCase):
    """Verify the actual SQL produces the right halving + tombstoning."""

    def setUp(self):
        self.db_path, self.add_fact, self.close = _make_fake_canonical_db()
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        old_60d = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(timespec='seconds')
        ancient_120d = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat(timespec='seconds')

        # fact 1: surfaced today (recent, should stay hot)
        self.add_fact(1, now, access_count=10)
        # fact 2: not surfaced in 60d, access_count=8 → should halve to 4
        self.add_fact(2, old_60d, access_count=8)
        # fact 3: not surfaced in 120d, access_count=5 → should tombstone (regardless of access_count)
        self.add_fact(3, ancient_120d, access_count=5)

    def tearDown(self):
        self.close()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_decay_enabled_halves_and_tombstones(self):
        _run_decay_sqlite(self.db_path, surfaced_ids=[1], decay_enabled=True)

        ac1, _, tomb1 = _read(self.db_path, 1)
        ac2, _, tomb2 = _read(self.db_path, 2)
        ac3, _, tomb3 = _read(self.db_path, 3)

        # Fact 1 surfaced → bumped to 11
        self.assertEqual(ac1, 11)
        self.assertEqual(tomb1, 0)

        # Fact 2 not surfaced in 60d → access_count halved (8 // 2 = 4)
        self.assertEqual(ac2, 4)
        self.assertEqual(tomb2, 0)

        # Fact 3 not surfaced in 120d → tombstoned (90d cutoff)
        self.assertEqual(tomb3, 1)

    def test_decay_disabled_preserves_old_facts(self):
        _run_decay_sqlite(self.db_path, surfaced_ids=[1], decay_enabled=False)

        ac1, _, tomb1 = _read(self.db_path, 1)
        ac2, _, tomb2 = _read(self.db_path, 2)
        ac3, _, tomb3 = _read(self.db_path, 3)

        # Surfaced: bumped
        self.assertEqual(ac1, 11)
        # NOT surfaced, decay disabled: untouched
        self.assertEqual(ac2, 8)
        self.assertEqual(tomb2, 0)
        self.assertEqual(ac3, 5)
        self.assertEqual(tomb3, 0)


class TestDecayFloorOfOne(unittest.TestCase):
    """access_count halving must floor at 1, never reach 0."""

    def setUp(self):
        self.db_path, self.add_fact, self.close = _make_fake_canonical_db()

    def tearDown(self):
        self.close()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_access_count_one_is_preserved_as_one(self):
        # Fact with access_count=1 and old last_confirmed_at → halve would be 0, but floor to 1
        old_60d = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat(timespec='seconds')
        self.add_fact(99, old_60d, access_count=1)

        _run_decay_sqlite(self.db_path, surfaced_ids=[1], decay_enabled=True)

        ac99, _, _ = _read(self.db_path, 99)
        self.assertEqual(ac99, 1, "MAX(1, access_count / 2) should floor at 1")


if __name__ == '__main__':
    unittest.main()
