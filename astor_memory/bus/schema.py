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
SCHEMA_VERSION = 19  # v1.16.69 (2026-10-05) explicit_user flag on events (Ship C #2 write-policy gate)

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
    -- v1.14.74 (2026-09-18): 4-tier memory taxonomy inspired by Hindsight ACL 2026 paper
    -- memory_class discriminates "world_fact" (objective) / "experience" (subjective) /
    -- "observation" (multi-evidence synthesis) / "mental_model" (cross-cutting).
    -- Earlier rows default to 'world_fact' (safest fallback for old data).
    memory_class TEXT NOT NULL DEFAULT 'world_fact'
        CHECK(memory_class IN ('world_fact', 'experience', 'observation', 'mental_model')),
    -- Plan § Cross-platform identity (Gap 7): per-user isolation
    user_id TEXT,
    session_id TEXT,
    -- Tier 3: 3-tier isolation (public / source / private / private_<user>).
    -- 'private' alone = generic private (caller must include user_id to disambiguate).
    -- 'private_<user>' = explicit per-user private (e.g. 'private_alice').
    tier TEXT NOT NULL DEFAULT 'public'
            CHECK(tier IN ('public', 'source', 'private', 'repo')
                  OR tier LIKE 'private\\_%' ESCAPE '\\'),
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
    -- v1.10.0: temporal boost support. event_date = ISO-8601 (YYYY-MM-DD
    -- or YYYY-MM); event_date_precision = day|month|year|none.
    event_date TEXT,
    event_date_precision TEXT NOT NULL DEFAULT 'none'
        CHECK(event_date_precision IN ('day', 'month', 'year', 'none')),
    -- v1.14.21 (2026-09-15 Ship B): RippleMem-style structured entity binding.
    -- Stores list of {type, value, fact_id} extracted at forge time. Distinct
    -- from keywords (semantic / hybrid-search boost) and context (human-readable).
    -- Used by future Ship C (entity_lex 3rd retrieval path) and by any caller
    -- that wants to bind "who/where/when" without reparsing content. Format:
    -- [{"type": "person|location|time|topic", "value": "...", "fact_id": N}]
    -- Empty list is fine — old facts without extraction still valid.
    entities_json TEXT NOT NULL DEFAULT '[]',
    -- v1.14.31 Ship S3 (2026-09-15): created_at populated on INSERT in
    -- promote_candidate so time_range reorder can use it as a soft
    -- proximity signal for legacy facts without event_date. ISO 8601.
    -- Backfill populates current ISO timestamp on existing rows.
    created_at TEXT,
    -- v1.14.74+ (2026-09-27 Ship A2-Akasha): evidence-grounded source linking.
    -- evidence_quote = literal substring from the source that the fact
    --   was extracted from. Empty for regex-only facts that have no
    --   accessible source. Wire clients SHOULD populate when extracting
    --   from external pages / files / chat transcripts.
    -- source_ref = opaque pointer back to the origin (file:line,
    --   wiki page slug, message id, URL). Lets the recall path and
    --   audit path re-fetch the source on demand.
    -- source_hash = SHA-256 of the source content at write time.
    --   Recall compares against rehash of current source to mark
    --   facts as [stale] when the source mutates.
    evidence_quote TEXT NOT NULL DEFAULT '',
    source_ref     TEXT NOT NULL DEFAULT '',
    source_hash    TEXT NOT NULL DEFAULT '',
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
    actor TEXT NOT NULL,                          -- 'admin' | 'admin:<id>' | 'user:<id>' | 'system'
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

