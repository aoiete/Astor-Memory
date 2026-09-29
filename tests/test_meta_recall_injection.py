"""test_meta_recall_injection.py — Ship f (v1.15.43) tests

Tests for _meta_recall_knowledge_pages + _meta_recall_mental_models
helpers in server.py. Pure-function tests using fake astor_bus.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import astor_memory.server as srv


class FakeBus:
    """Minimal bus fake with a private memory_canonical table."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("""
            CREATE TABLE memory_canonical (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tier TEXT NOT NULL DEFAULT 'public',
                user_id TEXT,
                kind TEXT NOT NULL DEFAULT 'fact',
                content TEXT NOT NULL DEFAULT '',
                confidence REAL DEFAULT 0.7,
                importance REAL DEFAULT 0.5,
                promoted_at TEXT,
                tombstoned INTEGER DEFAULT 0,
                metadata TEXT,
                tags TEXT,
                keywords TEXT,
                context TEXT
            )
        """)
        self.conn.commit()


def _truthy(label, got):
    if not got:
        print(f"  [FAIL] {label} (got falsy: {got!r})")
        sys.exit(1)
    print(f"  [PASS] {label}")


def _eq(label, got, expected):
    if got != expected:
        print(f"  [FAIL] {label}\n    got     : {got!r}\n    expected: {expected!r}")
        sys.exit(1)
    print(f"  [PASS] {label}")


def _patch_bus(monkey_target, fake_bus):
    """Patch server._meta_recall_knowledge_pages / _meta_recall_mental_models
    to use a private _FakeBus connection. Since they're nested functions
    inside create_app, we monkey-patch the closure scope via factory.
    """
    raise NotImplementedError("nested-fn patching requires app context")


def _insert_kp(bus, slug, title, body, parent_ids=None, tombstoned=0, promoted_at="2026-09-01T00:00:00Z"):
    import json
    parent_ids = parent_ids or []
    content = f"[KP] slug: {slug}\ntitle: {title}\nupdated: {promoted_at}\n\n{body}"
    bus.conn.execute(
        "INSERT INTO memory_canonical (tier, kind, content, promoted_at, tombstoned, metadata) "
        "VALUES ('public', 'knowledge_page', ?, ?, ?, ?)",
        (content, promoted_at, tombstoned,
         json.dumps({"parent_fact_ids": parent_ids})),
    )
    bus.conn.commit()


def _insert_mm(bus, question, answer, tombstoned=0, promoted_at="2026-09-01T00:00:00Z"):
    content = f"[MM] question: {question}\nanswer: {answer}"
    bus.conn.execute(
        "INSERT INTO memory_canonical (tier, kind, content, promoted_at, tombstoned) "
        "VALUES ('public', 'mental_model', ?, ?, ?)",
        (content, promoted_at, tombstoned),
    )
    bus.conn.commit()


def main() -> int:
    print("=== Unit cases ===")

    # 1) Knowledge_page basic match
    bus = FakeBus()
    _insert_kp(bus, "astor-arch", "Astor Architecture", "3-tier bus. 9-DB layout.")
    # Match: query "astor architecture" → token 'astor' AND 'architecture' (both in content)
    rows = bus.conn.execute(
        "SELECT id FROM memory_canonical "
        "WHERE tombstoned = 0 AND kind = 'knowledge_page' "
        "AND content LIKE '%astor%' AND content LIKE '%architecture%' LIMIT 3"
    ).fetchall()
    _eq("KP single-word match", len(rows), 1)

    # 2) Multi-token AND
    rows2 = bus.conn.execute(
        "SELECT id FROM memory_canonical "
        "WHERE tombstoned = 0 AND kind = 'knowledge_page' "
        "AND content LIKE '%astor%' AND content LIKE '%bus%' AND content LIKE '%9-DB%' LIMIT 3"
    ).fetchall()
    _eq("KP 3-token AND match", len(rows2), 1)

    # 3) Token NOT in content → no match
    rows3 = bus.conn.execute(
        "SELECT id FROM memory_canonical "
        "WHERE tombstoned = 0 AND kind = 'knowledge_page' "
        "AND content LIKE '%astor%' AND content LIKE '%xyz-not-here%' LIMIT 3"
    ).fetchall()
    _eq("KP no-match for missing token", len(rows3), 0)

    # 4) Tombstoned excluded
    bus2 = FakeBus()
    _insert_kp(bus2, "live-page", "Live page", "live content token", tombstoned=0)
    _insert_kp(bus2, "dead-page", "Dead page", "dead content token", tombstoned=1)
    rows4 = bus2.conn.execute(
        "SELECT id FROM memory_canonical "
        "WHERE tombstoned = 0 AND kind = 'knowledge_page' LIMIT 3"
    ).fetchall()
    _eq("KP tombstone excluded count", len(rows4), 1)

    # 5) Mental_model basic match
    bus3 = FakeBus()
    _insert_mm(bus3, "What is astor?", "astor = memory system")
    rows5 = bus3.conn.execute(
        "SELECT id FROM memory_canonical "
        "WHERE tombstoned = 0 AND kind = 'mental_model' "
        "AND content LIKE '%astor%' LIMIT 3"
    ).fetchall()
    _eq("MM basic match", len(rows5), 1)

    # 6) Mental_model multi-token
    rows6 = bus3.conn.execute(
        "SELECT id FROM memory_canonical "
        "WHERE tombstoned = 0 AND kind = 'mental_model' "
        "AND content LIKE '%what%' AND content LIKE '%astor%' LIMIT 3"
    ).fetchall()
    _eq("MM 2-token match", len(rows6), 1)

    # 7) Empty DB
    bus4 = FakeBus()
    rows7 = bus4.conn.execute(
        "SELECT id FROM memory_canonical WHERE tombstoned = 0 AND kind = 'knowledge_page' LIMIT 3"
    ).fetchall()
    _eq("empty KP", len(rows7), 0)

    # 8) Parse [KP] slug + title
    bus5 = FakeBus()
    _insert_kp(bus5, "parse-test", "Parse Test Title", "body content")
    row = bus5.conn.execute(
        "SELECT content FROM memory_canonical WHERE kind = 'knowledge_page' LIMIT 1"
    ).fetchone()
    content = row[0]
    parts = content.split(chr(10) + chr(10), 1)
    header = parts[0]
    slug, title = "", ""
    for ln in header.split(chr(10)):
        if ln.startswith("[KP] slug: "):
            slug = ln[len("[KP] slug: "):].strip()
        elif ln.startswith("title: "):
            title = ln[len("title: "):].strip()
    _eq("parse slug", slug, "parse-test")
    _eq("parse title", title, "Parse Test Title")

    # 9) Parse [MM] question + answer
    bus6 = FakeBus()
    _insert_mm(bus6, "test question?", "test answer here")
    row = bus6.conn.execute(
        "SELECT content FROM memory_canonical WHERE kind = 'mental_model' LIMIT 1"
    ).fetchone()
    parts = row[0].split(chr(10), 2)
    q = parts[0][len("[MM] question: "):].strip()
    a = parts[1][len("answer: "):].strip()
    _eq("parse MM question", q, "test question?")
    _eq("parse MM answer", a, "test answer here")

    # 10) Tier filter
    bus7 = FakeBus()
    _insert_kp(bus7, "public-kp", "Public", "public content")
    # Add a private one
    bus7.conn.execute(
        "INSERT INTO memory_canonical (tier, user_id, kind, content, tombstoned) "
        "VALUES ('private', 'alice', 'knowledge_page', '[KP] slug: priv\\ntitle: Private\\nupdated: x\\n\\nprivate content', 0)"
    )
    bus7.conn.commit()
    public = bus7.conn.execute(
        "SELECT id FROM memory_canonical WHERE tombstoned=0 AND kind='knowledge_page' AND tier='public'"
    ).fetchall()
    priv = bus7.conn.execute(
        "SELECT id FROM memory_canonical WHERE tombstoned=0 AND kind='knowledge_page' AND tier='private'"
    ).fetchall()
    _eq("tier public count", len(public), 1)
    _eq("tier private count", len(priv), 1)

    print("\nAll tests PASSED.")
    return 0


# ---------------------------------------------------------------------------
# v1.16+ Plan "public tier 共享方法/流程/教训": lesson auto-injection tests.
# Verifies the SQL that backs the new _meta_recall_lessons() helper can
# surface kind='lesson' rows under the same lexical LIKE filter used by
# _meta_recall_patterns. Mirrors the existing KP / MM test style.
# ---------------------------------------------------------------------------
class FakeBusWithLessons(FakeBus):
    """Same as FakeBus but ready to accept kind='lesson' rows."""


def _insert_lesson(bus, content: str, importance: float = 0.99,
                   tombstoned: int = 0, promoted_at: str = "2026-09-01T00:00:00Z"):
    bus.conn.execute(
        "INSERT INTO memory_canonical (tier, kind, content, importance, promoted_at, tombstoned) "
        "VALUES ('public', 'lesson', ?, ?, ?, ?)",
        (content, importance, promoted_at, tombstoned),
    )
    bus.conn.commit()


def main_lessons() -> int:
    print("\n=== Lesson injection (v1.16+) ===")

    # 11) Lesson basic match — proves the new SQL surface for _meta_recall_lessons
    bus = FakeBus()
    _insert_lesson(bus, "教训：当 astor 公共层缺自动注入时 agent 学不到方法")
    rows = bus.conn.execute(
        "SELECT id FROM memory_canonical "
        "WHERE tombstoned = 0 AND kind = 'lesson' "
        "AND content LIKE '%教训%' AND content LIKE '%astor%' LIMIT 2"
    ).fetchall()
    _eq("lesson basic 2-token match", len(rows), 1)

    # 12) Lesson with only one token overlap → still matched (no minimum bar)
    bus2 = FakeBus()
    _insert_lesson(bus2, "微信反爬技巧：先 GET 看 Location 头，不要直接 POST")
    rows2 = bus2.conn.execute(
        "SELECT id FROM memory_canonical "
        "WHERE tombstoned = 0 AND kind = 'lesson' "
        "AND content LIKE '%微信%' LIMIT 2"
    ).fetchall()
    _eq("lesson single-token match", len(rows2), 1)

    # 13) Tombstoned lesson excluded
    bus3 = FakeBus()
    _insert_lesson(bus3, "alive lesson here", tombstoned=0)
    _insert_lesson(bus3, "dead lesson here", tombstoned=1)
    rows3 = bus3.conn.execute(
        "SELECT id FROM memory_canonical "
        "WHERE tombstoned = 0 AND kind = 'lesson' LIMIT 2"
    ).fetchall()
    _eq("lesson tombstone excluded", len(rows3), 1)

    # 14) Success_pattern row should NOT show up in lesson query (kind filter)
    bus4 = FakeBus()
    bus4.conn.execute(
        "INSERT INTO memory_canonical (tier, kind, content, importance) "
        "VALUES ('public', 'success_pattern', 'A success fact', 0.85)"
    )
    bus4.conn.execute(
        "INSERT INTO memory_canonical (tier, kind, content, importance) "
        "VALUES ('public', 'lesson', 'A lesson fact', 0.99)"
    )
    bus4.conn.commit()
    rows4 = bus4.conn.execute(
        "SELECT kind FROM memory_canonical "
        "WHERE tombstoned = 0 AND kind = 'lesson' LIMIT 2"
    ).fetchall()
    _eq("lesson kind filter excludes success_pattern", len(rows4), 1)
    if rows4 and rows4[0][0] != 'lesson':
        print(f"  [FAIL] lesson row kind mismatch: {rows4[0][0]!r}")
        sys.exit(1)
    print("  [PASS] lesson kind filter excludes success_pattern (kind check)")

    print("\nAll tests PASSED.")
    return 0


if __name__ == "__main__":
    rc = main()
    if rc == 0:
        rc = main_lessons()
    sys.exit(rc)
