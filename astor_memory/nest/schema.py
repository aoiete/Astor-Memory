"""
Nest schema: vector embeddings stored in their own SQLite database per (tier, user).

Per plan § 9-db layout (2026-08-15 supersede):
- 3 tier × 3 store = 9 SQLite files total
- nest (this module) owns 3 files: one per tier scope
- per-user private embedding goes in users/<u>/memory/astor_nest_<u>.db

Why nest must align with bus on user_id / tier / publishable:
1. Embedding lookup is keyed by fact_id, but tier + user_id must match the source fact
   in bus. Mismatched embedding rows (e.g. user's embedding in public tier) cause leaks.
2. publishable=true means the embedding must also be copyable on `am compact`.
3. Storing these fields on embeddings makes `astor_recall()` an O(1) tier filter.

Schema v2 (2026-08-15) — adds user_id + tier + publishable to embeddings.
v1 only had fact_id; migration done by ALTER TABLE in astor_init_nest_schema.
"""

from __future__ import annotations

import sqlite3

NEST_SCHEMA_VERSION = 2

NEST_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS embeddings (
    fact_id INTEGER PRIMARY KEY,
    embedding BLOB NOT NULL,
    model_name TEXT NOT NULL,
    dim INTEGER NOT NULL,
    created_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- ACL alignment with bus.memory_canonical (plan §9-db consistency)
    user_id TEXT NOT NULL DEFAULT '_current',
    tier TEXT NOT NULL DEFAULT 'private'
        CHECK(tier IN ('public', 'source', 'private')),
    publishable INTEGER NOT NULL DEFAULT 0
        CHECK(publishable IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model_name);
CREATE INDEX IF NOT EXISTS idx_embeddings_user_tier ON embeddings(user_id, tier);
CREATE INDEX IF NOT EXISTS idx_embeddings_publishable ON embeddings(publishable) WHERE publishable = 1;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
"""


def astor_init_nest_schema(conn: sqlite3.Connection) -> None:
    """
    Initialize nest schema. Idempotent + auto-upgrades from v1 (adds user_id/tier/publishable).

    v1 (2026-08-15 prior): embeddings = (fact_id, embedding, model_name, dim, created_at, updated_at)
    v2 (this version):    + (user_id DEFAULT '_current', tier DEFAULT 'private', publishable DEFAULT 0)

    Order matters:
      1. Create baseline schema (IF NOT EXISTS) — covers fresh DBs and is a no-op
         for v1 DBs (table already exists).
      2. ALTER TABLE to add missing columns to v1 tables.
      3. CREATE INDEX only AFTER columns exist — otherwise SQLite errors with
         "no such column: user_id" trying to reference a column that doesn't yet exist.

    This handles both fresh DBs and existing v1 DBs in one call.
    """
    # Step 1: ensure table exists (no-op if v1 already there)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS embeddings (
        fact_id INTEGER PRIMARY KEY,
        embedding BLOB NOT NULL,
        model_name TEXT NOT NULL,
        dim INTEGER NOT NULL,
        created_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)
    """)
    # Step 2: add columns if missing (idempotent — checks PRAGMA first)
    _astor_upgrade_nest_v1_to_v2(conn)
    # Step 3: now columns exist — safe to create indexes that reference them
    conn.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_user_tier ON embeddings(user_id, tier)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_publishable ON embeddings(publishable) WHERE publishable = 1")
    # Step 4: record version
    conn.execute(
        "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
        (NEST_SCHEMA_VERSION,),
    )
    conn.commit()


def _astor_upgrade_nest_v1_to_v2(conn: sqlite3.Connection) -> None:
    """
    Add `user_id`, `tier`, `publishable` columns to embeddings table if missing.
    Idempotent. SQLite has no `ADD COLUMN IF NOT EXISTS` so probe via PRAGMA.
    """
    try:
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(embeddings)"
        ).fetchall()}
        if "user_id" not in cols:
            conn.execute(
                "ALTER TABLE embeddings ADD COLUMN user_id TEXT NOT NULL DEFAULT '_current'"
            )
        if "tier" not in cols:
            conn.execute(
                "ALTER TABLE embeddings ADD COLUMN tier TEXT NOT NULL DEFAULT 'private'"
            )
        if "publishable" not in cols:
            conn.execute(
                "ALTER TABLE embeddings ADD COLUMN publishable INTEGER NOT NULL DEFAULT 0"
            )
    except Exception:
        # Table doesn't exist (fresh DB) — IF NOT EXISTS just created it with v2 columns.
        pass


def astor_verify_nest_schema(conn: sqlite3.Connection) -> dict:
    """Verify nest schema. Returns dict with status."""
    c = conn.cursor()
    c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    actual = {row[0] for row in c.fetchall()}
    expected = {'embeddings', 'schema_version'}
    missing = expected - actual
    return {
        'ok': not missing,
        'schema_version': NEST_SCHEMA_VERSION,
        'tables_present': actual,
        'missing': missing,
    }


__all__ = [
    'NEST_SCHEMA_SQL', 'NEST_SCHEMA_VERSION',
    'astor_init_nest_schema', 'astor_verify_nest_schema',
]
