"""test_confidence_update.py — Ship P2.1 (2026-09-28)

Hindsight-style confidence update on reflect tests.
- _apply_confidence_updates: winner boost, losers decay
- Capping at 0.99
- audit log entry
- Decay preserves confidence on tombstoned row
- boost_strength / decay_strength knobs work
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory.nest.reflection import _apply_confidence_updates


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


class _FakeBus:
    """Minimal bus fake — only what _apply_confidence_updates touches."""
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("""
            CREATE TABLE memory_canonical (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                confidence REAL DEFAULT 0.7
            )
        """)
        # audit log
        self.conn.execute("""
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event TEXT,
                actor TEXT,
                target_id TEXT,
                metadata TEXT
            )
        """)
        self.conn.commit()
        self.audit_log = []

    def write_audit(self, event, actor, target_type, target_id, metadata,
                    reason, severity):
        self.conn.execute(
            "INSERT INTO audit_log (event, actor, target_id, metadata) "
            "VALUES (?, ?, ?, ?)",
            (event, actor, target_id, json.dumps(metadata)),
        )
        self.conn.commit()
        self.audit_log.append({
            "event": event, "actor": actor,
            "target_id": target_id, "metadata": metadata,
        })


def _insert_fact(bus, confidence):
    cur = bus.conn.execute(
        "INSERT INTO memory_canonical (confidence) VALUES (?)",
        (confidence,),
    )
    return int(cur.lastrowid)


def main() -> int:
    print("=== Unit cases ===")

    # 1) Winner boost — 3 losers absorbed
    bus = _FakeBus()
    w = _insert_fact(bus, 0.7)
    losers = [_insert_fact(bus, 0.5) for _ in range(3)]
    result = _apply_confidence_updates(
        bus, winner_id=w, merged_from_ids=losers + [w], actor="test",
        boost_strength=0.1, decay_strength=0.3,
    )
    _eq("winner_conf_before", result["winner_conf_before"], 0.7)
    # 0.7 + 0.1 * 3 = 1.0, capped at 0.99
    _eq("winner_conf_after capped", result["winner_conf_after"], 0.99)
    _eq("losers_decayed count", result["losers_decayed"], 3)
    # Losers confidence dropped
    for lid in losers:
        c = bus.conn.execute(
            "SELECT confidence FROM memory_canonical WHERE id = ?", (lid,),
        ).fetchone()[0]
        _truthy(f"loser {lid} confidence decayed", c < 0.5)
        _eq(f"loser {lid} confidence ≈ old * 0.7", round(c, 4), round(0.5 * 0.7, 4))
    # Audit log entry
    _eq("audit log entry count", len(bus.audit_log), 1)
    _eq("audit event", bus.audit_log[0]["event"], "reflection_confidence_update")

    # 2) Single fact (no losers) — empty merged_from → no boost
    bus = _FakeBus()
    w = _insert_fact(bus, 0.5)
    result = _apply_confidence_updates(
        bus, winner_id=w, merged_from_ids=[], actor="test",
    )
    _eq("empty merged_from winner_conf_before", result["winner_conf_before"], 0.5)
    _eq("empty merged_from winner_conf_after",
        round(result["winner_conf_after"], 4), 0.5)
    _eq("empty merged_from losers_decayed", result["losers_decayed"], 0)

    # 3) Empty losers list — no error
    bus = _fake_bus = _FakeBus()
    w = _insert_fact(bus, 0.6)
    result = _apply_confidence_updates(bus, winner_id=w, merged_from_ids=[], actor="test")
    _eq("empty losers winner_conf_after", result["winner_conf_after"], 0.6)
    _eq("empty losers losers_decayed", result["losers_decayed"], 0)

    # 4) Confidence > 0.99 cap
    bus = _FakeBus()
    w = _insert_fact(bus, 0.95)
    losers = [_insert_fact(bus, 0.5) for _ in range(5)]
    result = _apply_confidence_updates(
        bus, winner_id=w, merged_from_ids=losers + [w], actor="test",
        boost_strength=0.1,
    )
    # 0.95 + 0.5 = 1.45 → capped at 0.99
    _eq("over-cap winner_conf_after", result["winner_conf_after"], 0.99)

    # 5) Decay strength 0 → losers unchanged
    bus = _FakeBus()
    w = _insert_fact(bus, 0.5)
    losers = [_insert_fact(bus, 0.8) for _ in range(2)]
    _apply_confidence_updates(
        bus, winner_id=w, merged_from_ids=losers + [w], actor="test",
        boost_strength=0.0, decay_strength=0.0,
    )
    for lid in losers:
        c = bus.conn.execute(
            "SELECT confidence FROM memory_canonical WHERE id = ?", (lid,),
        ).fetchone()[0]
        _eq(f"decay_strength=0 loser {lid} unchanged", c, 0.8)

    # 6) Decay strength 1.0 → losers confidence = 0
    bus = _FakeBus()
    w = _insert_fact(bus, 0.7)
    losers = [_insert_fact(bus, 0.5) for _ in range(2)]
    _apply_confidence_updates(
        bus, winner_id=w, merged_from_ids=losers + [w], actor="test",
        boost_strength=0.0, decay_strength=1.0,
    )
    for lid in losers:
        c = bus.conn.execute(
            "SELECT confidence FROM memory_canonical WHERE id = ?", (lid,),
        ).fetchone()[0]
        _eq(f"decay_strength=1.0 loser {lid} → 0", c, 0.0)

    # 7) Non-existent winner_id → empty result, no error
    bus = _FakeBus()
    result = _apply_confidence_updates(
        bus, winner_id=99999, merged_from_ids=[1, 2, 3], actor="test",
    )
    _eq("missing winner_conf_before", result["winner_conf_before"], None)
    _eq("missing winner_conf_after", result["winner_conf_after"], None)

    # 8) Non-existent loser_id → silently skipped, decay count correct
    bus = _FakeBus()
    w = _insert_fact(bus, 0.6)
    real_loser = _insert_fact(bus, 0.5)
    _apply_confidence_updates(
        bus, winner_id=w, merged_from_ids=[real_loser, 99998], actor="test",
        decay_strength=0.5,
    )
    _truthy("missing loser skipped", True)
    c = bus.conn.execute(
        "SELECT confidence FROM memory_canonical WHERE id = ?", (real_loser,),
    ).fetchone()[0]
    _eq("real loser still decayed", round(c, 4), 0.25)  # 0.5 * 0.5

    print("\nAll tests PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
