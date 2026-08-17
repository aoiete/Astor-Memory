"""
SQLite schema for astor-memory bus + canonical fact store.

Schema follows Plan § Architecture (3-tier isolation):
- public: shared knowledge + skills + public rules
- source: admin-private (admin + admin-only)
- private_<user>: per-user persona (only that user)

Tables:
- events: append-only event log
- memory_candidates: extracted facts (review before promote)
- memory_canonical: promoted facts (the actual memory)
- audit_log: per-action audit trail (7-year retention per Plan § Audit log)
"""

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 5

SCHEMA_SQL = """
-- Pragmas set at connection time (bus/store.py:connect)
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    namespace TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    source TEXT NOT NULL,
    action TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    tombstone INTEGER NOT NULL DEFAULT 0,
    request_id TEXT,
    prev_event_id INTEGER,
    FOREIGN KEY (prev_event_id) REFERENCES events(id)
);

CREATE INDEX IF NOT EXISTS idx_events_namespace ON events(namespace, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_action ON events(action, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_id, ts DESC);

CREATE TABLE IF NOT EXISTS memory_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    namespace TEXT NOT NULL,
    content TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'fact',
    confidence REAL NOT NULL DEFAULT 0.7,
    importance REAL NOT NULL DEFAULT 0.5,
    tags TEXT NOT NULL DEFAULT '[]',
    metadata TEXT NOT NULL DEFAULT '{}',
    review_state TEXT NOT NULL DEFAULT 'pending',  -- pending | promoted | rejected
    promoted_at DATETIME,
    promoted_to INTEGER,                            -- memory_canonical.id
    rejected_reason TEXT,
    ttl_days INTEGER,
    expires_at DATETIME,
    scene TEXT NOT NULL DEFAULT 'casual',
    created_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (event_id) REFERENCES events(id)
);

CREATE INDEX IF NOT EXISTS idx_candidates_review ON memory_candidates(review_state, created_at) WHERE review_state = 'pending';

CREATE TABLE IF NOT EXISTS memory_canonical (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL UNIQUE,
    event_id INTEGER NOT NULL,
    namespace TEXT NOT NULL,
    content TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'fact',
    confidence REAL NOT NULL DEFAULT 0.7,
    importance REAL NOT NULL DEFAULT 0.5,
    tags TEXT NOT NULL DEFAULT '[]',
    metadata TEXT NOT NULL DEFAULT '{}',
    -- v1.2.0 schema v5 (2026-08-16): A-MEM-style structured fields. Extracted by
    -- forge (LLM mode for v1.2.0; regex mode derives heuristically). keywords
    -- powers hybrid_merge rerank Jaccard boost; context gives human-readable
    -- "what is this fact about" used by viewer + admin audit.
    keywords TEXT NOT NULL DEFAULT '[]',
    context TEXT NOT NULL DEFAULT '',
    promoted_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    promoted_by TEXT,
    last_confirmed_at DATETIME,
    last_confirmed_session TEXT,
    access_count INTEGER NOT NULL DEFAULT 0,
    tombstoned INTEGER NOT NULL DEFAULT 0,
    tombstoned_at DATETIME,
    expires_at DATETIME,
    scene TEXT NOT NULL DEFAULT 'casual',
    -- Insight 5: revision tracking (Plan § Insight 5)
    revision INTEGER NOT NULL DEFAULT 1,
    parent_revision_id INTEGER,
    superseded_by INTEGER,
    -- Insight 14: session-link (LongMemEval)
    origin_session_id TEXT,
    -- Insight 12 + 16: verdict state machine + decay
    verdict TEXT NOT NULL DEFAULT 'settled'
        CHECK(verdict IN ('settled', 'contested', 'thin', 'forgotten')),
    -- Insight 6: temporal scope
    scope_type TEXT NOT NULL DEFAULT 'user'
        CHECK(scope_type IN ('user', 'short_term', 'long_term', 'profile')),
    -- Plan § Cross-platform identity (Gap 7): per-user isolation
    user_id TEXT,
    session_id TEXT,
    -- Tier 3: 3-tier isolation (public / source / private / private_<user>).
    -- 'private' alone = generic private (caller must include user_id to disambiguate).
    -- 'private_<user>' = explicit per-user private (e.g. 'private_sunny').
    tier TEXT NOT NULL DEFAULT 'public'
            CHECK(tier IN ('public', 'source', 'private', 'repo')
                  OR tier LIKE 'private\_%' ESCAPE '\'),
    -- Stable ID for dedup (Plan § dedup)
    stable_id TEXT,
    -- Embedding cache invalidation (Plan § Embedding cache invalidation)
    embedding_version INTEGER NOT NULL DEFAULT 1,
    -- Plan § Verdict vs publishable independence (Insight 13, line 647-654)
    -- publishable=true means "copy this fact to public.db on next compact if it lives in source.db"
    -- Independent of `verdict` — admin can publish a settled fact or a thin fact
    -- (thin + publishable=true publishes with thin verdict, not auto-promoted)
    publishable INTEGER NOT NULL DEFAULT 0
        CHECK(publishable IN (0, 1)),
    FOREIGN KEY (candidate_id) REFERENCES memory_candidates(id),
    FOREIGN KEY (event_id) REFERENCES events(id),
    FOREIGN KEY (parent_revision_id) REFERENCES memory_canonical(id),
    FOREIGN KEY (superseded_by) REFERENCES memory_canonical(id)
);

CREATE INDEX IF NOT EXISTS idx_canonical_user ON memory_canonical(user_id, tombstoned) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_canonical_verdict ON memory_canonical(verdict, last_confirmed_at);
CREATE INDEX IF NOT EXISTS idx_canonical_tier ON memory_canonical(tier, tombstoned);
CREATE INDEX IF NOT EXISTS idx_canonical_tier_user ON memory_canonical(tier, user_id, tombstoned);
CREATE INDEX IF NOT EXISTS idx_canonical_stable ON memory_canonical(stable_id) WHERE stable_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_canonical_access ON memory_canonical(access_count DESC);
CREATE INDEX IF NOT EXISTS idx_canonical_publishable ON memory_canonical(publishable, verdict) WHERE publishable = 1;

-- Per-user isolation tables (private_<user_id>)
-- These are created dynamically when first user is added

-- Audit log (Plan § Audit log model, Gap 6)
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    event TEXT NOT NULL,
    actor TEXT NOT NULL,                          -- 'first_admin' | 'admin:<id>' | 'user:<id>' | 'system'
    target_type TEXT,                              -- 'fact' | 'skill' | 'cron' | 'db' | 'user'
    target_id TEXT,
    old_state TEXT,                                -- JSON snapshot before change
    new_state TEXT,                                -- JSON snapshot after change
    reason TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    severity TEXT NOT NULL DEFAULT 'info'           -- info | warning | critical
);

CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log(target_type, target_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_severity ON audit_log(severity, ts DESC) WHERE severity != 'info';

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Cascade write queue (added in v1.2.0 schema v4, 2026-08-16).
-- When nest.store() fails during promote_candidate (e.g. embedding model
-- OOM, LanceDB unavailable), the fact_id + content + tier + user_id
-- are queued here for retry. A separate replay pass (manual via
-- `am cascade replay` / `POST /v1/cascade/replay`, or scheduled cron)
-- processes pending rows and re-attempts the embed.
--
-- Design (EverOS md_change_state pattern, simplified for astor's
-- SQLite-only stack): durable queue inside the same bus DB; status
-- transitions pending -> succeeded | failed; failed rows are kept for
-- post-mortem and cleared by am cascade purge.
CREATE TABLE IF NOT EXISTS cascade_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id INTEGER NOT NULL,
    operation TEXT NOT NULL,                   -- 'embed_insert' | 'lex_index' | 'provenance_link'
    tier TEXT NOT NULL,                        -- public | source | private_<user> | repo_<id>
    user_id TEXT,                              -- NULL for public/source; user_id for private/repo
    payload TEXT NOT NULL DEFAULT '{}',        -- JSON: {"content": "...", "scope": "long_term"}
    enqueued_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_attempt_at DATETIME,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    status TEXT NOT NULL DEFAULT 'pending'     -- pending | succeeded | failed
);

CREATE INDEX IF NOT EXISTS idx_cascade_pending
    ON cascade_state(status, enqueued_at)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_cascade_fact ON cascade_state(fact_id);
"""