-- v1.6.0 schema v6 (2026-08-25): memory_experience table (OpenClaw Experience-inspired).
-- Captures "what the agent tried + what happened + what to do next time" as a
-- distinct first-class record. Distinct from memory_canonical facts:
-- canonical = static knowledge (e.g. "user prefers coffee"); experience =
-- dynamic reflection (e.g. "last 3 times user said stop, we ignored 'cancel';
-- next time, abort immediately"). outcome tag on canonical fact is the
-- TRIGGER; experience is the LEARNED REFLECTION.
CREATE TABLE IF NOT EXISTS memory_experience (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL,                -- 'public' | 'source' | 'private:<user>' | 'repo:<id>'
    user_id TEXT,                            -- owner of the experience (private_<user>)
    outcome TEXT NOT NULL DEFAULT 'neutral', -- 'success' | 'partial' | 'failure' | 'neutral'
    trigger_keywords TEXT NOT NULL DEFAULT '[]',  -- JSON array of keywords that triggered this experience
    trigger_fact_ids TEXT NOT NULL DEFAULT '[]',  -- JSON array of fact_ids that caused this experience
    action_summary TEXT NOT NULL DEFAULT '',      -- single-line: "what was tried"
    context TEXT NOT NULL DEFAULT '',            -- what was happening when this happened
    reflection TEXT NOT NULL DEFAULT '',          -- "why it failed/succeeded" (LLM-generated or manual)
    next_step_hint TEXT NOT NULL DEFAULT '',      -- "next time, do X instead"
    invocation_count INTEGER NOT NULL DEFAULT 0,  -- how many times this experience has been matched/invoked
    last_invoked_at DATETIME,                     -- timestamp of last match
    created_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    source_session_id TEXT,                       -- session where this experience originated
    importance REAL NOT NULL DEFAULT 0.7          -- mirrors memory_canonical.importance for hybrid ranking
);
CREATE INDEX IF NOT EXISTS idx_experience_namespace_user
    ON memory_experience(namespace, user_id, outcome);
CREATE INDEX IF NOT EXISTS idx_experience_outcome
    ON memory_experience(outcome, importance);
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
                # v1.10.3: include v6→v7 (event_date) and v7→v8 (experience
                # action_embedding). The original loop only ran up to
                # v4→v5 which meant private_<user> DBs stayed at v5 while
                # the public DB was on v6+ — out of sync, would fail the
                # later migrations when the private path first ran.
                _astor_upgrade_v1_to_v2(conn)
                _astor_upgrade_v2_to_v3(conn)
                _astor_upgrade_v3_to_v4(conn)
                _astor_upgrade_v4_to_v5(conn)
                _astor_upgrade_v5_to_v6(conn)
                _astor_upgrade_v6_to_v7(conn)
                _astor_upgrade_v7_to_v8(conn)
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
    _astor_upgrade_v5_to_v6(conn)
    _astor_upgrade_v6_to_v7(conn)
    _astor_upgrade_v7_to_v8(conn)
    _astor_upgrade_v8_to_v9(conn)
    _astor_upgrade_v9_to_v10(conn)
    _astor_upgrade_v10_to_v11(conn)
    _astor_upgrade_v11_to_v12(conn)
    _astor_upgrade_v12_to_v13(conn)
    _astor_upgrade_v13_to_v14(conn)
    _astor_upgrade_v14_to_v15(conn)
    _astor_upgrade_v15_to_v16(conn)
    _astor_upgrade_v16_to_v17(conn)
    _astor_upgrade_v17_to_v18(conn)
    _astor_upgrade_v18_to_v19(conn)
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


def _astor_upgrade_v5_to_v6(conn: sqlite3.Connection) -> None:
    """
    2026-08-25 (v1.6.0 ship): memory_experience table (OpenClaw Experience-inspired).

    The new table is created by SCHEMA_SQL (CREATE TABLE IF NOT EXISTS).
    This upgrade function only handles:
      - backfilling 'namespace' column on existing rows (default 'private:admin')
        — there are no rows yet since the table is fresh.
      - any future ALTER TABLE on memory_experience goes here.

    Idempotent. Safe to call on a v5 DB — table just gets created via
    SCHEMA_SQL on first run.
    """
    # No-op for now. Future ALTER TABLE goes here when columns are added.
    pass



def _astor_upgrade_v7_to_v8(conn: sqlite3.Connection) -> None:
    """v1.10.3 (2026-08-26): precompute action_summary embeddings at write-time.

    Why: /v1/read with reflect=true calls bus.match_experiences which, on
    kw=0 hits, embeds the query + ~10 unmatched experience action_summaries
    on every recall (~700-1100ms with bge-base). With this column, new
    experiences store their action_embedding at insert time and
    match_experiences just loads + matmuls (~5ms).

    Migration: ADD COLUMN action_embedding BLOB. Idempotent. Legacy rows
    (no embedding) still take the slow path until they're backfilled.
    """
    try:
        conn.execute(
            "ALTER TABLE memory_experience ADD COLUMN action_embedding BLOB"
        )
    except Exception:
        pass  # column already exists
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_experience_emb_ready "
            "ON memory_experience(namespace, outcome) WHERE action_embedding IS NOT NULL"
        )
    except Exception:
        pass


