"""
Grant system for cross-user private-tier access.

2026-08-16 ship (B option): user 授权 first_admin/admin 才能看 user private.
This is the strictest privacy model — even first_admin (the system root) must
obtain an explicit grant from the data owner before reading their private
tier. Grants are revocable, time-bounded, and audit-logged.

Storage:
- `~/.astor/audit/astor_grants.db` (mode 0600, separate file)
- One table `grants` — owner_id, grantee, scope, expires_at, revoked, created_at

Semantics:
- grantor = the data owner (a user_id). They grant access to their private DB.
- grantee = who receives the access. Either:
    - 'first_admin' (the canonical system root)
    - 'admin:<id>' (a named admin)
- scope = what is allowed:
    - 'read'        (read-only)
    - 'write'       (read + write; implies read)
    - 'admin'       (full moderation; implies write)
- expires_at = ISO timestamp; nullable means no expiry.
- revoked = 0/1. Revoked grants fail immediately.

Decision rule (used in astor_check_read/write):
    grant_active(owner_id, grantee, scope) returns True iff
    - grantee matches 'first_admin' / 'admin:<actor_id>' or owner_id itself
    - at least one non-revoked row exists for (owner_id, grantee)
    - that row's scope >= requested scope (admin > write > read)
    - expires_at is null OR in the future

Cross-tier grants (tier=source) remain first_admin-only. Grants ONLY apply
to tier=private. No grant can escalate a user to read source.

Audit:
- Every grant check (allow OR deny) writes one astor_audit row.
- Grant create/revoke also writes a row with action='admin_op'.

Lock: 2026-08-16.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .acl_layout import get_astor_dir

GRANT_DB_FILENAME = "astor_grants.db"

# Scope ordering — must follow plan § 2576-2589 matrix levels.
_SCOPE_ORDER = {"read": 1, "write": 2, "admin": 3}


def _grant_path() -> Path:
    p = get_astor_dir() / "audit" / GRANT_DB_FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


_SCHEMA = """
CREATE TABLE IF NOT EXISTS grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grantor TEXT NOT NULL,         -- owner user_id (the data owner)
    grantee TEXT NOT NULL,         -- 'first_admin' | 'admin:<id>' | 'user:<id>'
    scope TEXT NOT NULL,           -- 'read' | 'write' | 'admin'
    expires_at TEXT,               -- ISO 8601 UTC; NULL = no expiry
    revoked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    reason TEXT                      -- free text (grantee's stated purpose)
);

CREATE INDEX IF NOT EXISTS ix_grants_pair
    ON grants (grantor, grantee, revoked);

CREATE UNIQUE INDEX IF NOT EXISTS ux_grants_active
    ON grants (grantor, grantee, scope)
    WHERE revoked = 0;
