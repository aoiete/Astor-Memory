"""
Audit logger: writes every private/SOURCE-tier operation to a single SQLite
file at `~/.astor/audit/astor_audit.db` (mode 0600 when first written).

Per turn discussion 2026-08-15 ("private data must not leak"):
- Every read/write to a private tier db MUST produce an audit row
- Every SOURCE-tier write MUST produce an audit row (source = astor/agent
  internal — even though user can't read source, ops still need audit trail)
- PUBLIC-tier read/write is NOT audited (public is shared knowledge; volume
  too high for full audit; only anomalies are tracked via plan §verdict)

ACL: audit DB itself is opened in append-only mode from normal processes.
The first_admin CLI is the only role that can read it. This is enforced
by file-mode 0600 + the audit db sitting at `~/.astor/audit/` which regular
agents don't have ACL access to.

For v0.2 we log:
- actor (who)
- tier + user_id (what scope)
- action (read | write | delete | compact | migrate | admin_op)
- target (table/row/component)
- reason (free text — required for first_admin admin_op, optional otherwise)
- ts, metadata

Lock: 2026-08-15. The audit_log also exists inside astor_bus.db (per-plan
line 624-636), but that table is per-tier — this separate audit DB is the
cross-tier audit aggregator first_admin can query across all 9 db files.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from . import acl_layout  # R-class: dynamic lookup so monkeypatch can redirect
# Note: get_audit_path is called as acl_layout.get_audit_path() at runtime

AUDIT_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    actor TEXT NOT NULL,                  -- 'first_admin' | 'admin:<id>' | 'user:<id>' | 'system' | 'unknown' | 'server:<peer_id>'
    tier TEXT NOT NULL
        CHECK(tier IN ('public', 'source', 'private')),
    user_id TEXT,                          -- target user_id (the data being touched), may differ from actor
    action TEXT NOT NULL,                 -- 'read' | 'write' | 'delete' | 'compact' | 'migrate' | 'admin_op' | 'recall' | 'init' | 'peer_*'
    target TEXT,                           -- free-form: 'memory_canonical/id=42' | 'astor_bus_alice.db/embeddings' etc.
    reason TEXT,                           -- required for first_admin admin_op; optional otherwise
    metadata TEXT NOT NULL DEFAULT '{}',  -- JSON blob for op-specific extras
    -- v1.15.25 (2026-09-28) Ship I: peer_id denormalized column for fast
    -- per-peer audit feed. When actor starts with 'server:', the peer_id is
    -- the actor suffix (e.g. actor='server:astor:abc...', peer_id='astor:abc...').
    peer_id TEXT                           -- optional, set when action affects a specific peer
);

CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit(actor, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_target_user ON audit(user_id, ts DESC) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_tier_ts ON audit(tier, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_admin_op ON audit(action, reason) WHERE action = 'admin_op';
CREATE INDEX IF NOT EXISTS idx_audit_peer_id ON audit(peer_id, ts DESC) WHERE peer_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_action_ts ON audit(action, ts DESC);
"""

_lock = threading.Lock()
_conn = None  # module-level global; reset by tests via _reset_audit_conn()
_conn: sqlite3.Connection | None = None


def _reset_audit_conn() -> None:
    """R-class: tests need to clear the singleton so the next astor_audit() call
    re-resolves the (possibly monkeypatched) audit db path.
    """
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
        _conn = None


def _get_audit_conn() -> sqlite3.Connection:
    """Lazy connect. Mode 0600 on the file. Idempotent."""
    global _conn
    if _conn is not None:
        return _conn

    path = acl_layout.get_audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use sqlite3 with check_same_thread=False because audit_logger may be
    # called from any thread (Flask server)
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    # v1.15.25 Ship I: detect old schema (no peer_id column, has CHECK on action)
    # and migrate in-place by rebuilding the table.
    _migrate_audit_schema_v1525(conn)
    conn.executescript(AUDIT_DB_SCHEMA)
    conn.commit()
    try:
        os.chmod(path, 0o600)
    except (OSError, PermissionError):
        # Windows or sandboxed FS — chmod may not work, silent skip
        pass
    _conn = conn
    return conn