def _astor_upgrade_v8_to_v9(conn: sqlite3.Connection) -> None:
    """v1.14.21 (2026-09-15 Ship B): RippleMem-style structured entity binding.

    Adds `entities_json` column to memory_canonical. Stores
    [{"type": "person|location|time|topic", "value": "...", "fact_id": N}]
    as a JSON list. Extracted at forge time and during backfill.

    Backward compat: column defaults to '[]' (empty list). Existing rows
    unaffected. New writes populate it via forge.extract_entities().

    Index added for future Ship C (entity_lex 3rd retrieval path).
    """
    try:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(memory_canonical)"
        ).fetchall()}
    except Exception:
        return
    if "entities_json" not in cols:
        try:
            conn.execute(
                "ALTER TABLE memory_canonical ADD COLUMN entities_json "
                "TEXT NOT NULL DEFAULT '[]'"
            )
        except Exception:
            pass
    # Index on JSON1 json_each for future Ship C. No-op until Ship C ships.
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_canonical_entities "
            "ON memory_canonical(json_each(entities_json)) WHERE tombstoned = 0"
        )
    except Exception:
        pass


def _astor_upgrade_v9_to_v10(conn: sqlite3.Connection) -> None:
    """v1.14.31 Ship S3 (2026-09-15): add `created_at` column to
    memory_canonical for time_range proximity boost on legacy rows
    without event_date. Backfills existing rows with current ISO
    timestamp; promote_candidate will populate it on new writes.

    Idempotent: uses PRAGMA table_info to probe.
    """
    try:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(memory_canonical)"
        ).fetchall()}
    except Exception:
        return
    if 'created_at' not in cols:
        try:
            conn.execute(
                "ALTER TABLE memory_canonical ADD COLUMN created_at TEXT"
            )
        except Exception:
            pass
    # Backfill: set created_at = CURRENT_TIMESTAMP for rows where NULL
    # (legacy rows from pre-v1.14.31 era). Done as UPDATE so it's a
    # single batch operation; safe to re-run.
    try:
        conn.execute(
            "UPDATE memory_canonical SET created_at = "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE created_at IS NULL OR created_at = ''"
        )
    except Exception:
        pass







def _astor_upgrade_v13_to_v14(conn: sqlite3.Connection) -> None:
    """v1.16.8 (2026-09-30) Ship: bi-temporal fact lifecycle (Zep/Graphiti-inspired).

    Adds four columns to memory_canonical to support bi-temporal
    validity tracking — when a fact was true in the real world
    (valid_from / valid_until) and whether it was superseded by another
    fact (invalidated_by / invalidated_at / invalidated_reason).

    Article "2026 Agent 记忆六条路线" route 5 (Temporal KG) identified
    this gap in route 4 (Agentic Memory Pipeline / Mem0-style): old
    facts linger forever and pollute recall after a "correction".

    Migration is additive — all columns have safe defaults; existing
    rows get valid_from = created_at, valid_until = NULL (active).
    Idempotent via PRAGMA table_info probe.
    """
    try:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(memory_canonical)"
        ).fetchall()}
    except Exception:
        return
    additions = [
        ("valid_from",        "DATETIME"),
        ("valid_until",       "DATETIME"),
        ("invalidated_by",    "INTEGER"),
        ("invalidated_at",    "DATETIME"),
        ("invalidated_reason", "TEXT NOT NULL DEFAULT ''"),
    ]
    for name, decl in additions:
        if name in cols:
            continue
        try:
            conn.execute(
                f"ALTER TABLE memory_canonical ADD COLUMN {name} {decl}"
            )
        except Exception:
            pass
    # Backfill: valid_from = created_at for existing rows.
    try:
        conn.execute(
            "UPDATE memory_canonical SET valid_from = created_at "
            "WHERE valid_from IS NULL AND created_at IS NOT NULL"
        )
    except Exception:
        pass
    # Index on valid_until for fast "active facts only" queries.
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_canonical_valid_until "
            "ON memory_canonical(valid_until) "
            "WHERE valid_until IS NULL"
        )
    except Exception:
        pass


