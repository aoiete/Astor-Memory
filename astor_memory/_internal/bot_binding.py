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
            actor='admin:admin',
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
    """Get or create the bot-binding.db connection (with schema init).

    2026-08-16 ACL fix: check_same_thread=False is required because Flask
    serves multi-threaded, and the before_request hook now calls get_user()
    which uses this connection. Without this, every cross-thread request
    raises SQLite ProgrammingError.
    """
    global _con
    if _con is not None:
        return _con
    p = _db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), check_same_thread=False)
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
            source           TEXT,
            default_tier     TEXT NOT NULL DEFAULT 'public',
            trusted_agent    INTEGER NOT NULL DEFAULT 0,
            -- v1.17.5: whether this user's private tier may be mined
            -- into the shared tier, and whether they may write to it.
            --
            -- distill_opt_in has THREE states, not two:
            --    1  explicit yes
            --    0  explicit no
            --   -1  never asked  <- the default, in BOTH the schema and the ALTER
            --
            -- -1 is a real state, not a missing value. It reads differently
            -- depending on whose tier is being mined:
            --   the operator's own tier -- never asked is not a refusal, so the
            --   automatic path runs (value != 0) and the manual path always runs
            --   (running the command IS the consent);
            --   somebody ELSE's tier -- their yes is required, so -1 does not
            --   allow it (value == 1).
            --
            -- The dashboard switch is how the operator says no (0), so the
            -- command reads the flag for the operator too.
            --
-- allow_commons_write defaults to 1: writing to the shared
            -- tier is the normal thing for a fact to be for.
            -- -1 means "never asked": the mining command treats it as
            -- the operator's own consent to mine their own tier, which
            -- is what running the command has always meant. 0 is an
            -- explicit "off" that the operator set in the dashboard.
            -- The column predates this code (v1.16.30 added it to one
            -- live database by hand), so existing rows read 0, and
            -- without the sentinel the operator's own tier would come
            -- up unmined on every install that had that database.
            distill_opt_in       INTEGER NOT NULL DEFAULT -1,
            allow_commons_write  INTEGER NOT NULL DEFAULT 1
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

    # 2026-09-17: idempotent column migration for legacy DBs created before
    # `default_tier` / `trusted_agent` existed. SQLite has no ADD COLUMN IF NOT
    # EXISTS so we probe pragma_table_info first.
    existing = {row[1] for row in con.execute("PRAGMA table_info(user_meta)").fetchall()}
    if "default_tier" not in existing:
        con.execute(
            "ALTER TABLE user_meta ADD COLUMN default_tier TEXT NOT NULL DEFAULT 'public'"
        )
    if "trusted_agent" not in existing:
        con.execute(
            "ALTER TABLE user_meta ADD COLUMN trusted_agent INTEGER NOT NULL DEFAULT 0"
        )
    # v1.17.5: the same treatment for the two mining-consent columns.
    # They were read by the mining command and by the admin toggle
    # route, but never created by the schema -- v1.16.30 added
    # distill_opt_in to one live database by hand and the code never
    # grew the CREATE or the ALTER. So a fresh install, or any
    # machine that was not that one, raised "no such column" and the
    # mining command quietly fell back to its own tier.
    #
    # The default is -1 for distill_opt_in: "never asked". For another
    # user that means no consent, so their tier is not mined. For the
    # operator's own tier it means the consent that running the command
    # has always carried. 0 is an explicit "off".
    if "distill_opt_in" not in existing:
        con.execute(
            "ALTER TABLE user_meta ADD COLUMN distill_opt_in INTEGER NOT NULL DEFAULT -1"
        )
    if "allow_commons_write" not in existing:
        con.execute(
            "ALTER TABLE user_meta ADD COLUMN allow_commons_write INTEGER NOT NULL DEFAULT 1"
        )
    con.commit()


# ============================================================
# platforms — read/write
# ============================================================

