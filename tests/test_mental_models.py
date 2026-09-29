"""test_mental_models.py — Ship P1.1 (2026-09-28)

Hindsight-style mental_model layer tests. Verifies:
- Content round-trip: format + parse
- Bad content gracefully returns None on parse
- list_mental_models filters by tier + user_id
- get_mental_model exact-match (no vector search)
- upsert_mental_model tombstones prior, inserts new
- is_enabled reads env

Tests use an in-memory sqlite db with a minimal memory_canonical
schema so we don't need a live astor-memory-runtime.
"""
from __future__ import annotations

import os
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class FakeBus:
    """Minimal bus fake for unit-testing mental_models.

    v1.15.36: upsert_mental_model calls bus.append_event +
    bus.insert_candidate + bus.promote_candidate. Provide a fake that
    mimics the bus's chained write pipeline against an in-memory sqlite.
    """
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
        # Mimic live promote_candidate: pull candidate row, build canonical
        # INSERT with all required NOT NULL columns.
        row = self.conn.execute(
            "SELECT event_id, namespace, content, kind, confidence, "
            "importance, tags, metadata, scene FROM memory_candidates "
            "WHERE id = ?", (candidate_id,),
        ).fetchone()
        assert row is not None
        ev_id, ns, cont, knd, conf, imp, tags_json, meta_json, scene = row
        try:
            meta_dict = json.loads(meta_json) if meta_json else {}
        except Exception:
            meta_dict = {}
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
             evidence_quote, source_ref, source_hash, "mental_model"),
        )
        return int(cur.lastrowid)


def _make_minimal_bus_db() -> sqlite3.Connection:
    """Build an in-memory sqlite mirroring live memory_canonical schema.

    v1.15.36: introspect the live public tier schema and replicate the
    full column set (NOT NULL + defaults). Also creates memory_candidates
    + events tables (used by FakeBus.promote_candidate).
    """
    conn = sqlite3.connect(":memory:")
    live_db = r'D:\AI\Astor-Memory-Runtime\public\memory\astor_bus_public.db'
    import os
    src = None
    if os.path.exists(live_db):
        src = sqlite3.connect(live_db)
        cols = src.execute("PRAGMA table_info(memory_canonical)").fetchall()
    else:
        cols = None
    if cols:
        src.close()
        # Build CREATE TABLE statement from discovered columns
        col_defs = []
        for c in cols:
            # c = (cid, name, type, notnull, default, pk)
            type_str = c[2] or "TEXT"
            parts = [c[1], type_str]
            if c[3]:
                parts.append("NOT NULL")
            if c[4] is not None:
                d = c[4]
                if isinstance(d, str):
                    # String default — quote it
                    parts.append(f"DEFAULT '{d.replace(chr(39), chr(39) * 2)}'")
                else:
                    parts.append(f"DEFAULT {d}")
            if c[5]:
                parts.append("PRIMARY KEY")
            col_defs.append(" ".join(parts))
        ddl = "CREATE TABLE memory_canonical (" + ", ".join(col_defs) + ")"
    else:
        # No live DB to mirror — minimal fallback (just the cols mental_models.py uses)
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
            "tags TEXT,source_hash TEXT,"
            "created_at TEXT,promoted_at TEXT,promoted_by TEXT,"
            "tombstoned INTEGER DEFAULT 0,tombstoned_at TEXT)"
        )
    conn.execute(ddl)
    conn.execute("""
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            namespace TEXT NOT NULL DEFAULT '',
            agent_id TEXT,
            source TEXT,
            action TEXT,
            content TEXT,
            metadata TEXT,
            request_id TEXT
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
            tags TEXT,
            metadata TEXT,
            scene TEXT NOT NULL DEFAULT 'casual'
        )
    """)
    conn.commit()
    return conn


def _eq(label, got, expected):
    if got != expected:
        print(f"  [FAIL] {label}\n    got     : {got}\n    expected: {expected}")
        sys.exit(1)
    print(f"  [PASS] {label}")


def _truthy(label, got):
    if not got:
        print(f"  [FAIL] {label} (got falsy: {got!r})")
        sys.exit(1)
    print(f"  [PASS] {label}")