def _astor_upgrade_v10_to_v11(conn):
    """v1.14.73 stub fix migration (no DB schema changes, audit-only)."""
    return None


def _astor_upgrade_v12_to_v13(conn: sqlite3.Connection) -> None:
    """v1.14.74+ (2026-09-27 Ship A2-Akasha): Akasha-style evidence-grounded
    source linking on memory_canonical.

    Adds three columns:
      - evidence_quote : literal substring of the source the fact was
        extracted from. Empty default (regex-only / no-source facts
        still valid).
      - source_ref : opaque origin pointer ('wiki:slug', 'session:<id>',
        'file:<path:line>', 'url:<href>').
      - source_hash : SHA-256 hex of the source content at write time.
        Recall compares against a fresh hash of the source URL to
        mark [stale] when the source mutates.

    All columns default to '' (text) so the migration is additive and
    safe on every existing row. Idempotent: PRAGMA table_info probe.
    """
    try:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(memory_canonical)"
        ).fetchall()}
    except Exception:
        return
    additions = [
        ("evidence_quote", "TEXT NOT NULL DEFAULT ''"),
        ("source_ref",     "TEXT NOT NULL DEFAULT ''"),
        ("source_hash",    "TEXT NOT NULL DEFAULT ''"),
    ]
    for name, decl in additions:
        if name in cols:
            continue
        try:
            conn.execute(
                f"ALTER TABLE memory_canonical ADD COLUMN {name} {decl}"
            )
        except Exception:
            pass
    # Index for source_ref-based retrieval (recall by origin pointer).
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_canonical_source_ref "
            "ON memory_canonical(source_ref) WHERE source_ref != ''"
        )
    except Exception:
        pass


def _astor_upgrade_v14_to_v15(conn: sqlite3.Connection) -> None:
    """v1.16.12 (2026-09-30) Ship: M-flow L0 episode layer.

    Article: "受生物启发的认知记忆引擎 M-flow" 9/30 — 4-layer cone
    graph (Episode → Facet → FacetPoint → Entity). astor already has
    L1 memory_canonical (facts), L2 memory_experience (extracted
    experiences), L3 mental_model (deep insights). Missing: L0
    episodes — raw conversation chunks that L1 facts derive from.

    Why L0 matters: when a fact is recalled, the user often needs
    EVIDENCE (the original turn where it was said). Without L0 we can
    only return the derived fact, not the source.

    Schema: new `episodes` table stores raw conversation chunks.
    - id (PK)
    - namespace + user_id + tier (3-tier isolation, matches other tables)
    - session_id (correlates with agent session)
    - raw_text (the actual turn / conversation chunk)
    - derived_fact_ids (JSON array of fact_ids this episode gave rise to)
    - entities_json (entities extracted from raw_text)
    - created_at (DATETIME)
    - embedding (BLOB, optional, for vector recall)

    Idempotent: CREATE TABLE IF NOT EXISTS.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            namespace TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT '',
            tier TEXT NOT NULL DEFAULT 'public',
            session_id TEXT NOT NULL DEFAULT '',
            raw_text TEXT NOT NULL,
            derived_fact_ids TEXT NOT NULL DEFAULT '[]',
            entities_json TEXT NOT NULL DEFAULT '[]',
            created_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            embedding BLOB
        )
        """
    )
    # Indexes for common queries
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_episodes_session "
        "ON episodes(namespace, user_id, session_id) "
        "WHERE session_id != ''"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_episodes_created "
        "ON episodes(namespace, user_id, created_at DESC)"
    )