def astor_upgrade_all_tier_dbs() -> None:
    """Run v1→v2, v2→v3, v3→v4, v4→v5 migrations across all known (tier, user_id)
    scope DBs (public, source, every private_<user>, every repo_<id>).

    Called once at server startup so all 9 schema files share the same
    schema_version. Cross-tier provenance probes then succeed instead
    of crashing on 'no such column'.
    """
    from .._internal.acl_layout import (
        list_user_ids, list_repo_ids, Tier, get_db_path,
    )
    scopes: list[tuple[str, str | None]] = [
        (Tier.PUBLIC.value, None),
        (Tier.SOURCE.value, None),
    ]
    scopes += [(Tier.PRIVATE.value, u) for u in list_user_ids()]
    scopes += [(Tier.REPO.value, r) for r in list_repo_ids()]
    for tier, user_id in scopes:
        try:
            p = get_db_path(tier, 'bus', user_id)
            if not p.exists():
                continue
            conn = sqlite3.connect(str(p), timeout=5)
            try:
                _astor_upgrade_v1_to_v2(conn)
                _astor_upgrade_v2_to_v3(conn)
                _astor_upgrade_v3_to_v4(conn)
                _astor_upgrade_v4_to_v5(conn)
                conn.commit()
            finally:
                conn.close()
        except Exception:
            # Some DBs may be locked or corrupted — skip, do not crash
            pass


