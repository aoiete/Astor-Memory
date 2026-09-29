"""test_dashboard_staleness.py — Ship P3.1 staleness (2026-09-28)

Tests for _compute_staleness helper + staleness flag in panel items.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory.dashboard_data import (
    _compute_staleness,
    _mental_models_section,
    _knowledge_pages_section,
)


def _eq(label, got, expected):
    if got != expected:
        print(f"  [FAIL] {label}\n    got     : {got!r}\n    expected: {expected!r}")
        sys.exit(1)
    print(f"  [PASS] {label}")


def _truthy(label, got):
    if not got:
        print(f"  [FAIL] {label} (got falsy: {got!r})")
        sys.exit(1)
    print(f"  [PASS] {label}")


def main() -> int:
    print("=== Unit cases ===")

    # 1) Fresh timestamp → not stale
    now = datetime.now(timezone.utc)
    fresh_iso = (now - timedelta(days=5)).isoformat()
    st = _compute_staleness(fresh_iso)
    _truthy("fresh → age_days is float", isinstance(st["age_days"], float))
    _truthy("fresh → 5d < 30d", 4.5 < st["age_days"] < 5.5)
    _eq("fresh → not stale", st["is_stale"], False)

    # 2) Old timestamp → stale
    old_iso = (now - timedelta(days=60)).isoformat()
    st = _compute_staleness(old_iso)
    _truthy("60d → is_stale", st["is_stale"])
    _truthy("60d → age_days > 30", st["age_days"] > 30)

    # 3) Z suffix normalization
    st = _compute_staleness("2026-09-01T00:00:00Z")
    _truthy("Z suffix normalized", st["age_days"] is not None)

    # 4) Missing/empty → age_days=None, is_stale=False
    _eq("empty → None age", _compute_staleness(""), {"age_days": None, "is_stale": False, "stale_threshold_days": 30})
    _eq("None → None age", _compute_staleness(None), {"age_days": None, "is_stale": False, "stale_threshold_days": 30})

    # 5) Malformed → None
    st = _compute_staleness("not a date")
    _eq("malformed → None age", st["age_days"], None)
    _eq("malformed → not stale", st["is_stale"], False)

    # 6) Custom stale_days
    st = _compute_staleness((now - timedelta(days=10)).isoformat(), stale_days=7)
    _eq("stale_days=7, 10d old → stale", st["is_stale"], True)

    st = _compute_staleness((now - timedelta(days=5)).isoformat(), stale_days=7)
    _eq("stale_days=7, 5d old → fresh", st["is_stale"], False)

    # 7) Panel items carry staleness — integration with _mental_models_section
    tmp = tempfile.mkdtemp(prefix="astor_staleness_")
    try:
        tmpdir = Path(tmp)
        (tmpdir / "public" / "memory").mkdir(parents=True)
        pub = tmpdir / "public" / "memory" / "astor_bus_public.db"
        conn = sqlite3.connect(str(pub))
        conn.execute(
            "CREATE TABLE memory_canonical ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "tier TEXT NOT NULL DEFAULT 'public',"
            "user_id TEXT,"
            "kind TEXT NOT NULL DEFAULT 'fact',"
            "content TEXT NOT NULL DEFAULT '',"
            "confidence REAL DEFAULT 0.7,"
            "promoted_at TEXT,"
            "tombstoned INTEGER DEFAULT 0,"
            "metadata TEXT)"
        )
        # 1 fresh + 2 stale
        fresh_iso = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        stale_iso = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
        for i, p in enumerate([fresh_iso, stale_iso, stale_iso]):
            conn.execute(
                "INSERT INTO memory_canonical (tier, user_id, kind, content, "
                "confidence, promoted_at, tombstoned) "
                "VALUES (?, ?, ?, ?, ?, ?, 0)",
                ("public", None, "mental_model",
                 f"[MM] question: Q{i}?" + chr(10) + f"answer: A{i}",
                 0.7, p),
            )
        conn.commit()
        conn.close()
        del conn

        mm = _mental_models_section(tmpdir)
        _eq("mental_models count = 3", mm["count"], 3)
        _eq("mental_models stale_count = 2", mm["stale_count"], 2)
        fresh_items = [x for x in mm["items"] if not x["is_stale"]]
        stale_items = [x for x in mm["items"] if x["is_stale"]]
        _eq("1 fresh in items", len(fresh_items), 1)
        _eq("2 stale in items", len(stale_items), 2)
        _truthy("stale item has age_days",
                all(x["age_days"] > 30 for x in stale_items))
        _truthy("fresh item has age_days < 30",
                fresh_items[0]["age_days"] < 30)
        _truthy("items sorted by promoted_at desc",
                mm["items"][0]["promoted_at"] > mm["items"][-1]["promoted_at"])

        # 8) Knowledge page staleness
        conn = sqlite3.connect(str(pub))
        conn.execute(
            "INSERT INTO memory_canonical (tier, user_id, kind, content, "
            "confidence, promoted_at, tombstoned, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            ("public", None, "knowledge_page",
             "[KP] slug: stale-page" + chr(10) +
             "title: Old Page" + chr(10) +
             "updated: " + stale_iso + chr(10) + chr(10) + "body",
             0.7, stale_iso, json.dumps({"parent_fact_ids": []})),
        )
        conn.execute(
            "INSERT INTO memory_canonical (tier, user_id, kind, content, "
            "confidence, promoted_at, tombstoned, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            ("public", None, "knowledge_page",
             "[KP] slug: fresh-page" + chr(10) +
             "title: New Page" + chr(10) +
             "updated: " + fresh_iso + chr(10) + chr(10) + "body",
             0.7, fresh_iso, json.dumps({"parent_fact_ids": []})),
        )
        conn.commit()
        conn.close()
        del conn

        kp = _knowledge_pages_section(tmpdir)
        _eq("kp count = 2", kp["count"], 2)
        _eq("kp stale_count = 1", kp["stale_count"], 1)
        kp_stale = [x for x in kp["items"] if x["is_stale"]]
        _eq("1 stale kp in items", len(kp_stale), 1)
        _eq("stale kp slug", kp_stale[0]["slug"], "stale-page")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nAll tests PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