def astor_audit(
    actor: str,
    tier: str,
    action: str,
    user_id: str | None = None,
    target: str | None = None,
    reason: str | None = None,
    metadata: dict | None = None,
    peer_id: str | None = None,  # v1.15.25 Ship I: optional explicit peer_id
) -> None:
    """
    Write one audit row. Safe to call from any thread.

    Args:
        actor:   who is performing the action. Use one of:
                 'first_admin' / 'admin:<id>' / 'user:<id>' / 'system' / 'unknown'
        tier:    which tier the action targets (public/source/private)
        action:  one of 'read' | 'write' | 'delete' | 'compact' | 'migrate' |
                 'admin_op' | 'recall' | 'init'
        user_id: whose data is being touched (the target user's id, may differ
                 from actor for first_admin ops)
        target:  free-form identifier for what was touched (table/row/db path)
        reason:  required when action='admin_op' (or first_admin escalation),
                 optional otherwise
        metadata: extra dict for op-specific fields (compacted N rows, etc.)
    """
    if action == "admin_op" and not reason:
        raise ValueError(
            "astor_audit: action='admin_op' requires reason "
            "(audit escalation policy)"
        )
    md_json = json.dumps(metadata or {}, sort_keys=True)
    with _lock:
        conn = _get_audit_conn()
        conn.execute(
            """INSERT INTO audit
               (actor, tier, user_id, action, target, reason, metadata, peer_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (actor, tier, user_id, action, target, reason, md_json,
             peer_id or astor_set_peer_id(actor)),
        )


@contextmanager
def astor_audit_context(
    actor: str,
    tier: str,
    action: str,
    user_id: str | None = None,
    target: str | None = None,
    reason: str | None = None,
    metadata: dict | None = None,
):
    """Context manager variant for try/except bookkeeping."""
    start = datetime.utcnow().isoformat()
    exc_info = None
    try:
        yield
    except Exception as e:
        exc_info = repr(e)
        raise
    finally:
        md = dict(metadata or {})
        md["start"] = start
        if exc_info:
            md["exception"] = exc_info
        astor_audit(
            actor=actor, tier=tier, action=action,
            user_id=user_id, target=target, reason=reason, metadata=md,
        )


def astor_query_audit(
    actor: str | None = None,
    user_id: str | None = None,
    tier: str | None = None,
    action: str | None = None,
    since_ts: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """
    Read audit rows. Available only to first_admin CLI
    (`am admin audit-log`); not exposed in the public API.

    Returns list of dicts (row -> dict).
    """
    conn = _get_audit_conn()
    where, params = [], []
    if actor:
        where.append("actor = ?")
        params.append(actor)
    if user_id:
        where.append("user_id = ?")
        params.append(user_id)
    if tier:
        where.append("tier = ?")
        params.append(tier)
    if action:
        where.append("action = ?")
        params.append(action)
    if since_ts:
        where.append("ts >= ?")
        params.append(since_ts)
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    sql = (
        f"SELECT id, ts, actor, tier, user_id, action, target, reason, metadata"
        f" FROM audit{where_sql} ORDER BY ts DESC LIMIT ?"
    )
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = {
            "id": r[0], "ts": r[1], "actor": r[2], "tier": r[3],
            "user_id": r[4], "action": r[5], "target": r[6],
            "reason": r[7],
            "metadata": json.loads(r[8] or "{}"),
        }
        out.append(d)
    return out


def astor_close_audit() -> None:
    """Close the audit logger connection (test cleanup)."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


__all__ = [
    "astor_audit", "astor_audit_context",
    "astor_query_audit", "astor_close_audit",
]


def _migrate_audit_schema_v1525(conn: sqlite3.Connection) -> None:
    """v1.15.25 (2026-09-28) Ship I: rebuild audit table without CHECK on
    action, with new peer_id column.

    Idempotent. Only fires when the existing table is missing peer_id. Preserves
    all existing rows. SQLite cannot ALTER CONSTRAINTS or ADD CONSTRAINTS, so
    the table is rebuilt via the standard 12-step pattern: rename, create new,
    copy, drop old, rename new. The new table gets the v1.15.25 schema (which
    is the AUDIT_DB_SCHEMA constant).
    """
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='audit'"
    )
    if cur.fetchone() is None:
        return  # first run, no audit table yet — let schema create it
    cur = conn.execute("PRAGMA table_info(audit)")
    cols = {row[1] for row in cur.fetchall()}
    if "peer_id" in cols:
        return  # already migrated
    # Old schema detected. Rebuild.
    # We use a hard-coded OLD schema in case the AUDIT_DB_SCHEMA constant
    # is later updated and we're migrating an even older database.
    # v1.15.25 Ship I: rebuild via standard SQLite 12-step pattern.
    # Note: audit_logger uses isolation_level=None (autocommit), so each
    # statement is its own transaction. The DDL rename + CREATE + COPY +
    # DROP are atomic individually; if any step fails, the old table is
    # still intact and a future _get_audit_conn() call will retry.
    try:
        conn.execute("ALTER TABLE audit RENAME TO audit__v1_14_old")
        conn.executescript(AUDIT_DB_SCHEMA)
        # Copy all rows from old to new (peer_id stays NULL for legacy rows)
        conn.execute("""
            INSERT INTO audit (id, ts, actor, tier, user_id, action, target, reason, metadata)
            SELECT id, ts, actor, tier, user_id, action, target, reason, metadata
            FROM audit__v1_14_old
        """)
        conn.execute("DROP TABLE audit__v1_14_old")
    except Exception:
        # If anything failed, try to restore the original table so the
        # server can still start. Re-rename back if our new table exists.
        try:
            if conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='audit__v1_14_old'"
            ).fetchone() is not None and conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='audit'"
            ).fetchone() is not None:
                # We have both — the new audit (probably empty) is suspect.
                # Drop the new and rename old back.
                conn.execute("DROP TABLE audit")
                conn.execute("ALTER TABLE audit__v1_14_old RENAME TO audit")
        except Exception:
            pass
        raise