def _astor_upgrade_v15_to_v16(conn: sqlite3.Connection) -> None:
    """v1.16.29 (2026-10-01) visibility tier + provenance_kind.

    User feedback (R-class 7587/7568/11938):
      - Replace leaky "public tier defaults for all users" with explicit
        commons/personal visibility classification based on kind + content signals.
      - AUTO_COMMONS_KINDS = {method, recipe, lesson, success_pattern,
        failure_pattern, mental_model, knowledge_page, flow}
      - PII / emotion / first-person triggers force personal.

    Schema additions:
      - visibility TEXT NOT NULL DEFAULT 'personal'
      - provenance_kind TEXT NOT NULL DEFAULT 'user_write'

    Idempotent: ALTER TABLE ADD COLUMN via PRAGMA check.
    """
    from astor_memory.nest.visibility_classifier import (
            has_pii, has_first_person, has_emotion, has_geographic, AUTO_COMMONS_KINDS,
        )

    def _col_exists(c, col_name):
        rows = c.execute("PRAGMA table_info(memory_canonical)").fetchall()
        return any(r[1] == col_name for r in rows)

    if not _col_exists(conn, 'visibility'):
        conn.execute("ALTER TABLE memory_canonical ADD COLUMN visibility TEXT NOT NULL DEFAULT 'personal'")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_canonical_visibility_kind "
            "ON memory_canonical(visibility, kind, tombstoned)"
        )
    if not _col_exists(conn, 'provenance_kind'):
        conn.execute("ALTER TABLE memory_canonical ADD COLUMN provenance_kind TEXT NOT NULL DEFAULT 'user_write'")

    # Backfill: existing rows — promote auto-commons kind if content is clean.
    placeholders = ','.join('?' * len(AUTO_COMMONS_KINDS))
    rows = conn.execute(
        f"SELECT id, content FROM memory_canonical "
        f"WHERE visibility = 'personal' AND tombstoned = 0 AND kind IN ({placeholders})",
        tuple(AUTO_COMMONS_KINDS)
    ).fetchall()
    for _id, _content in rows:
        _content = _content or ''
        if (not has_pii(_content)
                and not has_first_person(_content)
                and not has_emotion(_content)
                and not has_geographic(_content)):
            conn.execute(
                "UPDATE memory_canonical SET visibility = 'commons' WHERE id = ?",
                (_id,)
            )
    conn.commit()


def _astor_upgrade_v11_to_v12(conn) -> None:
    """v1.14.74 (2026-09-18): Hindsight 4-tier memory_class taxonomy column."""
    try:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(memory_canonical)"
        ).fetchall()}
    except Exception:
        return
    if "memory_class" in cols:
        return
    try:
        conn.execute(
            "ALTER TABLE memory_canonical ADD COLUMN memory_class TEXT NOT NULL DEFAULT 'world_fact'"
        )
    except Exception:
        pass


def _astor_upgrade_v10_to_v11(conn) -> None:
    """v1.14.73 stub fix migration (no DB schema changes, audit-only)."""
    return None


def _astor_upgrade_v6_to_v7(conn: sqlite3.Connection) -> None:
    """v1.10.0 (2026-08-26): add event_date + event_date_precision columns
    to memory_canonical.

    Background: v1.3.0 (2026-08-25) shipped the AstorFact dataclass with
    event_date/event_date_precision fields, but the corresponding DB columns
    were NEVER added to memory_canonical — only stored in metadata. The
    /v1/read endpoint reads event_date via SELECT column which silently
    fails at runtime.

    This migration adds the columns + indexes. Safe on fresh DBs (CREATE
    TABLE already has them, ALTER TABLE errors silently via try/except).
    """
    # v1.10.0: temporal boost requires event_date column for hybrid_merge
    cols_to_add = [
        ("event_date", "TEXT"),
        ("event_date_precision", "TEXT NOT NULL DEFAULT 'none'"),
    ]
    for col, decl in cols_to_add:
        try:
            conn.execute(
                f"ALTER TABLE memory_canonical ADD COLUMN {col} {decl}"
            )
        except Exception:
            pass  # column already exists
    # Index: dates enable temporal filter / sorting
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_canonical_event_date "
            "ON memory_canonical(event_date)"
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


