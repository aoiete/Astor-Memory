"""test_graph_recall.py — Ship P1.2 (2026-09-28)

Hindsight-style graph recall tests. Verifies:
- SQL grammar compiles
- Returns matching facts + edges
- Tier + user_id filtering
- entity_types filter
- Case-insensitive substring match
- list_entities_by_freq aggregates correctly
- Empty DB returns empty
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory.nest.graph_recall import (
    graph_recall_by_entity,
    list_entities_by_freq,
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


def _make_minimal_bus_db() -> sqlite3.Connection:
    """Build a memory_canonical table that supports json_each."""
    conn = sqlite3.connect(":memory:")
    # json_extract / json_each require SQLite >=3.38 (built-in JSON1).
    # Build a minimal schema with the only columns graph_recall reads.
    conn.execute("""
        CREATE TABLE memory_canonical (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tier TEXT NOT NULL DEFAULT 'public',
            user_id TEXT,
            kind TEXT NOT NULL DEFAULT 'fact',
            content TEXT NOT NULL DEFAULT '',
            confidence REAL DEFAULT 0.7,
            promoted_at TEXT,
            tombstoned INTEGER DEFAULT 0,
            entities_json TEXT NOT NULL DEFAULT '[]'
        )
    """)
    conn.commit()
    return conn


def _insert(conn, content, entities, *, tier="public", user_id=None,
            kind="fact", promoted_at="2026-01-01T00:00:00Z"):
    """Helper: insert a fact with entities_json."""
    conn.execute(
        "INSERT INTO memory_canonical "
        "(tier, user_id, kind, content, promoted_at, entities_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tier, user_id, kind, content, promoted_at,
         json.dumps(entities)),
    )


def main() -> int:
    print("=== Unit cases ===")

    # 1) Empty DB → empty results
    conn = _make_minimal_bus_db()
    res = graph_recall_by_entity(conn, "Hermes")
    _eq("empty DB count", res["count"], 0)
    _eq("empty DB facts", res["facts"], [])
    _eq("empty DB edges", res["edges"], [])
    conn.close()

    # 2) Single match
    conn = _make_minimal_bus_db()
    _insert(conn, "Hermes is the agent runtime.",
            [{"type": "person", "value": "Hermes"}, {"type": "org", "value": "AI"}])
    _insert(conn, "Other fact about moomoo.",
            [{"type": "tool", "value": "moomoo"}])
    res = graph_recall_by_entity(conn, "Hermes")
    _eq("single match count", res["count"], 1)
    _truthy("single match content", "Hermes" in res["facts"][0]["content"])
    _eq("edge entity_value", res["edges"][0]["entity_value"], "Hermes")
    _eq("edge entity_type", res["edges"][0]["entity_type"], "person")
    conn.close()

    # 3) Case-insensitive
    conn = _make_minimal_bus_db()
    _insert(conn, "HERMES is uppercase.", [{"type": "person", "value": "HERMES"}])
    res = graph_recall_by_entity(conn, "hermes")
    _eq("case-insensitive count", res["count"], 1)
    conn.close()

    # 4) Substring match
    conn = _make_minimal_bus_db()
    _insert(conn, "moomoo broker integration.",
            [{"type": "tool", "value": "moomoo"}])
    res = graph_recall_by_entity(conn, "oomo")
    _eq("substring match count", res["count"], 1)
    conn.close()

    # 5) Tier filter
    conn = _make_minimal_bus_db()
    _insert(conn, "public Hermes", [{"type": "person", "value": "Hermes"}],
            tier="public")
    _insert(conn, "private Hermes", [{"type": "person", "value": "Hermes"}],
            tier="private")
    res = graph_recall_by_entity(conn, "Hermes", tier="public")
    _eq("tier filter count", res["count"], 1)
    _eq("tier filter content", "public" in res["facts"][0]["content"], True)
    conn.close()

    # 6) User filter
    conn = _make_minimal_bus_db()
    _insert(conn, "admin fact", [{"type": "person", "value": "Hermes"}],
            user_id="admin")
    _insert(conn, "alice fact", [{"type": "person", "value": "Hermes"}],
            user_id="alice")
    res = graph_recall_by_entity(conn, "Hermes", user_id="admin")
    _eq("user filter count", res["count"], 1)
    _eq("user filter user", res["facts"][0]["user_id"], "admin")
    conn.close()

    # 7) Entity types filter
    conn = _make_minimal_bus_db()
    _insert(conn, "Hermes person", [{"type": "person", "value": "Hermes"}])
    _insert(conn, "Hermes org", [{"type": "org", "value": "Hermes"}])
    res = graph_recall_by_entity(conn, "Hermes", entity_types=["person"])
    _eq("entity_types person count", res["count"], 1)
    _eq("entity_types person type",
        res["facts"][0]["matched_entity_type"], "person")
    conn.close()

    # 8) top_k cap
    conn = _make_minimal_bus_db()
    for i in range(5):
        _insert(conn, f"Hermes fact {i}", [{"type": "person", "value": "Hermes"}],
                promoted_at=f"2026-01-0{i+1}T00:00:00Z")
    res = graph_recall_by_entity(conn, "Hermes", top_k=3)
    _eq("top_k cap", len(res["facts"]), 3)
    _eq("top_k cap edges", len(res["edges"]), 3)
    conn.close()

    # 9) Tombstoned facts excluded
    conn = _make_minimal_bus_db()
    _insert(conn, "live Hermes", [{"type": "person", "value": "Hermes"}])
    _insert(conn, "dead Hermes", [{"type": "person", "value": "Hermes"}],
            promoted_at="2025-01-01T00:00:00Z")
    conn.execute("UPDATE memory_canonical SET tombstoned=1 WHERE content='dead Hermes'")
    res = graph_recall_by_entity(conn, "Hermes")
    _eq("tombstone exclusion", res["count"], 1)
    _truthy("tombstone: only live remains",
            "live Hermes" in res["facts"][0]["content"])
    conn.close()

    # 10) mental_model excluded (operator-curated, not recall surface)
    conn = _make_minimal_bus_db()
    _insert(conn, "mm about Hermes", [{"type": "person", "value": "Hermes"}],
            kind="mental_model")
    _insert(conn, "fact about Hermes", [{"type": "person", "value": "Hermes"}],
            kind="fact")
    res = graph_recall_by_entity(conn, "Hermes")
    _eq("mental_model excluded count", res["count"], 1)
    _eq("mental_model excluded kind", res["facts"][0]["kind"], "fact")
    conn.close()

    # 11) list_entities_by_freq aggregates
    conn = _make_minimal_bus_db()
    _insert(conn, "f1", [{"type": "person", "value": "Hermes"}])
    _insert(conn, "f2", [{"type": "person", "value": "Hermes"}])
    _insert(conn, "f3", [{"type": "person", "value": "moomoo"}])
    _insert(conn, "f4", [{"type": "tool", "value": "moomoo"}])
    res = list_entities_by_freq(conn, limit=10)
    _eq("freq top result entity", res[0]["entity_value"], "Hermes")
    _eq("freq top count", res[0]["fact_count"], 2)
    _truthy("freq: moomoo in result",
            any(r["entity_value"] == "moomoo" for r in res))
    conn.close()

    # 12) list_entities_by_freq entity_type filter
    conn = _make_minimal_bus_db()
    _insert(conn, "f1", [{"type": "person", "value": "Hermes"}])
    _insert(conn, "f2", [{"type": "tool", "value": "moomoo"}])
    res = list_entities_by_freq(conn, entity_type="person", limit=10)
    _eq("freq person filter count", len(res), 1)
    _eq("freq person filter value", res[0]["entity_value"], "Hermes")
    conn.close()

    print("\nAll tests PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