def astor_query_peer_audit(
    peer_id: str,
    *,
    action: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """v1.15.25 (2026-09-28) Ship I: per-peer audit feed.

    Returns up to `limit` most recent audit rows where peer_id matches,
    optionally filtered by action prefix (e.g. action='peer_recall' or
    action='peer_*') and a time window.

    Args:
        peer_id: exact peer_id to filter (e.g. 'astor:abc123...').
        action: optional action filter (exact match or LIKE 'peer_%').
        since: optional ISO timestamp (e.g. '2026-09-28T00:00:00Z').
        until: optional ISO timestamp upper bound.
        limit: max rows to return, capped at 200.

    Returns: list of dicts with keys id, ts, actor, tier, action, target,
    reason, metadata (parsed JSON), peer_id. Ordered by ts DESC.
    """
    _limit_in = int(limit) if limit is not None else 50
    limit = max(1, min(_limit_in, 200))
    clauses = ["peer_id = ?"]
    args: list = [peer_id]
    if action:
        if "%" in action or "_" in action:
            clauses.append("action LIKE ?")
            args.append(action)
        else:
            clauses.append("action = ?")
            args.append(action)
    if since:
        clauses.append("ts >= ?")
        args.append(since)
    if until:
        clauses.append("ts <= ?")
        args.append(until)
    where = " AND ".join(clauses)
    sql = (
        f"SELECT id, ts, actor, tier, user_id, action, target, reason, metadata, peer_id "
        f"FROM audit WHERE {where} ORDER BY ts DESC LIMIT ?"
    )
    args.append(limit)
    conn = _get_audit_conn()
    rows = conn.execute(sql, args).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            md = json.loads(r[8]) if r[8] else {}
        except Exception:
            md = {"_raw": r[8]}
        out.append({
            "id": int(r[0]),
            "ts": str(r[1] or ""),
            "actor": str(r[2] or ""),
            "tier": str(r[3] or ""),
            "user_id": r[4],
            "action": str(r[5] or ""),
            "target": r[6],
            "reason": r[7],
            "metadata": md,
            "peer_id": r[9],
        })
    return out


def astor_set_peer_id(actor: str) -> str:
    """v1.15.25 Ship I: derive peer_id from actor string.

    Convention: actor='server:<peer_id>' → returns '<peer_id>'.
    Other actor formats → returns None.
    """
    if not actor:
        return None
    if actor.startswith("server:"):
        rest = actor[len("server:"):]
        # If the rest looks like a peer_id (starts with 'astor:' and is 38 chars),
        # return it; otherwise return None.
        if rest.startswith("astor:") and len(rest) == 38:
            return rest
    return None
