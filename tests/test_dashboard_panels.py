"""test_dashboard_panels.py — Ship P3.1 dashboard (2026-09-28)

Mental Models + Knowledge Pages dashboard panel tests:
- _mental_models_section scans public bus
- _knowledge_pages_section scans public bus
- Both return empty when no rows
- by_tier counts match
- Items sorted by promoted_at DESC
- Multi-user (private_<u>) tiers counted
- limit cap works
- Answer preview truncation (>200 chars → ellipsis)
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory.dashboard_data import (
    _mental_models_section, _knowledge_pages_section,
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


def _setup_dirs(tmpdir: Path) -> Path:
    public_mem = tmpdir / "public" / "memory"
    public_mem.mkdir(parents=True)
    admin_mem = tmpdir / "users" / "admin" / "memory"
    admin_mem.mkdir(parents=True)
    return tmpdir


def _open_bus(path: Path) -> sqlite3.Connection:
    """Open or create a memory_canonical table. Idempotent."""
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_canonical (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tier TEXT NOT NULL DEFAULT 'public',
            user_id TEXT,
            kind TEXT NOT NULL DEFAULT 'fact',
            content TEXT NOT NULL DEFAULT '',
            confidence REAL DEFAULT 0.7,
            promoted_at TEXT,
            tombstoned INTEGER DEFAULT 0,
            metadata TEXT
        )
    """)
    conn.commit()
    return conn


def _insert_fact(conn, kind, content, conf, promoted, uid, tombstoned=0, metadata=None):
    conn.execute(
        "INSERT INTO memory_canonical (tier, user_id, kind, content, "
        "confidence, promoted_at, tombstoned, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("public", uid, kind, content, conf, promoted, tombstoned,
         json.dumps(metadata) if metadata else None),
    )


def _insert_private_fact(conn, kind, content, conf, promoted, uid, tombstoned=0, metadata=None):
    conn.execute(
        "INSERT INTO memory_canonical (tier, user_id, kind, content, "
        "confidence, promoted_at, tombstoned, metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("private", uid, kind, content, conf, promoted, tombstoned,
         json.dumps(metadata) if metadata else None),
    )


