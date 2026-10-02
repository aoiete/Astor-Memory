"""test_knowledge_pages.py — Ship P3.1 (2026-09-28)

Hindsight-style knowledge_page layer tests:
- Content round-trip
- Bad content → None
- list/get filters by tier + user_id
- Upsert tombstones prior same-slug row
- link_facts adds/removes from parent_fact_ids
- get_linked_facts returns content preview
- is_enabled env gate
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory.nest.knowledge_pages import (
    _format_kp_content, _parse_kp_content,
    upsert_knowledge_page, list_knowledge_pages, get_knowledge_page,
    get_linked_facts, link_facts, is_enabled, KnowledgePage,
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


class FakeBus:
    """Minimal bus fake — mirrors mental_models FakeBus."""
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def append_event(self, *, namespace, agent_id, source, action, content,
                     metadata=None, request_id=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO events (namespace, agent_id, source, action, "
            "content, metadata, request_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (namespace, agent_id, source, action, content,
             json.dumps(metadata or {}), request_id),
        )
        return int(cur.lastrowid)

    def insert_candidate(self, *, event_id, namespace, content,
                         kind="fact", confidence=0.7, importance=0.5,
                         tags=None, metadata=None, scene="casual",
                         keywords=None, context="", event_date=None,
                         event_date_precision="none", abstract="",
                         overview="", topic="", session_id="",
                         entities=None) -> int:
        meta = dict(metadata or {})
        if keywords is not None:
            meta["__keywords__"] = list(keywords)
        if context:
            meta["__context__"] = str(context)[:500]
        cur = self.conn.execute(
            "INSERT INTO memory_candidates "
            "(event_id, namespace, content, kind, confidence, importance, "
            "tags, metadata, scene) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, namespace, content, kind, confidence, importance,
             json.dumps(tags or []), json.dumps(meta), scene),
        )
        return int(cur.lastrowid)

    def promote_candidate(self, *, candidate_id, promoted_by,
                          user_id=None, tier="public", scope_type="long_term",
                          verdict="settled", origin_session_id=None,
                          stable_id=None, provenance_kind=None,
                          provenance_agent=None, evidence_quote="",
                          source_ref="", source_hash="") -> int:
        row = self.conn.execute(
            "SELECT event_id, namespace, content, kind, confidence, "
            "importance, tags, metadata, scene FROM memory_candidates "
            "WHERE id = ?", (candidate_id,),
        ).fetchone()
        assert row is not None
        ev_id, ns, cont, knd, conf, imp, tags_json, meta_json, scene = row
        meta_dict = json.loads(meta_json) if meta_json else {}
        kw_json = json.dumps(meta_dict.get("__keywords__") or [])
        ctx_text = str(meta_dict.get("__context__") or "")[:500]
        cur = self.conn.execute(
            "INSERT INTO memory_canonical ("
            "candidate_id, event_id, namespace, content, kind, confidence, "
            "importance, tags, metadata, keywords, context, "
            "promoted_by, user_id, tier, scope_type, verdict, "
            "embedding_version, "
            "provenance_kind, provenance_agent, "
            "evidence_quote, source_ref, source_hash, memory_class"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (candidate_id, ev_id, ns, cont, knd, conf, imp, tags_json,
             meta_json, kw_json, ctx_text,
             promoted_by, user_id, tier, scope_type, verdict,
             1, provenance_kind or "extracted", provenance_agent,
             evidence_quote, source_ref, source_hash, "knowledge_page"),
        )
        return int(cur.lastrowid)


def _make_bus_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            namespace TEXT NOT NULL DEFAULT '',
            agent_id TEXT, source TEXT, action TEXT,
            content TEXT, metadata TEXT, request_id TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE memory_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL DEFAULT 0,
            namespace TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'fact',
            confidence REAL NOT NULL DEFAULT 0.7,
            importance REAL NOT NULL DEFAULT 0.5,
            tags TEXT, metadata TEXT,
            scene TEXT NOT NULL DEFAULT 'casual'
        )
    """)
    _live_dir = os.environ.get('ASTOR_DIR') or os.path.expanduser('~/.astor')
    live_db = os.path.join(_live_dir, 'public', 'memory', 'astor_bus_public.db')
    if os.path.exists(live_db):
        src = sqlite3.connect(live_db)
        cols = src.execute("PRAGMA table_info(memory_canonical)").fetchall()
        src.close()
        col_defs = []
        for c in cols:
            type_str = c[2] or "TEXT"
            parts = [c[1], type_str]
            if c[3]:
                parts.append("NOT NULL")
            if c[4] is not None:
                d = c[4]
                if isinstance(d, str):
                    parts.append(f"DEFAULT '{d.replace(chr(39), chr(39) * 2)}'")
                else:
                    parts.append(f"DEFAULT {d}")
            if c[5]:
                parts.append("PRIMARY KEY")
            col_defs.append(" ".join(parts))
        ddl = "CREATE TABLE memory_canonical (" + ", ".join(col_defs) + ")"
    else:
        ddl = (
            "CREATE TABLE memory_canonical ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "candidate_id INTEGER NOT NULL DEFAULT 0,"
            "event_id INTEGER NOT NULL DEFAULT 0,"
            "namespace TEXT NOT NULL DEFAULT '',"
            "tier TEXT NOT NULL DEFAULT 'public',"
            "user_id TEXT,"
            "kind TEXT NOT NULL DEFAULT 'fact',"
            "memory_class TEXT NOT NULL DEFAULT 'world_fact',"
            "content TEXT NOT NULL DEFAULT '',"
            "importance REAL,confidence REAL,"
            "tags TEXT,metadata TEXT,source_hash TEXT,"
            "created_at TEXT,promoted_at TEXT,promoted_by TEXT,"
            "tombstoned INTEGER DEFAULT 0,tombstoned_at TEXT,"
            "embedding_version INTEGER NOT NULL DEFAULT 1,"
            "provenance_kind TEXT,provenance_agent TEXT,"
            "evidence_quote TEXT,source_ref TEXT)"
        )
    conn.execute(ddl)
    conn.commit()
    return conn