"""


_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _conn_lazy() -> sqlite3.Connection:
    """Lazy connect to grants DB. Idempotent. Thread-safe."""
    global _conn
    if _conn is not None:
        return _conn
    path = _grant_path()
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(_SCHEMA)
    conn.commit()
    try:
        os.chmod(path, 0o600)
    except (OSError, PermissionError):
        pass  # Windows — chmod is informational
    _conn = conn
    return conn


# === Public API ===

def create_grant(
    grantor: str,
    grantee: str,
    scope: str,
    expires_at: str | None = None,
    reason: str | None = None,
) -> int:
    """
    Create a new grant. The caller must be the grantor themselves
    (or first_admin on their behalf during user onboarding).

    Args:
        grantor:    user_id of the data owner
        grantee:    'first_admin' | 'admin:<id>' | 'user:<id>'
        scope:      'read' | 'write' | 'admin'
        expires_at: ISO 8601 UTC string; None = no expiry
        reason:     free text reason (auditor-readable)

    Returns: grant id (int)

    Raises:
        ValueError: on invalid scope/grantee format
    """
    _validate_grantee(grantee)
    if scope not in _SCOPE_ORDER:
        raise ValueError(f"scope must be one of {sorted(_SCOPE_ORDER)}, got {scope!r}")
    if expires_at is not None:
        # Validate ISO 8601
        _parse_iso(expires_at)

    created = _now_iso()
    with _lock:
        conn = _conn_lazy()
        cur = conn.execute(
            """INSERT INTO grants (grantor, grantee, scope, expires_at,
                                   revoked, reason, created_at)
               VALUES (?, ?, ?, ?, 0, ?, ?)""",
            (grantor, grantee, scope, expires_at, reason, created),
        )
        grant_id = cur.lastrowid
    return grant_id


def revoke_grant(grant_id: int, by: str) -> bool:
    """
    Revoke a grant by id. Returns True if a row was updated, False otherwise.
    `by` is the grantor user_id (must match the grant's grantor) or
    'first_admin' (system override). Caller must enforce that auth.
    """
    with _lock:
        conn = _conn_lazy()
        cur = conn.execute(
            "UPDATE grants SET revoked = 1, revoked_at = ? WHERE id = ? AND revoked = 0",
            (_now_iso(), grant_id),
        )
        return cur.rowcount > 0


def list_grants(
    grantor: str | None = None,
    grantee: str | None = None,
    include_revoked: bool = False,
) -> list[dict]:
    """
    List grants, optionally filtered by grantor and/or grantee.
    By default only active (revoked=0, not expired) grants are returned.
    """
    where = []
    args: list = []
    if grantor is not None:
        where.append("grantor = ?")
        args.append(grantor)
    if grantee is not None:
        where.append("grantee = ?")
        args.append(grantee)
    if not include_revoked:
        where.append("revoked = 0")

    sql = "SELECT id, grantor, grantee, scope, expires_at, revoked, created_at, revoked_at, reason FROM grants"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"

    with _lock:
        conn = _conn_lazy()
        rows = conn.execute(sql, args).fetchall()

    results = []
    now = datetime.now(timezone.utc)
    for row in rows:
        gid, gtor, gtee, scope, exp, revoked, created, revoked_at, reason = row
        active = not revoked and _not_expired(exp, now)
        results.append({
            "id": gid,
            "grantor": gtor,
            "grantee": gtee,
            "scope": scope,
            "expires_at": exp,
            "revoked": bool(revoked),
            "active": active,
            "created_at": created,
            "revoked_at": revoked_at,
            "reason": reason,
        })
    return results


def check_grant(
    grantor: str,
    grantee: str,
    required_scope: str,
) -> bool:
    """
    Return True iff at least one active grant covers (grantor, grantee, >= scope).

    Args:
        grantor:        owner user_id whose data is being accessed
        grantee:        'first_admin' | 'admin:<id>' | 'user:<id>'
        required_scope: minimum scope needed ('read' | 'write' | 'admin')

    Logic:
        - owner is always granted to themselves
        - otherwise find a non-revoked, non-expired row for (grantor, grantee)
          whose scope >= required_scope
    """
    # Owner is implicit
    if grantee == f"user:{grantor}":
        return True

    _validate_grantee(grantee)
    required_level = _SCOPE_ORDER.get(required_scope, 0)
    now_iso = datetime.now(timezone.utc).isoformat()

    with _lock:
        conn = _conn_lazy()
        rows = conn.execute(
            """SELECT scope FROM grants
               WHERE grantor = ? AND grantee = ? AND revoked = 0
                 AND (expires_at IS NULL OR expires_at > ?)
               ORDER BY id DESC""",
            (grantor, grantee, now_iso),
        ).fetchall()

    for (scope,) in rows:
        if _SCOPE_ORDER.get(scope, 0) >= required_level:
            return True
    return False


# === Helpers ===

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(s: str) -> datetime:
    """Parse an ISO 8601 string; raise ValueError on bad format."""
    # Handle Z suffix
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _not_expired(expires_at: str | None, now: datetime) -> bool:
    if expires_at is None:
        return True
    try:
        return _parse_iso(expires_at) > now
    except ValueError:
        return False  # treat malformed as expired (fail closed)


def _validate_grantee(grantee: str) -> None:
    """Grantee must be one of: 'first_admin' | 'admin:<id>' | 'user:<id>'."""
    if grantee == "first_admin":
        return
    if grantee.startswith("admin:") or grantee.startswith("user:"):
        rest = grantee.split(":", 1)[1]
        if rest and all(c.isalnum() or c in "_-" for c in rest) and len(rest) <= 64:
            return
    raise ValueError(
        f"grantee must be 'first_admin' or 'admin:<id>' or 'user:<id>', got {grantee!r}"
    )


__all__ = [
    "create_grant",
    "revoke_grant",
    "list_grants",
    "check_grant",
]