def astor_init_schema(conn: sqlite3.Connection) -> None:
    """
    Initialize schema. Idempotent (uses IF NOT EXISTS).
    Safe to call on a v1-schema DB — auto-migrates by ALTER TABLE ADD COLUMN.

    Order:
      1. executescript(SCHEMA_SQL) — IF NOT EXISTS creates new tables/cols (no-op for v1)
      2. ALTER TABLE for any new columns (v1 → v2 publishable, v2 → v3 provenance)
      3. CREATE INDEX only AFTER columns exist (same ordering issue as nest)
    """
    conn.executescript(SCHEMA_SQL)
    _astor_upgrade_v1_to_v2(conn)
    _astor_upgrade_v2_to_v3(conn)
    _astor_upgrade_v3_to_v4(conn)
    _astor_upgrade_v4_to_v5(conn)
    # Index that depends on the publishable column must be created AFTER the column exists.
    # The executescript above emits CREATE INDEX inside the same script as the table,
    # which works for fresh DBs but errors on v1 databases because the column doesn't exist yet.
    # Re-create the index here — IF NOT EXISTS makes it a no-op when first run succeeded.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_canonical_publishable "
        "ON memory_canonical(publishable, verdict) WHERE publishable = 1"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_canonical_tier_user "
        "ON memory_canonical(tier, user_id, tombstoned)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
        (SCHEMA_VERSION,),
    )
    conn.commit()


def _astor_upgrade_v1_to_v2(conn: sqlite3.Connection) -> None:
    """
    Add `publishable` column to memory_canonical if it doesn't exist yet.
    SQLite has no `ADD COLUMN IF NOT EXISTS`, so probe via PRAGMA table_info.
    Idempotent — safe to call multiple times.
    """
    try:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(memory_canonical)"
        ).fetchall()}
        if "publishable" not in cols:
            conn.execute(
                "ALTER TABLE memory_canonical ADD COLUMN publishable INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_canonical_publishable "
                "ON memory_canonical(publishable, verdict) WHERE publishable = 1"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_canonical_tier_user "
                "ON memory_canonical(tier, user_id, tombstoned)"
            )
    except Exception:
        # Table doesn't exist yet (fresh DB) — IF NOT EXISTS created it with publishable.
        pass