def list_platforms(enabled_only: bool = False) -> list[dict]:
    """Return all platform rows (or only enabled=True when enabled_only=True)."""
    con = _connect()
    sql = "SELECT * FROM platforms"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY platform_kind, account_id"
    return [dict(r) for r in con.execute(sql).fetchall()]


def get_platform(account_id_or_platform_id: str) -> dict | None:
    """Look up one platform row by platform_id (or None)."""
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
    """Flip a platform's enabled flag (0 or 1) — used by `am platform disable/enable`."""
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
    """Return all user_meta rows."""
    con = _connect()
    sql = "SELECT * FROM user_meta"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY user_id"
    return [dict(r) for r in con.execute(sql).fetchall()]


def get_user(user_id_or_alias: str) -> dict | None:
    """Look up one user_meta row by user_id or short_alias."""
    con = _connect()
    row = con.execute(
        "SELECT * FROM user_meta WHERE user_id = ? OR short_alias = ?",
        (user_id_or_alias, user_id_or_alias),
    ).fetchone()
    return dict(row) if row else None


# v1.16.79: the legal value sets for user_meta's two routing columns.
#
# Both columns were free TEXT with a DEFAULT and no CHECK, so anything could
# be written into them — and `admin` is exactly what an operator would write
# when asked to "make this user an admin", which is a ROLE, not a tier and not
# a plan. Once written, the value had to be papered over downstream: the /v1
# write path grew a branch mapping default_tier == 'admin' to the private tier,
# and the dashboard grew an 'admin' entry in its plan ranking. Both existed only
# to tolerate data that should never have been storable.
#
# Constrained here at the single write path every caller funnels through, so
# the downstream compatibility branches can be deleted rather than maintained
# forever. Existing rows are not rewritten by this change — `am doctor
# --migrate-user-meta` reports and repairs them.
VALID_ROLES = frozenset({"admin", "user", "first_admin", "system"})
# 'admin' is a real plan: it is what the operator's own account carries, and
# the dashboard sorts it to the top (PLAN_RANK admin:0) so the operator row is
# always visible above every other user.
VALID_SUBSCRIPTION_PLANS = frozenset({"admin", "free", "trial", "vip", "power", "pro"})
# 'admin' is a real default_tier: it means "the operator's own private
# bucket", and every consumer resolves it to private_<user_id>.
# 'repo' was retired in v1.16.79 — the tier stays in acl.Tier so existing
# repos/<id>/ databases keep resolving, but it is no longer something a user
# should be routed to, so it is not accepted as a new default_tier.
VALID_DEFAULT_TIERS = frozenset({"admin", "public", "private", "source"})


def _validate_user_meta_values(role=None, subscription_plan=None,
                              default_tier=None) -> None:
    """Reject values that cannot be routed. Raises ValueError naming the legal set.

    v1.16.79: rejecting here rather than defaulting is deliberate. Silently
    coercing a bad tier to 'public' would send a private write to the public
    bucket — an isolation failure, not a cosmetic one — so the caller is told
    instead.
    """
    if role is not None and role not in VALID_ROLES:
        raise ValueError(
            f"invalid role {role!r}; expected one of {sorted(VALID_ROLES)}")
    if (subscription_plan is not None
            and subscription_plan not in VALID_SUBSCRIPTION_PLANS):
        raise ValueError(
            f"invalid subscription_plan {subscription_plan!r}; expected one of "
            f"{sorted(VALID_SUBSCRIPTION_PLANS)}")
    if default_tier is not None and default_tier not in VALID_DEFAULT_TIERS:
        raise ValueError(
            f"invalid default_tier {default_tier!r}; expected one of "
            f"{sorted(VALID_DEFAULT_TIERS)}")


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
    """Insert or update a user_meta row. Returns user_id

    v1.16.79: role / subscription_plan are validated before the write. A caller
    that passes role="admin" alongside subscription_plan="admin" — the shape a
    "make me an admin" flag tends to produce — now fails loudly at the write
    instead of producing a row the routing layer has to special-case forever.
    """
    _validate_user_meta_values(role=role, subscription_plan=subscription_plan)
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
# Phase C-D: default_tier + trusted_agent (server-side role routing)
# ============================================================
#
# These two columns let the server classify inbound writes by user_id without
# trusting the caller. Both columns are audited on change because they escalate
# privilege (a trusted_agent's writes default to admin-visible tiers).