def main() -> int:
    print("=== Unit cases ===")

    from astor_memory.nest.mental_models import (
        MentalModel, _parse_mm_content, _format_mm_content,
        list_mental_models, get_mental_model, upsert_mental_model,
        is_enabled,
    )

    # 1) Round-trip
    q, ans = "What timezone is admin in?", "MDT (Mountain Daylight Time, UTC-6) — Calgary."
    content = _format_mm_content(q, ans)
    parsed = _parse_mm_content(content)
    _truthy("round-trip parse", parsed is not None)
    _eq("parsed question", parsed[0], q)
    _eq("parsed answer", parsed[1], ans)

    # 2) Bad inputs
    for bad in ["", "regular fact content", "[MM] question without answer",
                "[MM] question: q\n", "[MM] answer: a",
                "[MM] question: \nanswer: "]:
        _eq(f"bad input {bad!r} → None", _parse_mm_content(bad), None)

    # 3) list_mental_models filters
    conn = _make_minimal_bus_db()
    bus = FakeBus(conn)
    # Schema column check (mirror real memory_canonical; without
    # promoted_at the upsert would fail with OperationalError).
    cols = [r[1] for r in conn.execute("PRAGMA table_info(memory_canonical)").fetchall()]
    _truthy("schema has promoted_at column", "promoted_at" in cols)
    _truthy("schema has created_at column", "created_at" in cols)
    _truthy("schema has tombstoned_at column", "tombstoned_at" in cols)
    _truthy("schema has tombstoned column", "tombstoned" in cols)
    upsert_mental_model(bus, "Q1?", "A1", tier="public", user_id=None, confidence=0.8)
    upsert_mental_model(bus, "Q2?", "A2", tier="public", user_id="alice", confidence=0.6)
    upsert_mental_model(bus, "Q3?", "A3", tier="private", user_id="admin", confidence=0.9)
    items = list_mental_models(bus, tier="public", user_id=None)
    _eq("list public/admin → 1", len(items), 1)
    _eq("list public/admin question", items[0].question, "Q1?")
    items = list_mental_models(bus, tier="public", user_id="alice")
    _eq("list public/alice → 1", len(items), 1)
    _eq("list public/alice question", items[0].question, "Q2?")
    items = list_mental_models(bus, tier="private", user_id="admin")
    _eq("list private/admin → 1", len(items), 1)

    # 5) get_mental_model exact match
    mm = get_mental_model(bus, "Q1?", tier="public", user_id=None)
    _truthy("get Q1 returns", mm is not None)
    _eq("get Q1 question", mm.question, "Q1?")
    _eq("get Q1 answer", mm.answer, "A1")
    _eq("get Q1 confidence", mm.confidence, 0.8)

    # 6) get_mental_model miss
    _eq("get unknown → None",
        get_mental_model(bus, "Does not exist?", tier="public", user_id=None),
        None)

    # 7) upsert tombstones prior
    fid1 = upsert_mental_model(bus, "Q?", "answer v1", tier="public", user_id=None, confidence=0.5)
    fid2 = upsert_mental_model(bus, "Q?", "answer v2", tier="public", user_id=None, confidence=0.9)
    _truthy("new fid is fresh", fid2 != fid1)
    # prior should be tombstoned
    row = conn.execute("SELECT tombstoned FROM memory_canonical WHERE id=?", (fid1,)).fetchone()
    _eq("prior tombstoned=1", row[0], 1)
    # get_mental_model returns new
    mm = get_mental_model(bus, "Q?", tier="public", user_id=None)
    _eq("after upsert → new answer", mm.answer, "answer v2")
    _eq("after upsert → new confidence", mm.confidence, 0.9)
    _eq("after upsert → new fid", mm.fact_id, fid2)

    # 8) upsert with source_facts (best-effort; missing table silently ignored)
    fid3 = upsert_mental_model(bus, "Q?", "answer v3", tier="public", user_id=None,
                                confidence=0.95, source_facts=[999, 1000])  # fids don't exist
    _truthy("upsert with non-existent source_facts succeeds", fid3 > fid2)

    # 9) is_enabled env gate
    os.environ.pop("ASTOR_MENTAL_MODELS", None)
    _eq("default is_enabled True (v1.15.36 ships enabled)", is_enabled(), True)
    os.environ["ASTOR_MENTAL_MODELS"] = "1"
    _eq("ASTOR_MENTAL_MODELS=1 → True", is_enabled(), True)
    os.environ["ASTOR_MENTAL_MODELS"] = "0"
    _truthy("ASTOR_MENTAL_MODELS=0 → False", not is_enabled())
    # v1.15.36: default is now ON (module ships enabled). Verify default.
    import os as _os
    _os.environ.pop("ASTOR_MENTAL_MODELS", None)
    _truthy("default (no env) → True (module ships enabled)", is_enabled())
    os.environ.pop("ASTOR_MENTAL_MODELS", None)

    # 10) Format handles multi-line answer
    long_ans = "Line 1\nLine 2\nLine 3 with embedded newline"
    content = _format_mm_content("Multi-line Q?", long_ans)
    parsed = _parse_mm_content(content)
    _truthy("multi-line answer parses", parsed is not None)
    _eq("multi-line question", parsed[0], "Multi-line Q?")
    _eq("multi-line answer", parsed[1], long_ans)

    # 11) MentalModel dataclass fields
    mm = MentalModel(fact_id=42, question="Q", answer="A", confidence=0.5,
                     created_at="2026-09-28T00:00:00+00:00",
                     updated_at="2026-09-28T00:00:00+00:00",
                     source_count=0)
    _eq("MentalModel.fact_id", mm.fact_id, 42)
    _eq("MentalModel.confidence", mm.confidence, 0.5)

    print("\nAll tests PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())