def _astor_upgrade_v2_to_v3(conn: sqlite3.Connection) -> None:
    """
    2026-08-16: provenance-graph columns.
        parent_fact_ids    TEXT (JSON array of fact_ids this fact derived from)
        provenance_kind    TEXT (rule | extracted | inferred | manual | merged)
        provenance_agent   TEXT (which producer/forge/operator)
        provenance_depth   INTEGER (distance from source event; 0 = directly
                           observed, 1 = first derivative, etc.)
        provenance_at      DATETIME (when the lineage was last updated)
    Idempotent — uses PRAGMA table_info to probe.
    """
    try:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(memory_canonical)"
        ).fetchall()}
    except Exception:
        return
    alters = []
    if "parent_fact_ids" not in cols:
        alters.append(("parent_fact_ids", "TEXT"))
    if "provenance_kind" not in cols:
        alters.append(("provenance_kind", "TEXT DEFAULT 'extracted'"))
    if "provenance_agent" not in cols:
        alters.append(("provenance_agent", "TEXT"))
    if "provenance_depth" not in cols:
        alters.append(("provenance_depth", "INTEGER NOT NULL DEFAULT 0"))
    if "provenance_at" not in cols:
        alters.append(("provenance_at", "DATETIME"))
    for col, decl in alters:
        try:
            conn.execute(
                f"ALTER TABLE memory_canonical ADD COLUMN {col} {decl}"
            )
        except Exception:
            pass
    if alters:
        # Index parent_fact_ids so we can quickly find "who derived from me"
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_canonical_parent ON memory_canonical(parent_fact_ids)"
            )
        except Exception:
            pass
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_canonical_provenance ON memory_canonical(provenance_kind, provenance_agent)"
            )
        except Exception:
            pass


def _astor_upgrade_v3_to_v4(conn: sqlite3.Connection) -> None:
    """
    2026-08-16: cascade write queue for embed-write failures.

    Creates cascade_state table if absent. SQLite has no `CREATE TABLE IF
    NOT EXISTS` issue (unlike ADD COLUMN), so the SCHEMA_SQL block already
    creates the table on fresh DBs. This migration handles the upgrade case
    where an existing v3 DB doesn't yet have the table.

    Idempotent — safe to call multiple times.
    """
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cascade_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fact_id INTEGER NOT NULL,
                operation TEXT NOT NULL,
                tier TEXT NOT NULL,
                user_id TEXT,
                payload TEXT NOT NULL DEFAULT '{}',
                enqueued_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                last_attempt_at DATETIME,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cascade_pending "
            "ON cascade_state(status, enqueued_at) WHERE status = 'pending'"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cascade_fact ON cascade_state(fact_id)"
        )
    except Exception:
        # Best-effort — table creation may fail if locked, etc.
        pass


def _astor_upgrade_v4_to_v5(conn: sqlite3.Connection) -> None:
    """
    2026-08-16 (v1.2.0 ship): keywords + context columns on memory_canonical.

    Pattern adopted from A-MEM (agiresearch/A-mem, arXiv:2502.12110):
    - `keywords` — JSON array of 3-7 keywords/phrases extracted by LLM
      (regex mode derives heuristically). Powers hybrid_merge rerank via
      Jaccard boost.
    - `context` — 1-2 sentence human-readable summary. Used by viewer +
      admin audit for context at-a-glance.

    Idempotent — uses PRAGMA table_info to probe + ALTER TABLE ADD COLUMN.
    """
    try:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(memory_canonical)"
        ).fetchall()}
    except Exception:
        return
    alters = []
    if "keywords" not in cols:
        alters.append(("keywords", "TEXT NOT NULL DEFAULT '[]'"))
    if "context" not in cols:
        alters.append(("context", "TEXT NOT NULL DEFAULT ''"))
    for col, decl in alters:
        try:
            conn.execute(
                f"ALTER TABLE memory_canonical ADD COLUMN {col} {decl}"
            )
        except Exception:
            pass


def astor_verify_schema(conn: sqlite3.Connection) -> dict:
    """Verify schema matches expected. Returns dict with status."""
    c = conn.cursor()
    # Check expected tables exist
    c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    actual_tables = {row[0] for row in c.fetchall()}
    expected_tables = {
        'events', 'memory_candidates', 'memory_canonical',
        'audit_log', 'schema_version',
    }
    missing = expected_tables - actual_tables
    return {
        'schema_version': SCHEMA_VERSION,
        'tables_present': actual_tables,
        'missing': missing,
        'ok': len(missing) == 0,
    }
