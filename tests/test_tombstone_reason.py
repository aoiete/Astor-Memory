"""test_tombstone_reason.py — every death should record a cause.

2026-10-23 (Uni-Mem, arXiv 2610.29671): Uni-Mem's Event-Wise Credit
Assignment localises feedback to the events responsible for an outcome,
and propagates credit along the update lineage -- a record that was
later UPDATEd or DELETEd hands some of that credit to whoever changed
it. The point is that a deletion without a cause teaches the system
nothing.

Astor had the column (`invalidated_reason TEXT NOT NULL DEFAULT ''`)
and 95%+ of the dead rows had it empty: 4,421 of 4,641 in public,
8,550 of 8,910 in the admin private tier. Six tombstone paths wrote no
reason at all -- including /v1/forget, which is the path the cleanup
tool uses. So the write path could never be held accountable for the
debris it produced; the only way to find it was a regex over content
after the fact.

These tests pin the behaviour: a tombstoned row carries a non-empty
reason, and the reason says who did it.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from astor_memory.server import create_app, _forget_reason


def _bus_db(runtime: Path, tier: str, user_id: str | None = None) -> Path:
    if tier == "private":
        return (runtime / "users" / (user_id or "admin") / "memory" /
                f"astor_bus_{user_id or 'admin'}.db")
    return runtime / tier / "memory" / f"astor_bus_{tier}.db"


def _rows(runtime: Path, tier: str, user_id: str | None = None) -> list:
    db = _bus_db(runtime, tier, user_id)
    if not db.exists():
        return []
    con = sqlite3.connect(str(db))
    try:
        return con.execute(
            "SELECT id, content, tombstoned, "
            "COALESCE(invalidated_reason, '') FROM memory_canonical"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    runtime = tmp_path / "astor"
    monkeypatch.setenv("ASTOR_DIR", str(runtime))
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client(), runtime


def _write(client, text: str, tier: str = "public") -> dict:
    resp = client.post("/v1/write", json={
        "text": text, "tier": tier, "mode": "regex", "user": "admin",
        # v1.17.15: only a shareable kind lands in the public tier, and
        # these tests read the fact back from there.
        "kind": "lesson",
    })
    return resp.get_json() or {}


# --- the helper's vocabulary -------------------------------------------

def test_reason_defaults_are_never_empty():
    assert _forget_reason(None, False) == "user_request"
    assert _forget_reason(None, True) == "tombstone_request"
    assert _forget_reason("hygiene:test_residue", True) == "hygiene:test_residue"


def test_reason_is_truncated_not_dropped():
    long = "x" * 500
    out = _forget_reason(long, True)
    assert out and len(out) <= 120


# --- behaviour: the row actually carries the reason ---------------------

def test_forget_records_a_reason(client):
    c, runtime = client
    w = _write(c, "astor forget reason probe alpha beta")
    assert w.get("count", 0) >= 1, w
    fid = (w.get("fact_ids") or [None])[0]
    assert fid is not None

    resp = c.post("/v1/forget", json={
        "fact_id": fid, "tier": "public", "user_id": None,
        "tombstone_only": True,
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)[:200]

    rows = _rows(runtime, "public")
    row = next((r for r in rows if r[0] == fid), None)
    assert row is not None, "the row must survive as a tombstone"
    assert row[2] == 1, "the row must be tombstoned"
    assert row[3].strip(), "a tombstone without a reason teaches nothing"


def test_forget_records_the_caller_supplied_reason(client):
    c, runtime = client
    w = _write(c, "astor forget caller reason probe gamma delta")
    fid = (w.get("fact_ids") or [None])[0]
    assert fid is not None

    c.post("/v1/forget", json={
        "fact_id": fid, "tier": "public", "user_id": None,
        "tombstone_only": True, "reason": "hygiene:test_residue",
    })

    rows = _rows(runtime, "public")
    row = next((r for r in rows if r[0] == fid), None)
    assert row is not None
    assert row[3] == "hygiene:test_residue", row[3]


def test_reason_is_not_written_to_a_live_row(client):
    """A fact that was never forgotten must not carry a death reason."""
    c, runtime = client
    w = _write(c, "astor live row has no death reason epsilon zeta")
    fid = (w.get("fact_ids") or [None])[0]
    assert fid is not None
    rows = _rows(runtime, "public")
    row = next((r for r in rows if r[0] == fid), None)
    assert row is not None
    assert row[2] == 0
    assert not row[3].strip(), "a live fact has no reason to carry"