def main() -> int:
    print("=== Unit cases ===")

    # 1) Round-trip content
    slug, title, body = "test-slug", "Test Page", "Body line 1\nLine 2"
    now = "2026-09-28T00:00:00Z"
    content = _format_kp_content(slug, title, body, now)
    parsed = _parse_kp_content(content)
    _truthy("round-trip parse", parsed is not None)
    _eq("parsed slug", parsed[0], slug)
    _eq("parsed title", parsed[1], title)
    _eq("parsed body", parsed[2], body)
    _eq("parsed updated", parsed[3], now)

    # 2) Bad inputs
    _eq("empty → None", _parse_kp_content(""), None)
    _eq("regular content → None", _parse_kp_content("regular fact content"), None)
    _eq("[KP] without slug → None",
        _parse_kp_content("[KP] title: x\n\nbody"), None)

    # 3) list/get with empty DB
    conn = _make_bus_db()
    bus = FakeBus(conn)
    pages = list_knowledge_pages(bus)
    _eq("empty DB list", pages, [])
    page = get_knowledge_page(bus, "missing")
    _eq("missing page → None", page, None)
    conn.close()

    # 4) upsert + list
    conn = _make_bus_db()
    bus = FakeBus(conn)
    fid = upsert_knowledge_page(
        bus, slug="astor-architecture",
        title="Astor Architecture Overview",
        body="3-tier bus. 9-DB layout. Per-user isolation.",
        tier="public", user_id="admin",
        confidence=0.85,
    )
    _truthy("upsert returns fid", isinstance(fid, int) and fid > 0)
    pages = list_knowledge_pages(bus, tier="public", user_id="admin")
    _eq("list count", len(pages), 1)
    _eq("list slug", pages[0].slug, "astor-architecture")
    _eq("list title", pages[0].title, "Astor Architecture Overview")
    _truthy("list body has '3-tier'", "3-tier" in pages[0].body)
    conn.close()

    # 5) get specific page
    conn = _make_bus_db()
    bus = FakeBus(conn)
    upsert_knowledge_page(bus, slug="p1", title="P1", body="B1",
                          tier="public")
    upsert_knowledge_page(bus, slug="p2", title="P2", body="B2",
                          tier="public")
    page = get_knowledge_page(bus, "p1", tier="public")
    _eq("get slug", page.slug, "p1")
    _eq("get title", page.title, "P1")
    conn.close()

    # 6) tier filter
    conn = _make_bus_db()
    bus = FakeBus(conn)
    upsert_knowledge_page(bus, slug="pub", title="Pub", body="B",
                          tier="public")
    upsert_knowledge_page(bus, slug="priv", title="Priv", body="B",
                          tier="private", user_id="admin")
    pub_pages = list_knowledge_pages(bus, tier="public")
    priv_pages = list_knowledge_pages(bus, tier="private", user_id="admin")
    _eq("public count", len(pub_pages), 1)
    _eq("private count", len(priv_pages), 1)
    _eq("public slug", pub_pages[0].slug, "pub")
    _eq("private slug", priv_pages[0].slug, "priv")
    conn.close()

    # 7) Upsert tombstones prior same-slug
    conn = _make_bus_db()
    bus = FakeBus(conn)
    fid1 = upsert_knowledge_page(bus, slug="topic", title="V1", body="B1",
                                  tier="public")
    fid2 = upsert_knowledge_page(bus, slug="topic", title="V2", body="B2",
                                  tier="public")
    _truthy("new fid different", fid1 != fid2)
    pages = list_knowledge_pages(bus, tier="public")
    _eq("after re-upsert count", len(pages), 1)
    _eq("after re-upsert title", pages[0].title, "V2")
    conn.close()

    # 8) LIKE wildcard escape in get (slug with %)
    conn = _make_bus_db()
    bus = FakeBus(conn)
    upsert_knowledge_page(bus, slug="50%-off", title="Discount", body="B",
                          tier="public")
    page = get_knowledge_page(bus, "50%", tier="public")
    _truthy("wildcard escape — page found", page is not None)
    _eq("wildcard escape — slug match", page.slug, "50%-off")
    conn.close()

    # 9) link_facts add + remove
    conn = _make_bus_db()
    bus = FakeBus(conn)
    # Insert 4 facts to link
    linked_fids = []
    for i in range(4):
        cur = bus.conn.execute(
            "INSERT INTO memory_candidates (event_id, namespace, content) "
            "VALUES (?, ?, ?)",
            (1, "test", f"linked fact {i}"),
        )
        linked_fids.append(int(cur.lastrowid))
        cur = bus.conn.execute(
            "INSERT INTO memory_canonical (candidate_id, event_id, namespace, "
            "content, kind, confidence, importance, tags, metadata, "
            "promoted_by, tier, scope_type, verdict, embedding_version, "
            "provenance_kind, provenance_agent, evidence_quote, "
            "source_ref, source_hash, memory_class) "
            "VALUES (?, ?, ?, ?, 'fact', 0.7, 0.5, '[]', '{}', 'test', "
            "'public', 'long_term', 'settled', 1, 'extracted', 'test', "
            "'', '', '', 'world_fact')",
            (linked_fids[-1], 1, "test", f"linked fact {i}"),
        )
    # Upsert page with first 2 facts linked
    upsert_knowledge_page(
        bus, slug="topic", title="T", body="B",
        tier="public",
        parent_fact_ids=[linked_fids[0], linked_fids[1]],
    )
    # Add fact 2, remove fact 0
    link_facts(bus, slug="topic", tier="public",
               add_facts=[linked_fids[2]], remove_facts=[linked_fids[0]])
    page = get_knowledge_page(bus, "topic", tier="public")
    _eq("after link_facts parents count", len(page.parent_fact_ids), 2)
    _truthy("link_facts added 2",
            linked_fids[2] in page.parent_fact_ids)
    _truthy("link_facts removed 0",
            linked_fids[0] not in page.parent_fact_ids)
    conn.close()

    # 10) link_facts unknown slug raises
    conn = _make_bus_db()
    bus = FakeBus(conn)
    try:
        link_facts(bus, slug="missing", tier="public")
        print("  [FAIL] link_facts unknown slug should raise")
        sys.exit(1)
    except ValueError:
        print("  [PASS] link_facts unknown slug raises ValueError")
    conn.close()

    # 11) get_linked_facts returns content previews
    conn = _make_bus_db()
    bus = FakeBus(conn)
    cur1 = bus.conn.execute(
        "INSERT INTO memory_candidates (event_id, namespace, content) "
        "VALUES (?, ?, ?)", (1, "t", "x"))
    fid_a = int(cur1.lastrowid)
    bus.conn.execute(
        "INSERT INTO memory_canonical (candidate_id, event_id, namespace, "
        "content, kind, confidence, importance, tags, metadata, "
        "promoted_by, tier, scope_type, verdict, embedding_version, "
        "provenance_kind, provenance_agent, evidence_quote, "
        "source_ref, source_hash, memory_class) "
        "VALUES (?, ?, ?, ?, 'fact', 0.7, 0.5, '[]', '{}', 'test', "
        "'public', 'long_term', 'settled', 1, 'extracted', 'test', "
        "'', '', '', 'world_fact')",
        (fid_a, 1, "t", "Important fact about Hermes agent"),
    )
    upsert_knowledge_page(
        bus, slug="agent", title="Agent", body="B",
        tier="public",
        parent_fact_ids=[fid_a],
    )
    page = get_knowledge_page(bus, "agent", tier="public")
    linked = get_linked_facts(bus, page)
    _eq("linked count", len(linked), 1)
    _truthy("linked has content", "Hermes" in linked[0]["content"])
    conn.close()

    # 12) get_linked_facts empty parent_fact_ids
    conn = _make_bus_db()
    bus = FakeBus(conn)
    upsert_knowledge_page(bus, slug="x", title="X", body="B", tier="public")
    page = get_knowledge_page(bus, "x", tier="public")
    linked = get_linked_facts(bus, page)
    _eq("empty parents → no linked", linked, [])
    conn.close()

    # 13) is_enabled default ON
    os.environ.pop("ASTOR_KNOWLEDGE_PAGES", None)
    _truthy("default ON (v1.15.39 ships enabled)", is_enabled())
    os.environ["ASTOR_KNOWLEDGE_PAGES"] = "0"
    _truthy("ASTOR_KNOWLEDGE_PAGES=0 → False", not is_enabled())
    os.environ.pop("ASTOR_KNOWLEDGE_PAGES", None)

    # 14) upsert with empty slug raises
    conn = _make_bus_db()
    bus = FakeBus(conn)
    try:
        upsert_knowledge_page(bus, slug="", title="x", body="x", tier="public")
        print("  [FAIL] empty slug should raise")
        sys.exit(1)
    except ValueError:
        print("  [PASS] empty slug raises ValueError")
    conn.close()

    print("\nAll tests PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
