"""Phase E1 tests for LOCK rule helpers.

Covers ``seed_lock_rule``, ``fetch_lock_rules``, ``evaluate_text``,
``parse_lock_rule`` from ``astor_memory._internal.lock_rules``.

v1.14.63 (2026-09-17): locked the latent bug where ``seed_lock_rule``
did not explicitly pin ``tombstoned=0``. Tests here pin that contract
so future regressions are caught.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from astor_memory._internal.lock_rules import (
    LockRule,
    evaluate_text,
    fetch_lock_rules,
    parse_lock_rule,
    seed_lock_rule,
)


def _make_bus(tmp_path: Path) -> sqlite3.Connection:
    """Build a minimal memory_canonical table for testing.

    Mirrors the schema subset ``seed_lock_rule`` actually uses. Tests
    only need INSERT + SELECT; no need to wire the full event/candidate
    machinery.
    """
    db = tmp_path / "astor_bus_test.db"
    con = sqlite3.connect(str(db))
    # fetch_lock_rules / parse_lock_rule use dict access (r["id"]),
    # which requires Row factory — same as cli/main.py:_open_lock_rule_bus.
    con.row_factory = sqlite3.Row
    con.execute(
        """
        CREATE TABLE memory_canonical (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER UNIQUE,
            event_id INTEGER NOT NULL,
            namespace TEXT,
            content TEXT,
            kind TEXT,
            confidence REAL,
            importance REAL,
            tags TEXT,
            metadata TEXT,
            topic TEXT,
            promoted_at DATETIME,
            promoted_by TEXT,
            last_confirmed_at DATETIME,
            last_confirmed_session TEXT,
            access_count INTEGER,
            tombstoned INTEGER NOT NULL DEFAULT 0,
            tombstoned_at DATETIME,
            expires_at DATETIME,
            scene TEXT,
            revision INTEGER,
            parent_revision_id INTEGER,
            superseded_by INTEGER,
            origin_session_id TEXT,
            verdict TEXT,
            scope_type TEXT,
            user_id TEXT,
            session_id TEXT,
            tier TEXT,
            stable_id TEXT,
            embedding_version INTEGER,
            publishable INTEGER,
            parent_fact_ids TEXT,
            provenance_kind TEXT,
            provenance_agent TEXT,
            provenance_depth INTEGER,
            provenance_at DATETIME,
            keywords TEXT,
            context TEXT,
            event_date TEXT,
            event_date_precision TEXT,
            entities_json TEXT,
            created_at TEXT
        )
        """
    )
    con.commit()
    return con


class TestParseLockRule(unittest.TestCase):
    def test_minimal(self):
        fact = {
            "fact_id": 1,
            "content": "Personal financial info → private",
            "kind": "lock_rule",
            "tags": '["LOCK"]',
            "topic": "personal-finance-private",
            "keywords": '["TFSA", "RRSP"]',
            "context": json.dumps({
                "target_tier": "private",
                "scope": "global",
                "priority": 8,
                "action": "route",
                "tags_extra": [],
            }),
        }
        rule = parse_lock_rule(fact)
        self.assertIsNotNone(rule)
        self.assertEqual(rule.rule_name, "personal-finance-private")
        self.assertEqual(rule.target_tier, "private")
        self.assertEqual(rule.priority, 8)
        self.assertEqual(rule.scope, "global")

    def test_metadata_fallback(self):
        """Legacy facts store keywords/context under metadata.__keywords__ etc.

        fetch_lock_rules promotes metadata.__keywords__ / __context__ /
        __topic__ into top-level fields before calling parse_lock_rule, so
        the parser sees them at top level. This test exercises that
        promotion path through fetch_lock_rules, not parse_lock_rule
        directly.
        """
        with tempfile.TemporaryDirectory() as td:
            con = _make_bus(Path(td))
            try:
                # Insert fact with legacy metadata-only fields
                con.execute(
                    """
                    INSERT INTO memory_canonical
                    (event_id, namespace, content, kind, tags, metadata, topic,
                     user_id, tier, tombstoned)
                    VALUES (1, 'admin', 'Methods ship as public', 'lock_rule',
                            '["LOCK"]', ?, '', 'admin', 'public', 0)
                    """,
                    (json.dumps({
                        "__keywords__": ["workflow", "method"],
                        "__context__": {
                            "target_tier": "rule_ship",
                            "scope": "global",
                            "priority": 3,
                            "action": "route",
                            "tags_extra": [],
                        },
                        "__topic__": "rule-ship-methods",
                    }),),
                )
                con.commit()

                rules = fetch_lock_rules(con, user_id=None, scope=None)
                self.assertEqual(len(rules), 1)
                self.assertEqual(rules[0].rule_name, "rule-ship-methods")
                self.assertEqual(rules[0].target_tier, "rule_ship")
            finally:
                con.close()

    def test_rejects_non_lock(self):
        """LockRule parser must ignore facts that aren't tagged LOCK."""
        fact = {
            "fact_id": 3,
            "content": "Some fact",
            "kind": "fact",
            "tags": "[]",
        }
        self.assertIsNone(parse_lock_rule(fact))


