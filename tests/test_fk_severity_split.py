"""Regression tests for the v1.17.37 foreign-key severity split.

The bug this locks: /v1/health/diagnose reported every foreign-key violation
row, including tombstoned ones, so ship_c_warn was permanently True on a healthy
database. A warn flag that never clears is worse than no flag -- the operator
learns to ignore it and real failures go unseen.

The fix splits the count into live (tombstoned = 0) and legacy (tombstoned != 0),
and keys ship_c_warn on the live count only.

These tests drive the SHIPPED endpoint code, not a copy of it: they build a real
bus database, run the real /v1/health/diagnose handler against it, and assert on
the response. Reverting the fix makes them fail.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from astor_memory import server as astor_server  # noqa: E402


def _make_bus(tmp_path: Path) -> Path:
    """A bus db with the tables diagnose touches, plus canonical rows whose
    candidate_id points at a non-existent parent."""
    db = tmp_path / "users" / "admin" / "memory" / "astor_bus_admin.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.executescript(
        """
        CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT, namespace TEXT,
                             agent_id TEXT, source TEXT, action TEXT,
                             content TEXT, metadata TEXT);
        CREATE TABLE memory_candidates (id INTEGER PRIMARY KEY, event_id INTEGER,
                                        namespace TEXT, content TEXT, kind TEXT);
        CREATE TABLE memory_canonical (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            event_id INTEGER NOT NULL,
            namespace TEXT NOT NULL DEFAULT 'admin',
            content TEXT NOT NULL DEFAULT '',
            tombstoned INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (candidate_id) REFERENCES memory_candidates(id),
            FOREIGN KEY (event_id) REFERENCES events(id)
        );
        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            event TEXT NOT NULL,
            actor TEXT NOT NULL,
            target_type TEXT,
            target_id TEXT,
            old_state TEXT,
            new_state TEXT,
            reason TEXT,
            metadata TEXT NOT NULL DEFAULT '{}',
            severity TEXT NOT NULL DEFAULT 'info'
        );
        """
    )
    con.execute("INSERT INTO events (id, ts, content) VALUES (1, '2026-01-01', 'e')")
    con.execute("INSERT INTO memory_candidates (id, event_id, content) VALUES (100, 1, 'c')")
    con.commit()
    con.close()
    return db


def _insert_sentinel(con: sqlite3.Connection, row_id: int) -> None:
    """A TEXT sentinel in an INTEGER FK column -- what the one-shot
    public->source promotion wrote on UNIQUE-conflict rows. Violates the FK by
    type, not by a missing parent, and the fact is live and recallable."""
    con.execute(
        "INSERT INTO memory_canonical (id, candidate_id, event_id, content) "
        "VALUES (?, 'from_public:1198', 'migrated:public:_:long_term:abc', 'sentinel')",
        (row_id,),
    )


def _diagnose(astor_dir):
    app = astor_server.create_app()
    client = app.test_client()
    resp = client.get("/v1/health/diagnose?user=admin&astor_dir=%s" % astor_dir.as_posix())
    assert resp.status_code == 200, resp.data[:400]
    return resp.get_json()


def test_healthy_database_reports_no_violations(tmp_path):
    db = _make_bus(tmp_path)
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO memory_canonical (id, candidate_id, event_id) VALUES (1, 100, 1)"
    )
    con.commit()
    con.close()

    payload = _diagnose(tmp_path)
    chk = payload["db_corruption_check"]
    assert chk["foreign_key_violations"] == 0
    assert chk["foreign_key_violations_live"] == 0


def test_live_and_legacy_violations_are_counted_separately(tmp_path):
    db = _make_bus(tmp_path)
    con = sqlite3.connect(str(db))
    # One live orphan (real defect) and one tombstoned orphan (history).
    con.execute(
        "INSERT INTO memory_canonical (id, candidate_id, event_id, content) "
        "VALUES (1, 900, 1, 'live orphan')"
    )
    con.execute(
        "INSERT INTO memory_canonical (id, candidate_id, event_id, content, tombstoned) "
        "VALUES (2, 901, 1, 'dead orphan', 1)"
    )
    con.commit()
    con.close()

    payload = _diagnose(tmp_path)
    chk = payload["db_corruption_check"]
    # One orphan row trips TWO FK constraints (candidate_id and event_id), so the
    # raw violation count is 2 while the row count is 1. Both are reported; the
    # buckets are counted in ROWS so they add up.
    assert chk["foreign_key_violations_rows"] == 2, "two distinct canonical rows violate"
    assert chk["foreign_key_violations_live"] == 1, "only the tombstoned=0 row is live"
    assert chk["foreign_key_violations_legacy"] == 1
    assert chk["foreign_key_violations_defect"] == 1, "the live orphan is a real defect"
    assert chk["foreign_key_violations_sentinel"] == 0
    # The invariant that the first cut of this change broke: the three buckets
    # partition the violating ROWS exactly.
    assert (
        chk["foreign_key_violations_live"]
        + chk["foreign_key_violations_legacy"]
        == chk["foreign_key_violations_rows"]
    )
    assert (
        chk["foreign_key_violations_sentinel"]
        + chk["foreign_key_violations_defect"]
        == chk["foreign_key_violations_live"]
    )


def test_a_live_violation_raises_ship_c_warn(tmp_path):
    db = _make_bus(tmp_path)
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO memory_canonical (id, candidate_id, event_id, content) "
        "VALUES (1, 900, 1, 'live orphan')"
    )
    con.commit()
    con.close()

    payload = _diagnose(tmp_path)
    assert payload["ship_c_warn"] is True, "a live orphan is a real defect -- it must alarm"


def test_legacy_only_violations_do_not_raise_ship_c_warn(tmp_path):
    """The regression this whole change exists for.

    A database whose only FK violations are on tombstoned rows is healthy:
    those rows can never be recalled. Before v1.17.37 ship_c_warn was True here
    forever, so every real failure was buried under a permanent warn.
    """
    db = _make_bus(tmp_path)
    con = sqlite3.connect(str(db))
    for i in range(1, 4):
        con.execute(
            "INSERT INTO memory_canonical (id, candidate_id, event_id, content, tombstoned) "
            "VALUES (?, ?, 1, 'dead', 1)", (i, 900 + i)
        )
    con.commit()
    con.close()

    payload = _diagnose(tmp_path)
    chk = payload["db_corruption_check"]
    assert chk["foreign_key_violations"] == 3
    assert chk["foreign_key_violations_live"] == 0
    assert payload["ship_c_warn"] is False, (
        "legacy-only violations must not keep ship_c_warn True -- "
        "a warn that never clears hides real failures"
    )


def test_sentinel_violations_are_counted_separately(tmp_path):
    """A TEXT sentinel in an INTEGER FK column is not a missing parent.

    The one-shot public->source promotion wrote these on UNIQUE-conflict rows
    (NULL is not allowed there). They violate the FK by type. The fact is live
    and recall serves it normally, so it must not be counted as a defect.
    """
    db = _make_bus(tmp_path)
    con = sqlite3.connect(str(db))
    _insert_sentinel(con, 1)
    con.commit()
    con.close()

    payload = _diagnose(tmp_path)
    chk = payload["db_corruption_check"]
    assert chk["foreign_key_violations_live"] == 1
    assert chk["foreign_key_violations_sentinel"] == 1
    assert chk["foreign_key_violations_defect"] == 0, (
        "a sentinel row is history, not a live defect"
    )
    assert payload["ship_c_warn"] is False, (
        "sentinel violations must not keep ship_c_warn True"
    )


def test_a_real_live_orphan_still_raises_ship_c_warn(tmp_path):
    """The bucket split must not swallow real defects: a numeric candidate_id
    with no parent row is a genuine missing pointer."""
    db = _make_bus(tmp_path)
    con = sqlite3.connect(str(db))
    con.execute(
        "INSERT INTO memory_canonical (id, candidate_id, event_id, content) "
        "VALUES (1, 900, 1, 'real orphan')"
    )
    con.commit()
    con.close()

    payload = _diagnose(tmp_path)
    chk = payload["db_corruption_check"]
    assert chk["foreign_key_violations_defect"] == 1
    assert chk["foreign_key_violations_sentinel"] == 0
    assert payload["ship_c_warn"] is True, "a real orphan must still alarm"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
