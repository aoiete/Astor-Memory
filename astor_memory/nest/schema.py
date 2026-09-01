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

# v3 (2026-08-26): composite PK (fact_id, model_name). v2 used fact_id alone,
# which meant embedding rows for different models silently OVERWROTE each
# other on INSERT OR REPLACE. Background re-embed script discovered this
# when bge-small rows replaced bge-base rows mid-run. Fix: re-create table
# with composite PK, migrate existing rows by INSERT OR IGNORE into new
# table, drop old, rename.
NEST_SCHEMA_VERSION = 3

NEST_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS embeddings (
    fact_id INTEGER NOT NULL,
    embedding BLOB NOT NULL,
    model_name TEXT NOT NULL,
    dim INTEGER NOT NULL,
    created_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    -- ACL alignment with bus.memory_canonical (plan §9-db consistency)
    user_id TEXT NOT NULL DEFAULT '_current',
    tier TEXT NOT NULL DEFAULT 'private'
        CHECK(tier IN ('public', 'source', 'private', 'repo')),
    publishable INTEGER NOT NULL DEFAULT 0
        CHECK(publishable IN (0, 1)),
    -- v3: composite PK so different models don't overwrite each other
    PRIMARY KEY (fact_id, model_name)
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
        updated_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        tier TEXT NOT NULL DEFAULT 'private'
            CHECK(tier IN ('public', 'source', 'private', 'repo'))
    )
    """)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)
    """)
    # Step 2: add columns if missing (idempotent — checks PRAGMA first)
    _astor_upgrade_nest_v1_to_v2(conn)
    # Step 2b: v2 -> v3 migration (composite PK). Idempotent.
    _astor_upgrade_nest_v2_to_v3(conn)
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


def _astor_upgrade_nest_v2_to_v3(conn: sqlite3.Connection) -> None:
    """
    v2 -> v3 (2026-08-26): migrate embeddings PK from (fact_id) to
    (fact_id, model_name). Critical fix: v2's single-column PK allowed
    embedding rows for different models to silently OVERWRITE each other
    when callers used INSERT OR REPLACE on fact_id alone. Discovered when
    a background re-embed script (model= bge-small) wiped out bge-base
    rows on the same fact_ids.

    Migration steps (SQLite doesn't support ALTER TABLE ... PRIMARY KEY):
      1. Create new table embeddings_v3 with composite PK.
      2. Copy rows: INSERT OR IGNORE so duplicates collapse to one.
      3. Drop embeddings, rename embeddings_v3 to embeddings.
      4. Recreate indexes.

    Safe to run multiple times — idempotent.
    """
    cur = conn.cursor()
    # Check if PK is already composite (pragma table_info shows order of cols)
    info = cur.execute("PRAGMA table_info(embeddings)").fetchall()
    pk_cols = [row[1] for row in info if row[5] > 0]  # pk > 0 means PK
    if len(pk_cols) >= 2:
        # Already composite PK
        return
    if len(pk_cols) == 0:
        # Table doesn't exist (fresh DB) — schema create handles it
        return
    # v2: PK is just fact_id. Migrate.
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS embeddings_v3 (
                fact_id INTEGER NOT NULL,
                embedding BLOB NOT NULL,
                model_name TEXT NOT NULL,
                dim INTEGER NOT NULL,
                created_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                updated_at DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                user_id TEXT NOT NULL DEFAULT '_current',
                tier TEXT NOT NULL DEFAULT 'private',
                publishable INTEGER NOT NULL DEFAULT 0
                    CHECK(publishable IN (0, 1)),
                PRIMARY KEY (fact_id, model_name)
            )
        """)
        # Copy rows (INSERT OR IGNORE handles dupes if migration ran partially)
        cur.execute("""
            INSERT OR IGNORE INTO embeddings_v3
                (fact_id, embedding, model_name, dim, created_at, updated_at,
                 user_id, tier, publishable)
            SELECT fact_id, embedding, model_name, dim, created_at, updated_at,
                   user_id, tier, COALESCE(publishable, 0)
            FROM embeddings
        """)
        cur.execute("DROP TABLE embeddings")
        cur.execute("ALTER TABLE embeddings_v3 RENAME TO embeddings")
        # Recreate indexes (they were dropped with the table)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_model ON embeddings(model_name)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_user_tier ON embeddings(user_id, tier)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_publishable ON embeddings(publishable) WHERE publishable = 1")
    except Exception as _e:
        # If migration fails partway, the DB may be in an inconsistent state.
        # Re-raise — caller (astor_init_nest_schema) will retry on next startup.
        raise


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