class TestEvaluateText(unittest.TestCase):
    def _rule(self, name, target_tier, priority, keywords):
        return LockRule(
            rule_id=1, rule_name=name, description="",
            target_tier=target_tier, scope="global",
            priority=priority, action="route",
            keywords=keywords, tags_extra=[],
        )

    def test_keyword_match(self):
        rule = self._rule(
            "personal-finance-private", "private", 8,
            [r"TFSA", r"RRSP", r"\$[\d,]+"],
        )
        match = evaluate_text("my TFSA balance is 5000", [rule])
        self.assertIsNotNone(match)
        self.assertEqual(match["rule_name"], "personal-finance-private")
        self.assertEqual(match["target_tier"], "private")

    def test_priority_wins(self):
        low = self._rule("low", "public", 2, ["workflow"])
        high = self._rule("high", "private", 9, ["workflow"])
        match = evaluate_text("workflow step 1", [low, high])
        self.assertEqual(match["rule_name"], "high")

    def test_no_match(self):
        rule = self._rule(
            "private-finance", "private", 5,
            ["TFSA", "RRSP"],
        )
        self.assertIsNone(evaluate_text("random chat", [rule]))


class TestFetchLockRules(unittest.TestCase):
    def test_excludes_tombstoned(self):
        """v1.14.63 R-class fix: tombstoned=1 rules must NOT be returned."""
        with tempfile.TemporaryDirectory() as td:
            con = _make_bus(Path(td))
            try:
                seed_lock_rule(
                    con, topic="rule-a", keywords=["alpha"],
                    target_tier="private",
                )
                seed_lock_rule(
                    con, topic="rule-b", keywords=["beta"],
                    target_tier="public",
                )
                n = con.execute(
                    "SELECT COUNT(*) FROM memory_canonical "
                    "WHERE kind='lock_rule' AND tombstoned=0"
                ).fetchone()[0]
                self.assertEqual(n, 2)

                # Tombstone rule-a and verify fetch skips it
                con.execute(
                    "UPDATE memory_canonical SET tombstoned=1 "
                    "WHERE topic='rule-a'"
                )
                con.commit()

                rules = fetch_lock_rules(con, user_id=None, scope=None)
                names = [r.rule_name for r in rules]
                self.assertNotIn("rule-a", names)
                self.assertIn("rule-b", names)
            finally:
                con.close()


class TestSeedLockRule(unittest.TestCase):
    def test_pins_tombstoned_zero(self):
        """v1.14.63 R-class fix: seed_lock_rule must explicitly tombstoned=0.

        Without this fix, decay sweep + dedup can race on freshly seeded
        rules and silently archive them. Pinning the value explicitly makes
        the contract bullet-proof.
        """
        with tempfile.TemporaryDirectory() as td:
            con = _make_bus(Path(td))
            try:
                rule_id = seed_lock_rule(
                    con, topic="tombstone-test", keywords=["test"],
                    target_tier="private",
                )
                row = con.execute(
                    "SELECT tombstoned, last_confirmed_at "
                    "FROM memory_canonical WHERE id=?",
                    (rule_id,),
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(
                    row[0], 0,
                    f"tombstoned should be 0, got {row[0]}",
                )
                # last_confirmed_at must be set NOW so 90d decay gate doesn't trip
                self.assertIsNotNone(
                    row[1],
                    "last_confirmed_at should be set to NOW",
                )
            finally:
                con.close()

    def test_round_trip(self):
        """Seed + fetch + evaluate: end-to-end LOCK rule path."""
        with tempfile.TemporaryDirectory() as td:
            con = _make_bus(Path(td))
            try:
                seed_lock_rule(
                    con, topic="e2e-private",
                    keywords=["SSN", "credit card"],
                    target_tier="private", priority=8,
                )
                rules = fetch_lock_rules(con, user_id=None, scope=None)
                self.assertEqual(len(rules), 1)
                self.assertEqual(rules[0].rule_name, "e2e-private")

                match = evaluate_text(
                    "my credit card is 4111-1111-1111-1111", rules,
                )
                self.assertIsNotNone(match)
                self.assertEqual(match["target_tier"], "private")
            finally:
                con.close()

    def test_invalid_target_tier_raises(self):
        """Validation guard at insert boundary."""
        with tempfile.TemporaryDirectory() as td:
            con = _make_bus(Path(td))
            try:
                with self.assertRaises(ValueError) as cm:
                    seed_lock_rule(
                        con, topic="bad", keywords=["x"],
                        target_tier="bogus_tier",
                    )
                self.assertIn("target_tier", str(cm.exception))
            finally:
                con.close()

    def test_schema_missing_columns_is_forgiving(self):
        """Old DBs without 'topic'/'metadata'/'tombstoned' columns must not
        crash seed_lock_rule. The schema-tolerant code path is what makes
        the upgrade story work."""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "old_schema.db"
            con = sqlite3.connect(str(db))
            # Minimal legacy schema — none of topic/keywords/context/metadata/
            # tombstoned/last_confirmed_at columns
            con.execute(
                """
                CREATE TABLE memory_canonical (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT,
                    kind TEXT,
                    tags TEXT,
                    namespace TEXT,
                    user_id TEXT,
                    tier TEXT
                )
                """
            )
            con.commit()
            try:
                rule_id = seed_lock_rule(
                    con, topic="legacy", keywords=["x"],
                    target_tier="public",
                )
                row = con.execute(
                    "SELECT kind, content, tags "
                    "FROM memory_canonical WHERE id=?",
                    (rule_id,),
                ).fetchone()
                self.assertEqual(row[0], "lock_rule")
                self.assertIn("LOCK", row[2])
            finally:
                con.close()


if __name__ == "__main__":
    unittest.main()
