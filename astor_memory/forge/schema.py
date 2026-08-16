"""
Forge schema: LLM extraction cache + audit log + schema migration history.

Forge owns 3 SQLite files per plan § 9-db layout:
- ~/.astor/public/memory/astor_forge_public.db
- ~/.astor/source/memory/astor_forge_source.db
- ~/.astor/users/<u>/memory/astor_forge_<u>.db

What lives in each:
- llm_call_log: every LLM call request + response + cost (audit mandatory,
  never source-content) — required per "private data isolation" rule from
  turn discussion 2026-08-15.
- schema_migrations: every am-migrate step the agent has applied (public tier
  only — schema metadata is not private).
- extraction_cache: optional regex+LLM cache keyed by content hash.

For v0.2 we ship only llm_call_log + schema_migrations; extraction_cache is
left for v0.3 (LLM fallback chain already does cost-based caching in memory).

Lock: 2026-08-15 — forge schema first appears here. Previously forge was pure
in-memory (no DB). Adding llm_call_log is mandatory for ACL audit compliance.
"""

from __future__ import annotations

import sqlite3

FORGE_SCHEMA_VERSION = 1

FORGE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS llm_call_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- ACL alignment with bus (plan §actor types: 'first_admin' | 'admin' | 'system' | 'user:<id>')
    actor TEXT NOT NULL,
    -- The user whose data this call affects (may be the actor's own user, or
    -- 'admin' for source-tier ops). For PUBLIC calls, user_id='_public'.
    user_id TEXT NOT NULL DEFAULT '_current',
    tier TEXT NOT NULL DEFAULT 'private'
        CHECK(tier IN ('public', 'source', 'private', 'repo')),
    -- Call metadata
    provider TEXT NOT NULL,                       -- 'm3' | 'openai' | 'anthropic' | 'gemini' | 'ollama' | 'deepseek' | 'zhipu' | 'regex_fallback'
    model TEXT,                                   -- e.g. 'gpt-4o-mini' | None for regex
    operation TEXT NOT NULL,                      -- 'extract' | 'summarize' | 'classify' | 'embed'
    -- Content (private: input may contain user original text)
    input_hash TEXT NOT NULL,                     -- sha256 of input (for dedup; never reveals input)
    input_length INTEGER,                         -- byte length of input
    output_json TEXT,                             -- LLM response (parsed or raw JSON)
    -- Result
    success INTEGER NOT NULL DEFAULT 0,
    error_msg TEXT,
    latency_ms INTEGER,
    -- User reason annotation (for first_admin audit escalations)
    reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_llm_actor ON llm_call_log(actor, ts DESC);
CREATE INDEX IF NOT EXISTS idx_llm_user_tier ON llm_call_log(user_id, tier, ts DESC);
CREATE INDEX IF NOT EXISTS idx_llm_provider ON llm_call_log(provider, ts DESC);
CREATE INDEX IF NOT EXISTS idx_llm_hash ON llm_call_log(input_hash);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    ts DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    applied_by TEXT NOT NULL,                     -- 'first_admin' | 'system'
    description TEXT,
    applied_sql TEXT
);

CREATE INDEX IF NOT EXISTS idx_migrations_ts ON schema_migrations(ts DESC);

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
"""


def astor_init_forge_schema(conn: sqlite3.Connection) -> None:
    """Initialize forge schema. Idempotent (uses IF NOT EXISTS)."""
    conn.executescript(FORGE_SCHEMA_SQL)
    conn.execute(
        "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
        (FORGE_SCHEMA_VERSION,),
    )
    conn.commit()


def astor_verify_forge_schema(conn: sqlite3.Connection) -> dict:
    """Verify forge schema. Returns dict with status."""
    c = conn.cursor()
    c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    actual = {row[0] for row in c.fetchall()}
    expected = {'llm_call_log', 'schema_migrations', 'schema_version'}
    missing = expected - actual
    return {
        'ok': not missing,
        'schema_version': FORGE_SCHEMA_VERSION,
        'tables_present': actual,
        'missing': missing,
    }


__all__ = [
    'FORGE_SCHEMA_SQL', 'FORGE_SCHEMA_VERSION',
    'astor_init_forge_schema', 'astor_verify_forge_schema',
]
