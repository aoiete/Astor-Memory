"""Bot binding DB API — replaces install-state.json platform_bindings + wechat_bots.db.

Tables in bot-binding.db:
  - platforms: per-bot (per platform_kind + account_id) config + token
  - bindings:  chat_id <- platform; user_id <- binding target
  - user_meta: human-readable info per user

Single env: ASTOR_DIR (path to astor root). bot-binding.db = $ASTOR_DIR/bot-binding.db.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .acl_layout import get_astor_dir

SCHEMA_VERSION = 1

# Singleton (per-process)
_con: sqlite3.Connection | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _audit(action: str, target: str, metadata: dict | None = None, reason: str | None = None) -> None:
    """Write audit row for any state-mutating ops in this module.

    Tier='private' because user_meta stores real names + roles (PII).
    Action='admin_op' because binding changes escalate (revoking a binding
    immediately cuts off a user — that's an admin-level op).
    """
    # Lazy import to avoid circular issues at module load
    from .audit_logger import astor_audit
    try:
        astor_audit(
            actor="first_admin",
            tier="private",
            action="admin_op",
            target=target,
            reason=reason or "bot-binding state change",
            metadata=metadata or {},
        )
    except Exception:
        # Never let audit failure block the actual op. best-effort.
        pass


def _db_path() -> Path:
    return get_astor_dir() / "bot-binding.db"


def _connect() -> sqlite3.Connection:
    """Get or create the bot-binding.db connection (with schema init)."""
    global _con
    if _con is not None:
        return _con
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    _init_schema(con)
    _con = con
    return con


def _init_schema(con: sqlite3.Connection) -> None:
    cur = con.cursor()
    cur.executescript(f"""
        CREATE TABLE IF NOT EXISTS _schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS platforms (
            platform_id        TEXT PRIMARY KEY,
            platform_kind      TEXT NOT NULL,
            account_id         TEXT NOT NULL UNIQUE,
            account_token      TEXT NOT NULL,
            base_url           TEXT,
            enabled            INTEGER NOT NULL DEFAULT 1,
            notes              TEXT,
            created_at         TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at         TEXT NOT NULL DEFAULT (datetime('now')),
            source             TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_platforms_kind ON platforms(platform_kind);

        CREATE TABLE IF NOT EXISTS user_meta (
            user_id          TEXT PRIMARY KEY,
            short_alias      TEXT UNIQUE NOT NULL,
            display_name     TEXT,
            real_name        TEXT,
            role             TEXT NOT NULL DEFAULT 'user',
            subscription_plan TEXT DEFAULT 'trial',
            timezone         TEXT,
            tz_offset_hours  INTEGER,
            verified_by      TEXT,
            verified_at      TEXT,
            notes            TEXT,
            active           INTEGER NOT NULL DEFAULT 1,
            extra_json       TEXT,
            created_at       TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
            source           TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_user_meta_alias ON user_meta(short_alias);

        CREATE TABLE IF NOT EXISTS bindings (
            binding_id        TEXT PRIMARY KEY,
            platform_id       TEXT NOT NULL,
            chat_id           TEXT NOT NULL,
            user_id           TEXT NOT NULL,
            scope             TEXT NOT NULL DEFAULT 'single',
            role_inherit      TEXT NOT NULL DEFAULT 'user',
            allow_from        TEXT,
            active            INTEGER NOT NULL DEFAULT 1,
            bound_at          TEXT NOT NULL DEFAULT (datetime('now')),
            revoked_at        TEXT,
            revoked_by        TEXT,
            bound_by          TEXT NOT NULL,
            notes             TEXT,
            FOREIGN KEY (platform_id) REFERENCES platforms(platform_id),
            FOREIGN KEY (user_id) REFERENCES user_meta(user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_bindings_user ON bindings(user_id);
        CREATE INDEX IF NOT EXISTS idx_bindings_active ON bindings(active);
    """)
    cur.execute("INSERT OR IGNORE INTO _schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    con.commit()


# ============================================================
# platforms — read/write
# ============================================================

def list_platforms(enabled_only: bool = False) -> list[dict]:
    con = _connect()
    sql = "SELECT * FROM platforms"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY platform_kind, account_id"
    return [dict(r) for r in con.execute(sql).fetchall()]


def get_platform(account_id_or_platform_id: str) -> dict | None:
    con = _connect()
    row = con.execute(
        "SELECT * FROM platforms WHERE account_id = ? OR platform_id = ?",
        (account_id_or_platform_id, account_id_or_platform_id),
    ).fetchone()
    return dict(row) if row else None


def upsert_platform(
    platform_kind: str,
    account_id: str,
    account_token: str,
    base_url: str | None = None,
    enabled: bool = True,
    notes: str | None = None,
    source: str = "manual",
) -> str:
    """Insert or update a platform row. Returns platform_id."""
    con = _connect()
    platform_id = f"{platform_kind}:{account_id}"
    now = _now_iso()
    con.execute("""
        INSERT INTO platforms (platform_id, platform_kind, account_id, account_token,
                               base_url, enabled, notes, created_at, updated_at, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (platform_id) DO UPDATE SET
            account_token = excluded.account_token,
            base_url = excluded.base_url,
            enabled = excluded.enabled,
            notes = excluded.notes,
            updated_at = excluded.updated_at,
            source = excluded.source
    """, (platform_id, platform_kind, account_id, account_token, base_url,
          int(enabled), notes, now, now, source))
    con.commit()
    _audit(
        action="admin_op",
        target=f"platforms/{platform_id}",
        metadata={"kind": platform_kind, "enabled": enabled, "source": source},
        reason=f"upsert platform {platform_id}",
    )
    return platform_id


def set_platform_enabled(platform_id: str, enabled: bool) -> None:
    con = _connect()
    con.execute("UPDATE platforms SET enabled = ?, updated_at = ? WHERE platform_id = ?",
                (int(enabled), _now_iso(), platform_id))
    con.commit()
    _audit(
        action="admin_op",
        target=f"platforms/{platform_id}",
        metadata={"enabled": enabled},
        reason=f"{'enable' if enabled else 'disable'} platform {platform_id}",
    )


# ============================================================
# user_meta — read/write
# ============================================================

def list_users(active_only: bool = True) -> list[dict]:
    con = _connect()
    sql = "SELECT * FROM user_meta"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY user_id"
    return [dict(r) for r in con.execute(sql).fetchall()]


def get_user(user_id_or_alias: str) -> dict | None:
    con = _connect()
    row = con.execute(
        "SELECT * FROM user_meta WHERE user_id = ? OR short_alias = ?",
        (user_id_or_alias, user_id_or_alias),
    ).fetchone()
    return dict(row) if row else None


def upsert_user(
    user_id: str,
    short_alias: str,
    real_name: str | None = None,
    display_name: str | None = None,
    role: str = "user",
    subscription_plan: str = "trial",
    notes: str | None = None,
    active: bool = True,
    source: str = "manual",
) -> None:
    con = _connect()
    now = _now_iso()
    con.execute("""
        INSERT INTO user_meta (user_id, short_alias, display_name, real_name, role,
                               subscription_plan, notes, active, created_at, updated_at, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (user_id) DO UPDATE SET
            short_alias = excluded.short_alias,
            display_name = excluded.display_name,
            real_name = excluded.real_name,
            role = excluded.role,
            subscription_plan = excluded.subscription_plan,
            notes = excluded.notes,
            active = excluded.active,
            updated_at = excluded.updated_at,
            source = excluded.source
    """, (user_id, short_alias, display_name, real_name, role,
          subscription_plan, notes, int(active), now, now, source))
    con.commit()
    _audit(
        action="admin_op",
        target=f"user_meta/{user_id}",
        metadata={"short_alias": short_alias, "role": role, "source": source},
        reason=f"upsert user {user_id} (alias={short_alias})",
    )


# ============================================================
# bindings — read/write
# ============================================================

def list_bindings(user_id: str | None = None, active_only: bool = True) -> list[dict]:
    """List all bindings, optionally filtered by user. Joins platforms + user_meta."""
    con = _connect()
    sql = """
        SELECT b.*, p.platform_kind, p.account_id,
               u.short_alias, u.real_name, u.role as user_role
        FROM bindings b
        JOIN platforms p ON b.platform_id = p.platform_id
        JOIN user_meta u ON b.user_id = u.user_id
    """
    args: tuple = ()
    if user_id:
        sql += " WHERE b.user_id = ?"
        args = (user_id,)
        if active_only:
            sql += " AND b.active = 1"
    elif active_only:
        sql += " WHERE b.active = 1"
    sql += " ORDER BY p.platform_kind, b.chat_id"
    rows = con.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def resolve_chat_to_user(platform_id: str, chat_id: str) -> dict | None:
    """For incoming messages: lookup which user the chat_id belongs to."""
    con = _connect()
    row = con.execute("""
        SELECT b.*, u.role as user_role, u.subscription_plan
        FROM bindings b
        JOIN user_meta u ON b.user_id = u.user_id
        WHERE b.platform_id = ? AND b.chat_id = ? AND b.active = 1
        LIMIT 1
    """, (platform_id, chat_id)).fetchone()
    return dict(row) if row else None


def upsert_binding(
    platform_id: str,
    chat_id: str,
    user_id: str,
    scope: str = "single",
    allow_from: str | None = None,
    bound_by: str = "first_admin",
    notes: str | None = None,
) -> str:
    """Bind a chat_id to a user. Returns binding_id.

    If a binding already exists for this platform+chat, REVOKE the old one and create new.
    """
    con = _connect()
    # Look up user for role_inherit
    user = get_user(user_id)
    if user is None:
        raise ValueError(f"User {user_id} not found in user_meta")
    role_inherit = user.get("role") or "user"

    # Revoke any existing binding
    now = _now_iso()
    con.execute("""
        UPDATE bindings SET active = 0, revoked_at = ?, revoked_by = ?
        WHERE platform_id = ? AND chat_id = ? AND active = 1
    """, (now, bound_by, platform_id, chat_id))

    binding_id = str(uuid.uuid4())
    con.execute("""
        INSERT INTO bindings (binding_id, platform_id, chat_id, user_id, scope,
                              role_inherit, allow_from, active, bound_at, bound_by, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
    """, (binding_id, platform_id, chat_id, user_id, scope, role_inherit,
          allow_from, now, bound_by, notes))
    con.commit()
    _audit(
        action="admin_op",
        target=f"bindings/{binding_id}",
        metadata={"platform_id": platform_id, "chat_id": chat_id,
                  "user_id": user_id, "scope": scope, "allow_from": allow_from,
                  "bound_by": bound_by},
        reason=f"bind {platform_id}:{chat_id} -> {user_id}",
    )
    return binding_id


def revoke_binding(binding_id: str, revoked_by: str = "first_admin") -> None:
    con = _connect()
    con.execute("""
        UPDATE bindings SET active = 0, revoked_at = ?, revoked_by = ?
        WHERE binding_id = ?
    """, (_now_iso(), revoked_by, binding_id))
    con.commit()
    _audit(
        action="admin_op",
        target=f"bindings/{binding_id}",
        metadata={"revoked_by": revoked_by},
        reason=f"revoke binding {binding_id}",
    )


# ============================================================
# CLI helpers
# ============================================================

def close() -> None:
    global _con
    if _con is not None:
        _con.close()
        _con = None