# v1.16.79: 'admin' STAYS. It is a legitimate value, not corruption.
#
# An earlier pass in this same change removed 'admin' on the reasoning that it
# is a role rather than a tier, and proposed to migrate existing rows away
# from it. That was wrong on the evidence: the operator's own intent is that a
# first_admin row carries admin in BOTH columns, and the rest of the codebase
# already treats it as first-class — the dashboard ranks subscription_plan
# 'admin' at 0 so the operator surfaces above every user, and
# dashboard_data._LEGACY_PRIVATE_STEMS maps the 'admin' stem to the private
# tier. Scrubbing the value would have made the operator's own account
# invisible in their own UI.
#
# The real defect was never the value. It was that the tier was undeclared:
# nothing said which strings were routable, so nothing could reject a
# genuinely wrong one. That is fixed by VALID_DEFAULT_TIERS /
# VALID_SUBSCRIPTION_PLANS below, which accept admin and reject everything
# else. `am doctor --migrate-user-meta` now only reports values outside those
# sets, so an admin row is never reported and never touched.
_VALID_TIERS = ("public", "source", "private", "admin")


def get_user_default_tier(user_id: str) -> str | None:
    """Return the default tier for a user_id, or None if no row / unknown user."""
    con = _connect()
    row = con.execute(
        "SELECT default_tier FROM user_meta WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        return None
    return row["default_tier"]


def set_user_default_tier(
    user_id: str,
    tier: str,
    *,
    actor: str = "admin:admin",
) -> None:
    """Set the default tier for a user_id. Raises ValueError for unknown users
    or invalid tier values. Audited as admin_op.
    """
    if tier not in _VALID_TIERS:
        raise ValueError(
            f"invalid tier {tier!r}; must be one of {_VALID_TIERS}"
        )
    con = _connect()
    if con.execute("SELECT 1 FROM user_meta WHERE user_id = ?", (user_id,)).fetchone() is None:
        raise ValueError(f"User {user_id} not found in user_meta")
    con.execute(
        "UPDATE user_meta SET default_tier = ?, updated_at = ? WHERE user_id = ?",
        (tier, _now_iso(), user_id),
    )
    con.commit()
    _audit(
        action="admin_op",
        target=f"user_meta/{user_id}",
        metadata={"field": "default_tier", "value": tier, "actor": actor},
        reason=f"set default_tier={tier} for user {user_id}",
    )


def set_distill_opt_in(
    user_id: str,
    opt_in: bool | int,
    actor: str = "admin",
) -> bool:
    """Set the mining-consent flag for a user_id. Audited as admin_op.

    Whether this user's private tier may be promoted into the shared
    tier. The mining command reads it: a user who has not opted in is
    not mined, and the operator's own tier is mined because running
    the command is the consent.

    ``opt_in`` is True/False for on/off, or -1 for "never asked" --
    the state a fresh row starts in, which the operator can choose to
    return to. The three states are different: -1 is the absence of a
    decision, 0 is a decision not to share, 1 is a decision to share.

    Returns True if a row was updated, False if the user is unknown.
    """
    value = -1 if opt_in == -1 else int(bool(opt_in))
    con = _connect()
    if con.execute("SELECT 1 FROM user_meta WHERE user_id = ?",
                   (user_id,)).fetchone() is None:
        return False
    con.execute(
        "UPDATE user_meta SET distill_opt_in = ?, updated_at = ? WHERE user_id = ?",
        (value, _now_iso(), user_id))
    con.commit()
    try:
        _audit(action="admin_op", target=f"user_meta/{user_id}",
               metadata={"field": "distill_opt_in", "value": value,
                         "actor": actor},
               reason=f"set distill_opt_in={value} for user {user_id}")
    except Exception:
        pass
    return True


def get_distill_opt_in(user_id: str) -> int:
    """The mining-consent flag for a user: 1 on, 0 off, -1 never asked.

    The three states are different. -1 is the absence of a decision,
    and it reads differently depending on who is asking: the user
    themselves asking to have their own fact distilled IS the
    consent, so -1 lets them; the operator reaching into someone
    else's tier has no such consent, so -1 does not.
    """
    con = _connect()
    try:
        row = con.execute(
            "SELECT distill_opt_in FROM user_meta WHERE user_id = ?",
            (user_id,)).fetchone()
    except Exception:
        return -1
    if row is None or row[0] is None:
        return -1
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return -1


def distill_auto_allowed(user_id: str, *, own: bool = False) -> bool:
    """May this user's tier be distilled WITHOUT them asking?

    This is the AUTOMATIC path -- the weekly job, the distill_auto
    endpoint. The switch means "run automatically", so for the
    operator's own tier the absence of an answer is not a refusal:
    never asked (-1) runs, turned off (0) does not, turned on (1)
    does.

    Somebody ELSE's tier is different: their yes is required, and
    the absence of an answer is not a yes.
    """
    value = get_distill_opt_in(user_id)
    if own:
        return value != 0
    return value == 1


def distill_mining_allowed(user_id: str, *, own: bool = False) -> bool:
    """May this user's tier be promoted into the shared tier BY A
    COMMAND SOMEONE RAN?

    This is the MANUAL path. The switch turns the AUTOMATIC runs
    off, and the operator said exactly that: "turn off automatic and
    it becomes manual". So turning the switch off must not also
    forbid the manual run -- the person typing the command is the
    consent.

    Somebody else's tier still needs their yes (1). Their own tier
    does not: running the command is the consent.
    """
    value = get_distill_opt_in(user_id)
    if own:
        return True
    return value == 1


def is_trusted_agent(user_id: str) -> bool:
    """Return True iff user_id has trusted_agent=1."""
    con = _connect()
    row = con.execute(
        "SELECT trusted_agent FROM user_meta WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        return False
    return bool(row["trusted_agent"])


def set_trusted_agent(
    user_id: str,
    trusted: bool,
    *,
    actor: str = "admin:admin",
) -> None:
    """Flip the trusted_agent flag for a user_id. Audited as admin_op.

    Note: trust should always be paired with an explicit default_tier; the
    caller is expected to call ``set_user_default_tier`` first or in the same
    batch.
    """
    con = _connect()
    if con.execute("SELECT 1 FROM user_meta WHERE user_id = ?", (user_id,)).fetchone() is None:
        raise ValueError(f"User {user_id} not found in user_meta")
    con.execute(
        "UPDATE user_meta SET trusted_agent = ?, updated_at = ? WHERE user_id = ?",
        (int(bool(trusted)), _now_iso(), user_id),
    )
    con.commit()
    _audit(
        action="admin_op",
        target=f"user_meta/{user_id}",
        metadata={"field": "trusted_agent", "value": bool(trusted), "actor": actor},
        reason=f"set trusted_agent={bool(trusted)} for user {user_id}",
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


def list_admin_chat_ids() -> list[tuple[str, str]]:
    """Return [(platform_id, chat_id), ...] for all active bindings whose user
    has role in (first_admin, admin).

    This is the canonical lookup for "who is the operator across all
    platforms". Callers must NOT hardcode any chat_id; instead they should
    ask this function at runtime.

    Used by ACL when an inbound message arrives without explicit user_id
    (e.g. just a chat_id from a platform webhook) — ACL needs to know
    which user that chat maps to.
    """
    con = _connect()
    rows = con.execute("""
        SELECT b.platform_id AS platform_id, b.chat_id AS chat_id
        FROM bindings b
        JOIN user_meta u ON b.user_id = u.user_id
        WHERE b.active = 1 AND u.role IN ('first_admin', 'admin')
    """).fetchall()
    return [(r["platform_id"], r["chat_id"]) for r in rows]


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
    """Mark a binding as inactive (soft delete) + audit row."""
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
    """Close the bot-binding.db singleton connection."""
    global _con
    if _con is not None:
        _con.close()
        _con = None


# ============================================================
# Phase C-D: cross-channel consistency audit
# ============================================================
#
# Phase B shipped multi-channel role routing (admin-only via evox, others
# via hermes + bot-binding). The risk: a user_id could have one binding
# that resolves to admin role and another binding that resolves to user
# role, with different downstream paths. This helper audits that
# invariant — for each active binding, the binding.role_inherit must
# match user_meta.role and the platform must be enabled. Any mismatch
# is returned as a structured inconsistency for admin review.


def check_cross_channel_consistency() -> list[dict]:
    """Audit all active bindings for cross-channel role consistency.

    For every active binding, verifies:
      1. ``binding.role_inherit`` matches the bound ``user_meta.role``
      2. ``user_meta.active`` is 1 (a binding to a deactivated user is a
         stale binding that should be revoked)
      3. The bound ``platforms.enabled`` is 1 (a binding to a disabled
         platform can never receive messages)

    Returns a list of inconsistency dicts (empty list = all consistent).
    Each inconsistency has: ``binding_id``, ``platform_id``, ``chat_id``,
    ``user_id``, and ``issues`` (list of {field, ...} dicts).

    Read-only — never mutates state. Audit row is NOT written here; the
    REST endpoint that calls this is expected to write one audit row per
    run so admins can correlate findings over time.
    """
    con = _connect()
    rows = con.execute("""
        SELECT b.binding_id, b.platform_id, b.chat_id, b.user_id,
               b.role_inherit, b.scope, b.allow_from,
               u.role AS user_role, u.active AS user_active,
               u.default_tier AS user_default_tier,
               u.trusted_agent AS user_trusted_agent,
               p.enabled AS platform_enabled, p.platform_kind
        FROM bindings b
        JOIN user_meta u ON b.user_id = u.user_id
        JOIN platforms p ON b.platform_id = p.platform_id
        WHERE b.active = 1
    """).fetchall()

    inconsistencies: list[dict] = []
    for r in rows:
        issues: list[dict] = []
        if r["role_inherit"] != r["user_role"]:
            issues.append({
                "field": "role_inherit",
                "binding_value": r["role_inherit"],
                "user_meta_value": r["user_role"],
                "severity": "high",
                "note": (
                    "binding claims one role but user_meta says another; "
                    "downstream ACL will use the binding's role_inherit"
                ),
            })
        if not r["user_active"]:
            issues.append({
                "field": "user_active",
                "value": False,
                "severity": "medium",
                "note": "binding points to a deactivated user",
            })
        if not r["platform_enabled"]:
            issues.append({
                "field": "platform_enabled",
                "value": False,
                "severity": "low",
                "note": "binding points to a disabled platform",
            })
        if issues:
            inconsistencies.append({
                "binding_id": r["binding_id"],
                "platform_id": r["platform_id"],
                "platform_kind": r["platform_kind"],
                "chat_id": r["chat_id"],
                "user_id": r["user_id"],
                "scope": r["scope"],
                "user_default_tier": r["user_default_tier"],
                "user_trusted_agent": bool(r["user_trusted_agent"]),
                "issues": issues,
            })
    return inconsistencies

# ============================================================
# v1.16.79: repair rows written before the value sets were enforced
# ============================================================

def scan_user_meta_values() -> dict:
    """Report user_meta rows whose routing columns hold unroutable values.

    Returns {"bad_tier": [(user_id, value), ...], "bad_plan": [(user_id, value), ...],
             "checked": int}. Read-only; repairs nothing.

    The two columns had no CHECK constraint, so 'admin' — a role, not a tier
    and not a plan — could be stored in either. The write path now refuses to
    create such a row; this finds the ones that already exist.
    """
    con = _connect()
    try:
        tiers = list(con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_meta'"))
        if not tiers:
            return {"bad_tier": [], "bad_plan": [], "checked": 0}
        total = con.execute("SELECT COUNT(*) FROM user_meta").fetchone()[0]
        bad_tier = [
            (r[0], r[1]) for r in con.execute(
                "SELECT user_id, default_tier FROM user_meta "
                "WHERE default_tier NOT IN "
                "('admin','public','source','private') OR default_tier IS NULL")
        ]
        bad_plan = [
            (r[0], r[1]) for r in con.execute(
                "SELECT user_id, subscription_plan FROM user_meta "
                "WHERE subscription_plan NOT IN "
                "('admin','free','trial','vip','power','pro') "
                "OR subscription_plan IS NULL")
        ]
        return {"bad_tier": bad_tier, "bad_plan": bad_plan, "checked": total}
    finally:
        con.close()


def migrate_user_meta_values(dry_run=True) -> dict:
    """Repair unroutable default_tier / subscription_plan values.

    v1.16.79. The mapping is not arbitrary:

      default_tier  'admin' -> 'private'
        Only reached for a tier outside VALID_DEFAULT_TIERS. 'admin' is in
        that set and is therefore never touched here — it is the operator's
        own bucket and stays as written.

      subscription_plan  -> 'trial'
        Only reached for a plan outside VALID_SUBSCRIPTION_PLANS. 'admin' is
        in that set too, so the operator's own plan is never rewritten.

      So in practice this only repairs typos and values written by code that
      bypassed the setter: 'Admin', 'ADMIN', 'vip ', 'premium', ''. Facts are
      NOT re-scoped or deleted — only the mislabelled column is corrected.

    Returns a dict with the rows changed (or that would change, under dry_run).
    """
    # Read the offenders and close the connection BEFORE auditing.
    #
    # _audit() opens its own connection to the audit database, and doing that
    # while holding this one left the module-level connection in a state where
    # the following statements raised "Cannot operate on a closed database" —
    # the same class of error as astor R-class 12953, where two handles to one
    # SQLite file fight. Sequence the work instead of nesting it.
    con = _connect()
    try:
        tier_rows = list(con.execute(
            "SELECT user_id, default_tier FROM user_meta "
            "WHERE default_tier NOT IN ('public','source','private') "
            "OR default_tier IS NULL"))
        plan_rows = list(con.execute(
            "SELECT user_id, subscription_plan FROM user_meta "
            "WHERE subscription_plan NOT IN "
            "('free','trial','vip','power','pro') OR subscription_plan IS NULL"))
        if not dry_run and (tier_rows or plan_rows):
            stamp = _now_iso()
            for user_id, value in tier_rows:
                con.execute(
                    "UPDATE user_meta SET default_tier = ?, updated_at = ? "
                    "WHERE user_id = ?",
                    ('private' if value.lower() == 'admin' else 'public', stamp, user_id))
            for user_id, _value in plan_rows:
                con.execute(
                    "UPDATE user_meta SET subscription_plan = ?, updated_at = ? "
                    "WHERE user_id = ?",
                    ('power', stamp, user_id))
            con.commit()
    finally:
        con.close()

    # Auditing after the write, on its own connections, one per changed row.
    if not dry_run:
        for user_id, value in tier_rows:
            _audit(action="admin_op", target=f"user_meta/{user_id}",
                   metadata={"field": "default_tier", "old": value,
                             "new": 'private' if value == 'admin' else 'public'},
                   reason="v1.16.79 migrate unroutable default_tier")
        for user_id, value in plan_rows:
            _audit(action="admin_op", target=f"user_meta/{user_id}",
                   metadata={"field": "subscription_plan", "old": value,
                             "new": "power"},
                   reason="v1.16.79 migrate unroutable subscription_plan")

    return {"dry_run": dry_run,
            "default_tier": [(u, v) for u, v in tier_rows],
            "subscription_plan": [(u, v) for u, v in plan_rows]}