def main() -> int:
    print("=== Unit cases ===")
    tmp = tempfile.mkdtemp(prefix="astor_test_")
    try:
        tmpdir = _setup_dirs(Path(tmp))
        astor = tmpdir

        # 1) Empty ASTOR dir → both sections empty
        mm = _mental_models_section(astor)
        kp = _knowledge_pages_section(astor)
        _eq("empty mental_models count", mm["count"], 0)
        _eq("empty mental_models items", mm["items"], [])
        _eq("empty knowledge_pages count", kp["count"], 0)
        _eq("empty knowledge_pages items", kp["items"], [])

        # 2) Mental models in public
        pub_path = astor / "public" / "memory" / "astor_bus_public.db"
        pub_conn = _open_bus(pub_path)
        _insert_fact(pub_conn, "mental_model",
                     "[MM] question: Q1?" + chr(10) + "answer: A1",
                     0.8, "2026-09-01T00:00:00Z", None)
        _insert_fact(pub_conn, "mental_model",
                     "[MM] question: Q2?" + chr(10) + "answer: A2",
                     0.7, "2026-09-02T00:00:00Z", "alice")
        pub_conn.commit()
        pub_conn.close()
        del pub_conn

        mm = _mental_models_section(astor)
        _eq("public mental_models count", mm["count"], 2)
        _eq("public by_tier", mm["by_tier"].get("public"), 2)
        _eq("mental_models items count", len(mm["items"]), 2)
        _eq("first item question", mm["items"][0]["question"], "Q2?")
        _eq("first item answer_preview", mm["items"][0]["answer_preview"], "A2")
        _eq("second item question", mm["items"][1]["question"], "Q1?")

        # 3) Knowledge page in public
        pub_conn = _open_bus(pub_path)
        # Existing mental_models stay; append kp
        _insert_fact(pub_conn, "knowledge_page",
                     "[KP] slug: astor-arch" + chr(10) +
                     "title: Architecture" + chr(10) +
                     "updated: 2026-09-03T00:00:00Z" + chr(10) + chr(10) + "Body",
                     0.9, "2026-09-03T00:00:00Z", None)
        pub_conn.commit()
        pub_conn.close()
        del pub_conn

        kp = _knowledge_pages_section(astor)
        _eq("public kp by_tier", kp["by_tier"].get("public"), 1)
        _eq("kp items count", len(kp["items"]), 1)
        _eq("kp item slug", kp["items"][0]["slug"], "astor-arch")
        _eq("kp item title", kp["items"][0]["title"], "Architecture")
        _eq("kp parent_fact_count", kp["items"][0]["parent_fact_count"], 0)

        # 4) Multi-user (admin private)
        admin_path = astor / "users" / "admin" / "memory" / "astor_bus_admin.db"
        admin_conn = _open_bus(admin_path)
        _insert_private_fact(admin_conn, "mental_model",
                             "[MM] question: Admin Q?" + chr(10) + "answer: Admin A",
                             0.85, "2026-09-04T00:00:00Z", "admin")
        admin_conn.commit()
        admin_conn.close()
        del admin_conn

        mm = _mental_models_section(astor)
        _eq("multi-user mental_models count", mm["count"], 3)
        _truthy("multi-user admin tier present",
                "private:admin" in mm["by_tier"])
        _eq("admin tier count", mm["by_tier"]["private:admin"], 1)

        # 5) tombstoned excluded
        pub_conn = _open_bus(pub_path)
        _insert_fact(pub_conn, "mental_model",
                     "[MM] question: Dead?" + chr(10) + "answer: gone",
                     0.5, "2026-09-05T00:00:00Z", None, tombstoned=1)
        pub_conn.commit()
        pub_conn.close()
        del pub_conn

        mm = _mental_models_section(astor)
        _eq("tombstone excluded count", mm["count"], 3)  # still 3, not 4

        # 6) limit cap
        pub_conn = _open_bus(pub_path)
        for i in range(5):
            _insert_fact(pub_conn, "mental_model",
                         "[MM] question: Q" + str(i) + "?" + chr(10) +
                         "answer: A" + str(i),
                         0.5, f"2026-09-1{i+5}T00:00:00Z", None)
        pub_conn.commit()
        pub_conn.close()
        del pub_conn

        mm = _mental_models_section(astor, limit=3)
        _eq("limit=3 items count", len(mm["items"]), 3)

        # 7) Knowledge page with parent_fact_ids in metadata
        pub_conn = _open_bus(pub_path)
        _insert_fact(pub_conn, "knowledge_page",
                     "[KP] slug: linked" + chr(10) +
                     "title: Linked Page" + chr(10) +
                     "updated: 2026-09-10T00:00:00Z" + chr(10) + chr(10) + "body",
                     0.9, "2026-09-10T00:00:00Z", None,
                     metadata={"parent_fact_ids": [1, 2, 3, 4, 5]})
        pub_conn.commit()
        pub_conn.close()
        del pub_conn

        kp = _knowledge_pages_section(astor)
        _truthy("kp with parents parsed", any(
            x["slug"] == "linked" and x["parent_fact_count"] == 5
            for x in kp["items"]
        ))

        # 8) Answer preview truncation (>200 chars → ellipsis)
        long_answer = "A" * 300
        pub_conn = _open_bus(pub_path)
        _insert_fact(pub_conn, "mental_model",
                     "[MM] question: Long?" + chr(10) + "answer: " + long_answer,
                     0.5, "2026-09-15T00:00:00Z", None)
        pub_conn.commit()
        pub_conn.close()
        del pub_conn

        mm = _mental_models_section(astor)
        long_items = [x for x in mm["items"] if x["question"] == "Long?"]
        _truthy("long answer found", len(long_items) > 0)
        _truthy("long answer truncated with ellipsis",
                long_items[0]["answer_preview"].endswith("..."))
        _truthy("long answer ≤ 203 chars",
                len(long_items[0]["answer_preview"]) <= 203)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nAll tests PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