def _astor_upgrade_v16_to_v17(conn: sqlite3.Connection) -> None:
    """
    v1.16.66 (2026-10-05) Ship P24 #1 — raw_chat_chunk RAG layer (BigGuo
    WeChat "双层记忆" article, second layer: context-aware chunk retrieval).

    Adds 4 columns to `events` table so the same append-only event log
    carries both metadata events AND raw conversation chunks. The columns
    are NULLABLE — they only carry meaning when action='raw_chat_chunk'.

      chunk_role      TEXT   ('user' | 'assistant' | 'mixed' | 'system')
      chunk_window_id TEXT   UUID of the parent conversation window
                              (groups turns that share a logical session)
      chunk_turns     TEXT   JSON array of {role, content} pairs, the
                              raw turn-by-turn content
      chunk_prefix    TEXT   LLM-generated prefix for context-aware
                              retrieval, e.g.
                              "[user=alice ts=2026-10-05 14:32 topic=passport]"

    Index on (action, namespace, ts DESC) makes /v1/chat/recall cheap.

    Idempotent: ALTER TABLE ADD COLUMN via PRAGMA check + try/except.
    """
    try:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(events)"
        ).fetchall()}
    except Exception:
        return

    alters = []
    if "chunk_role" not in cols:
        alters.append(("chunk_role", "TEXT"))
    if "chunk_window_id" not in cols:
        alters.append(("chunk_window_id", "TEXT"))
    if "chunk_turns" not in cols:
        alters.append(("chunk_turns", "TEXT"))
    if "chunk_prefix" not in cols:
        alters.append(("chunk_prefix", "TEXT"))

    for col, decl in alters:
        try:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col} {decl}")
        except Exception:
            pass

    # Index for /v1/chat/recall: filter by action='raw_chat_chunk' + namespace.
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_action_namespace_ts "
            "ON events(action, namespace, ts DESC)"
        )
    except Exception:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_chunk_window "
            "ON events(chunk_window_id) WHERE chunk_window_id IS NOT NULL"
        )
    except Exception:
        pass


def _astor_upgrade_v17_to_v18(conn: sqlite3.Connection) -> None:
    """
    v1.16.68 (2026-10-05) Ship B #1 — `status` column on memory_canonical.

    Distinguishes "tombstoned" (= deletion; not visible) from
    "inactive" (= temporarily disabled; still visible but flagged).

    MemFit § 8 + Basic Memory 2026 §08 both call this out: when a fact
    is superseded (user changed preference), the system should EDIT
    the existing entry to mark it inactive, NOT delete it. Our existing
    `verdict=forgotten` + `tombstoned=1` mixed deletion + staleness
    semantics. Adding a separate `status` field lets recall surface
    inactive facts with a ⚠ marker so the LLM can reason about
    history-of-thought rather than pretend the fact never existed.

    Values:
      active    — default, fully recallable
      inactive  — superseded/disabled; recallable but flagged ⚠
      archived  — long-retired; recallable only via explicit user query

    Idempotent: ALTER TABLE + PRAGMA guard.
    """
    try:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(memory_canonical)"
        ).fetchall()}
    except Exception:
        return
    if "status" in cols:
        return
    try:
        conn.execute(
            "ALTER TABLE memory_canonical ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
        )
    except Exception:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_canonical_status "
            "ON memory_canonical(status, tombstoned) WHERE tombstoned = 0"
        )
    except Exception:
        pass


def _astor_upgrade_v18_to_v19(conn: sqlite3.Connection) -> None:
    """
    v1.16.69 (2026-10-05) Ship C #2 — write-policy 3-of-3 gate.

    Adds `explicit_user INTEGER NOT NULL DEFAULT 0` to events.
    Basic Memory 2026 §06 gate: only facts matching >=2 of 3 conditions
    should be written to long-term memory:
      1. May be useful in the future
      2. User explicitly expressed OR confirmed
      3. Independently verifiable
    Our shell_hooks auto-capture every tool result, which is too
    permissive. Now the gate matters at PROMOTE time (events ->
    candidate -> canonical). A new `bus.promote_candidate()` flag
    `require_explicit_user` (default True) drops candidates whose
    source event lacks explicit_user=1.

    Idempotent ALTER TABLE ADD COLUMN.
    """
    try:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(events)"
        ).fetchall()}
    except Exception:
        return
    if "explicit_user" in cols:
        return
    try:
        conn.execute(
            "ALTER TABLE events ADD COLUMN explicit_user INTEGER NOT NULL DEFAULT 0"
        )
    except Exception:
        pass
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_explicit_user "
            "ON events(explicit_user) WHERE explicit_user = 1"
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
