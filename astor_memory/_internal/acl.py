"""
ACL enforcement: the central gatekeeper that ensures all data access follows
plan §3-tier × 3-store permission matrix (lines 2576-2607).

Three roles (per plan):
- first_admin: writes source.db, reads any private **with explicit grant from
  data owner (2026-08-16 strict-privacy ship)**, creates users, cannot be demoted.
- admin: power user; CANNOT read source.db (distinction from first_admin per
  plan line 2570 + 2624). Cross-user private access **requires explicit grant
  from data owner (2026-08-16 strict-privacy ship)**.
- user: can only access own private_<id>.db.

This module is process-level state. It enforces:
1. When astor_bus(actor, tier, user_id) is called, the actor must have permission
   to access (tier, user_id) per the matrix.
2. Write paths to private dbs are validated against the requested user_id.
3. The actor + user_id are recorded in audit rows automatically.

v1.2 hardening (2026-09-01):
- astor_init_acl validates every argument; bad input cannot be silently
  accepted. actor must match the canonical regex (rejects typos + smuggling).
  Role / actor must be consistent (rejects impersonation like
  actor='first_admin' role='user').
- tier='public' write is restricted to first_admin + admin so that a
  compromised 'user' or 'system' context cannot spam public.
- astor_check_read / astor_check_write raise PermissionError_ when
  user_id=None for tier=private or tier=repo. The previous "silent pass"
  was a footgun that hid caller mistakes.
- astor_init_acl re-init (changing actor within the same process) is
  audit-logged so silent privilege escalation cannot go unnoticed.

Lock: 2026-08-15 (turn design discussion); 2026-08-16 strict-privacy ship;
2026-09-01 v1.2 hardening.
"""

from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass
from typing import Literal

from . import grants
from .audit_logger import astor_audit

# v1.2 follow-up: contextvars support for async / per-task ACL isolation.
# threading.local isolates per-thread, but asyncio coroutines share a thread
# and need per-task isolation. We use a ContextVar as the source of truth for
# reads, while keeping threading.local as a fallback for non-asyncio callers
# that ran astor_init_acl() in main thread.
_ACL_CTX: contextvars.ContextVar["_AclSnapshot | None"] = contextvars.ContextVar(
    "astor_acl_ctx", default=None,
)


Role = Literal["first_admin", "admin", "user", "system"]


class PermissionError_(Exception):
    """Raised when an actor attempts an action disallowed by the permission matrix."""


_VALID_ROLES = {"first_admin", "admin", "user", "system"}
_VALID_TIERS = {"public", "source", "private", "repo"}

# Strict actor pattern: only the four canonical forms. Anything else is
# rejected at init time so typos / smuggling cannot silently bind a fake ACL.
import re as _re_
_ACTOR_RE = _re_.compile(r"^(first_admin|system|admin:[a-zA-Z0-9_\-]{1,64}|user:[a-zA-Z0-9_\-]{1,64})$")

# user_id must look like a sane id (canonical or short_alias).
_USER_ID_RE = _re_.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


# Per plan §2576-2589 matrix: action → which roles are allowed.
# Format: (action, target_tier) → set of allowed roles
# target_tier could be 'source' / 'private_<id>' / 'public' — represented here
# by source/private/public for the matrix-level check; finer per-user checks
# are done in `astor_check_read` / `astor_check_write`.
#
# 2026-08-16 strict-privacy ship: this matrix lists *which roles can attempt*
# access. Cross-user private still goes through `grants.check_grant(...)` for
# fine-grained per-data-owner authorization. So matrix entries alone no longer
# mean "implicit access allowed".
_MATRIX = {
    # Read source: only first_admin
    ("read", "source"): {"first_admin"},
    # Write source: only first_admin
    ("write", "source"): {"first_admin"},
    # Read public: any role (everyone shares public knowledge)
    ("read", "public"): {"first_admin", "admin", "user", "system"},
    # Write public: only first_admin + admin (NOT user/system — compromised
    # user/system contexts must not be able to spam public).
    ("write", "public"): {"first_admin", "admin"},
    # Read private: only the owner (cross-user via grant only)
    ("read", "private"): {"first_admin", "admin", "user"},
    # Write private: only the owner (cross-user via grant only)
    ("write", "private"): {"first_admin", "admin", "user"},
    # v1.1: tier=repo (per-git-repository memory). Anyone can read; only
    # first_admin can write (since the writer is the agent itself).
    ("read", "repo"): {"first_admin", "admin", "user", "system"},
    ("write", "repo"): {"first_admin"},
    # Read admin DBs (cross-private): only first_admin (and audit row mandatory)
    ("read", "private_other"): {"first_admin"},
    # Hard rules per plan
    ("create_user", "_"): {"first_admin"},
    ("delete_user", "_"): {"first_admin"},
    ("promote", "_"): {"first_admin"},
    ("demote", "_"): {"first_admin"},
    ("force_verdict", "any"): {"first_admin"},
    ("bot_admin", "_"): {"first_admin"},  # `am bot on/off/add-user/promote/demote`
}


@dataclass(frozen=True)
class _AclSnapshot:
    """Snapshot used by the ContextVar path. Mirrors AccessContext fields but
    is private so the public AccessContext remains the only externally
    observable contract."""
    actor: str
    role: str
    tier: str
    user_id: str | None


@dataclass(frozen=True)
class AccessContext:
    """Set at process init via `astor_init_acl(...)`; bound into ACL checks."""
    actor: str         # 'first_admin' | 'admin:<id>' | 'user:<id>' | 'system'
    role: str          # 'first_admin' | 'admin' | 'user' | 'system'
    tier: str          # 'public' | 'source' | 'private'
    user_id: str | None  # active user (None when tier=public or source)


_CURRENT = threading.local()


def _maybe_audit_grant(actor: str, action: str, tier: str, user_id: str | None, target: str) -> None:
    """Write a grant row to the audit log, throttled per (actor, action, tier, user_id)
    so high-frequency cross-user reads do not flood the audit log."""
    import time as _time
    key = (actor, action, tier, user_id, target)
    now = _time.time()
    last = _GRANT_AUDIT_CACHE.get(key)
    if last is not None and (now - last) < _GRANT_AUDIT_CACHE_TTL_SEC:
        return
    _GRANT_AUDIT_CACHE[key] = now
    astor_audit(
        actor=actor, tier=tier, action=action,
        user_id=user_id, target=target,
        metadata={"grant_path": True},
    )


@dataclass
class _LeakyBucket:
    """Leaky bucket: capacity is the burst allowance; refill rate is the
    steady-state per-second throughput. Each (actor, target, action) gets
    its own bucket so a flood against one target cannot starve other targets.

    Tokens refill continuously (not per-tick) so the bucket drains smoothly
    rather than in 1-second sawtooth pulses.
    """
    capacity: float          # max tokens (= burst allowance)
    refill_per_sec: float    # tokens refilled per second
    tokens: float            # current available tokens
    last_refill: float       # last time tokens were refilled (monotonic)

    def take(self, cost: float = 1.0) -> bool:
        now = _time_rate.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
        self.last_refill = now
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False


# Per-(actor, target_user_id, action) leaky bucket.
_RATE_BUCKETS: dict[tuple[str, str | None, str], _LeakyBucket] = {}
_GLOBAL_BUCKET: _LeakyBucket | None = None
_RATE_BUCKET_CAP = 5.0
_RATE_BUCKET_REFILL = 5.0
_RATE_GLOBAL_CAP = 50.0
_RATE_GLOBAL_REFILL = 50.0

import time as _time_rate


def _get_or_make_bucket(actor: str, target_user_id: str | None, action: str) -> _LeakyBucket:
    key = (actor, target_user_id, action)
    bucket = _RATE_BUCKETS.get(key)
    if bucket is None:
        bucket = _LeakyBucket(
            capacity=_RATE_BUCKET_CAP,
            refill_per_sec=_RATE_BUCKET_REFILL,
            tokens=_RATE_BUCKET_CAP,
            last_refill=_time_rate.time(),
        )
        _RATE_BUCKETS[key] = bucket
    return bucket


def _canonicalize_user_id(user_id: str | None, *, for_tier: str) -> str:
    """v1.2 step 4: defense-in-depth — reject user_id values that could be
    interpreted as filesystem paths or otherwise abuse the ACL.

    Caller passes user_id; the ACL does not currently resolve user_id into
    a filesystem path itself, but downstream layers (bus/store.py and
    nest/vector_store.py) DO use the value in Path.joinpath(...) to build
    db paths. Any non-canonical value (path traversal chars, NUL byte,
    backslash separator, leading dot, double slashes) is rejected here so
    the failure surfaces at the ACL layer rather than at the path layer
    where the diagnostic is harder to interpret.

    Returns the user_id unchanged if it is canonical.
    """
    if user_id is None:
        return user_id  # None is the caller's responsibility (raises separately)
    # Reject any control character (incl. NUL) or path separator.
    # NUL is written as chr(0) to avoid embedding a literal null byte in source.
    forbidden_chars = ("/", "\\", chr(0), "\n", "\r", "\t")
    for c in forbidden_chars:
        if c in user_id:
            raise PermissionError_(
                f"user_id={user_id!r} contains forbidden character "
                f"(forbidden for {for_tier} tier to prevent path traversal)"
            )
    # Reject leading dot (hidden files / relative path)
    if user_id.startswith("."):
        raise PermissionError_(
            f"user_id={user_id!r} starts with '.' (forbidden for {for_tier} tier)"
        )
    # Reject consecutive dots (e.g. '..' alone, even though already caught above)
    if ".." in user_id:
        raise PermissionError_(
            f"user_id={user_id!r} contains '..' (forbidden for {for_tier} tier)"
        )
    return user_id


def _enforce_rate_limit(actor: str, target_user_id: str | None, action: str) -> None:
    """Per-target leaky bucket. Each (actor, target_user_id, action) has
    capacity=5 tokens + refill=5/s. Exceeding raises PermissionError_.

    Also enforces a per-process global ceiling so a co-ordinated flood
    across many (actor, target) pairs cannot still succeed.
    """
    global _GLOBAL_BUCKET
    if _GLOBAL_BUCKET is None:
        _GLOBAL_BUCKET = _LeakyBucket(
            capacity=_RATE_GLOBAL_CAP,
            refill_per_sec=_RATE_GLOBAL_REFILL,
            tokens=_RATE_GLOBAL_CAP,
            last_refill=_time_rate.time(),
        )
    if not _GLOBAL_BUCKET.take():
        raise PermissionError_(
            f"rate limit: process exceeded {_RATE_GLOBAL_CAP} grant checks "
            f"per second (global ceiling)"
        )
    bucket = _get_or_make_bucket(actor, target_user_id, action)
    if not bucket.take():
        raise PermissionError_(
            f"rate limit: actor={actor!r} on target={target_user_id!r} exceeded "
            f"{_RATE_BUCKET_CAP} {action!r} checks per second (leaky bucket full)"
        )


def astor_init_acl(actor: str, role: str, tier: str, user_id: str | None = None) -> None:
    """
    Bind process-level ACL state. Call once at startup (bot init / CLI entry).

    Args:
        actor:   who this process is acting as. Convention:
                 'first_admin' (system root), 'admin:<id>', 'user:<id>',
                 'system' (background tasks like `am compact`)
        role:    one of 'first_admin' / 'admin' / 'user' / 'system' (system tasks
                 can write source only when role=first_admin explicitly opted in)
        tier:    which ACL tier this process is currently operating in
        user_id: the active user_id when tier='private' (None otherwise)

    Example:
            # bot entry point for alice:
            astor_init_acl(actor='user:alice', role='user', tier='private', user_id='alice')
        # first_admin CLI:
        astor_init_acl(actor='first_admin', role='first_admin', tier='source')
    """
    # Validate role + tier first (fail-fast on caller error)
    if role not in _VALID_ROLES:
        raise ValueError(f"role must be one of {sorted(_VALID_ROLES)}, got {role!r}")
    if tier not in _VALID_TIERS:
        raise ValueError(f"tier must be one of {sorted(_VALID_TIERS)}, got {tier!r}")
    if tier in ("private", "repo") and not user_id:
        raise ValueError(f"tier={tier} requires user_id (private=username, repo=repo_id)")
    if tier not in ("private", "repo") and user_id:
        raise ValueError(f"tier={tier} requires user_id=None (system scope)")
    # v1.2 hardening: actor must match canonical form (reject typos + smuggling)
    if not _ACTOR_RE.match(actor or ""):
        raise ValueError(
            f"actor={actor!r} does not match canonical form "
            f"(first_admin|system|admin:<id>|user:<id> with [A-Za-z0-9_-]{{1,64}})"
        )
    # v1.2 hardening: actor / role consistency (prevent impersonation)
    if actor == "first_admin" and role != "first_admin":
        raise ValueError(f"actor={actor!r} requires role='first_admin', got role={role!r}")
    if actor == "system" and role != "system":
        raise ValueError(f"actor={actor!r} requires role='system', got role={role!r}")
    if actor.startswith("admin:") and role not in ("admin", "first_admin"):
        raise ValueError(f"actor={actor!r} requires role in ('admin','first_admin'), got role={role!r}")
    if actor.startswith("user:") and role != "user":
        raise ValueError(f"actor={actor!r} requires role='user', got role={role!r}")
    # v1.2 hardening: user_id format check
    if user_id is not None and not _USER_ID_RE.match(user_id):
        raise ValueError(f"user_id={user_id!r} must match [A-Za-z0-9_-]{{1,64}}")
    # v1.2 hardening: re-init (different actor in same process) is
    # audit-logged so silent privilege escalation cannot go unnoticed.
    prev_actor = getattr(_CURRENT, "actor", None)
    if prev_actor is not None and prev_actor != actor:
        try:
            from .audit_logger import astor_audit
            astor_audit(
                actor=prev_actor,
                tier=getattr(_CURRENT, "tier", "public"),
                action="rebind",
                user_id=getattr(_CURRENT, "user_id", None),
                target="acl_context",
                metadata={"new_actor": actor, "new_role": role, "new_tier": tier},
            )
        except Exception:
            pass
    _CURRENT.actor = actor
    _CURRENT.role = role
    _CURRENT.tier = tier
    _CURRENT.user_id = user_id
    # v1.2 follow-up: also set the asyncio ContextVar so per-coroutine
    # callers see the correct ACL context without leaking between tasks.
    _ACL_CTX.set(_AclSnapshot(actor=actor, role=role, tier=tier, user_id=user_id))


def astor_current_acl() -> AccessContext:
    """Return the currently bound ACL context. Raises if not init'd yet.

    Resolution order (v1.2 follow-up):
      1. asyncio ContextVar (per-coroutine in async callers)
      2. threading.local (per-thread in legacy sync callers)
      3. raise PermissionError_ — caller forgot to call astor_init_acl
    """
    snapshot = _ACL_CTX.get()
    if snapshot is not None:
        return AccessContext(
            actor=snapshot.actor,
            role=snapshot.role,
            tier=snapshot.tier,
            user_id=snapshot.user_id,
        )
    try:
        return AccessContext(
            actor=_CURRENT.actor,
            role=_CURRENT.role,
            tier=_CURRENT.tier,
            user_id=_CURRENT.user_id,
        )
    except AttributeError:
        raise PermissionError_(
            "astor_acl not initialized — call astor_init_acl(...) at process entry"
        )


def astor_check_read(tier: str, user_id: str | None = None) -> None:
    """
    Pre-flight check before reading data from (tier, user_id).
    Raises PermissionError_ on denial. Always succeeds for public.

    Plan rules:
    - first_admin: read any private IFF has explicit grant from data owner
    - admin:       read any private IFF has explicit grant from data owner
    - user:        read ONLY own private_<self> + public

    v1.2 step 4: rejects user_id values containing path separators or
    control characters so downstream Path.joinpath() cannot be abused.
    """
    user_id = _canonicalize_user_id(user_id, for_tier=tier)
    ctx = astor_current_acl()
    if tier == "public":
        # Every role can read public
        return
    if tier == "source":
        allowed = _MATRIX[("read", "source")]
        if ctx.role not in allowed:
            raise PermissionError_(
                f"actor={ctx.actor!r} (role={ctx.role}) cannot read tier=source; "
                f"only first_admin may read source.db"
            )
        return
    if tier == "repo":
        # v1.1: repo tier readable by any role; writer restriction is on write.
        return
    # tier == "private"
    if user_id is None:
        # v1.2 hardening: pathological case must surface. Caller must supply
        # who they are reading. A silent pass here was a footgun — caller
        # bugs went undetected because they got "success" but read the wrong
        # data.
        raise PermissionError_(
            f"actor={ctx.actor!r} attempted to read tier=private with user_id=None; "
            f"caller must supply the target user_id"
        )
    # Own private scope is always allowed, including the canonical `admin`
        # first_admin identity. The grant system is only for cross-user access.
        # Also resolve user_id in case a raw platform chat_id was passed directly to acl check.
        # 2026-09-01 cleanup: ACL does NOT hardcode any bot_id. Instead it queries
        # bot-binding.db via list_admin_chat_ids() to discover operator bindings at
        # runtime. The fallback path (admin caller wants to bind a bot while
        # bot-binding.db is unavailable) is documented in BACKUP_FALLBACK.md and
        # uses env vars — never embedded here.
        from .bot_binding import resolve_chat_to_user as _resolve_chat
        from .bot_binding import list_admin_chat_ids as _list_admin_chats
        canonical_target = _resolve_chat("telegram:hermes_bot", user_id)
        if canonical_target is None:
            for platform_id, chat_id in _list_admin_chats():
                b = _resolve_chat(platform_id, chat_id)
                if b is not None:
                    canonical_target = b["user_id"]
                    break
        else:
            canonical_target = canonical_target["user_id"]
        if user_id == ctx.user_id or (canonical_target and canonical_target == ctx.user_id):
            return
        canonical_target = _resolve_chat("telegram:hermes_bot", user_id)
        if canonical_target is None:
            for platform_id, chat_id in _list_admin_chats():
                b = _resolve_chat(platform_id, chat_id)
                if b is not None:
                    canonical_target = b["user_id"]
                    break
        else:
            canonical_target = canonical_target["user_id"]
        if user_id == ctx.user_id or (canonical_target and canonical_target == ctx.user_id):
            return
    if ctx.role == "first_admin":
        # 2026-08-16 strict-privacy ship (B option): first_admin no longer has
        # implicit cross-user private access. Must hold an explicit grant
        # from the data owner. Audit row written regardless of outcome.
        if grants.check_grant(grantor=user_id, grantee="first_admin", required_scope="read"):
            astor_audit(
                actor=ctx.actor, tier="private", action="read",
                user_id=user_id, target="granted",
                metadata={"required_scope": "read"},
            )
            return
        astor_audit(
            actor=ctx.actor, tier="private", action="read",
            user_id=user_id, target="denied_no_grant",
            metadata={"required_scope": "read", "reason": "first_admin lacks grant"},
        )
        raise PermissionError_(
            f"actor={ctx.actor!r} (role=first_admin) cannot read private_<{user_id}>; "
            f"user grant required (strict privacy model 2026-08-16)"
        )
    if ctx.role == "admin":
        # 2026-08-16 strict-privacy ship: admin can no longer implicitly read
        # other users' private. Must have explicit grant from data owner.
        admin_grantee = f"admin:{ctx.user_id}" if ctx.user_id else None
        # v1.2 step 3: per-target leaky bucket rate limit before grant query.
        _enforce_rate_limit(ctx.actor, user_id, "read")
        if admin_grantee and grants.check_grant(
            grantor=user_id, grantee=admin_grantee, required_scope="read"
        ):
            astor_audit(
                actor=ctx.actor, tier="private", action="read",
                user_id=user_id, target="granted",
                metadata={"required_scope": "read"},
            )
            return
        astor_audit(
            actor=ctx.actor, tier="private", action="read",
            user_id=user_id, target="denied_no_grant",
            metadata={"required_scope": "read", "reason": "admin lacks grant"},
        )
        raise PermissionError_(
            f"actor={ctx.actor!r} (role=admin) cannot read private_<{user_id}>; "
            f"user grant required (strict privacy model 2026-08-16)"
        )
    # user: only own private
    if user_id != ctx.user_id:
        raise PermissionError_(
            f"actor={ctx.actor!r} (role={ctx.role}) cannot read "
            f"private_<{user_id}>; only own private allowed"
        )


def astor_check_write(tier: str, user_id: str | None = None) -> None:
    """
    Pre-flight check before writing data to (tier, user_id).
    Raises PermissionError_ on denial.

    Plan rules:
    - first_admin: write any private IFF has explicit 'write'/'admin' grant
    - admin:       write any private IFF has explicit 'write'/'admin' grant
    - user:        write ONLY own private_<self> + public

    v1.2 step 4: rejects user_id values containing path separators or
    control characters so downstream Path.joinpath() cannot be abused.
    """
    user_id = _canonicalize_user_id(user_id, for_tier=tier)
    ctx = astor_current_acl()
    if tier == "public":
        return  # any role can write public (their own facts)
    if tier == "source":
        if ctx.role != "first_admin":
            raise PermissionError_(
                f"actor={ctx.actor!r} (role={ctx.role}) cannot write tier=source; "
                f"only first_admin may write source.db"
            )
        return
    if tier == "repo":
        # v1.1: only first_admin can write to repo tier (agent self-
        # pattern about a specific repo).
        if ctx.role != "first_admin":
            raise PermissionError_(
                f"actor={ctx.actor!r} (role={ctx.role}) cannot write tier=repo; "
                f"only first_admin may write repo.db"
            )
        return
    # tier == "private"
    if user_id is None:
        # v1.2 hardening: pathological case must surface (same rationale
        # as the read path). A silent pass here allowed caller bugs to
        # accidentally write into another user's namespace.
        raise PermissionError_(
            f"actor={ctx.actor!r} attempted to write tier=private with user_id=None; "
            f"caller must supply the target user_id"
        )
    # Own private scope is always allowed, including the canonical `admin`
        # first_admin identity. The grant system is only for cross-user access.
        # Also resolve user_id in case a raw platform chat_id was passed directly to acl check.
        # 2026-09-01 cleanup: same runtime-query pattern as the read path — see
        # astor_check_read for rationale. ACL stays free of any hardcoded bot ID.
        from .bot_binding import resolve_chat_to_user as _resolve_chat
        from .bot_binding import list_admin_chat_ids as _list_admin_chats
        canonical_target = _resolve_chat("telegram:hermes_bot", user_id)
        if canonical_target is None:
            for platform_id, chat_id in _list_admin_chats():
                b = _resolve_chat(platform_id, chat_id)
                if b is not None:
                    canonical_target = b["user_id"]
                    break
        else:
            canonical_target = canonical_target["user_id"]
        if user_id == ctx.user_id or (canonical_target and canonical_target == ctx.user_id):
            return
        # 2026-08-16 strict-privacy ship: first_admin must hold a 'write' or
        # 'admin' grant from the data owner before writing their private tier.
        # v1.2 step 3: per-target leaky bucket rate limit before grant query.
        _enforce_rate_limit(ctx.actor, user_id, "write")
        if grants.check_grant(grantor=user_id, grantee="first_admin", required_scope="write"):
            astor_audit(
                actor=ctx.actor, tier="private", action="write",
                user_id=user_id, target="granted",
                metadata={"required_scope": "write"},
            )
            return
        astor_audit(
            actor=ctx.actor, tier="private", action="write",
            user_id=user_id, target="denied_no_grant",
            metadata={"required_scope": "write", "reason": "first_admin lacks grant"},
        )
        raise PermissionError_(
            f"actor={ctx.actor!r} (role=first_admin) cannot write private_<{user_id}>; "
            f"user write-grant required (strict privacy model 2026-08-16)"
        )
    if ctx.role == "admin":
        # 2026-08-16 strict-privacy ship: admin must hold a 'write' or 'admin'
        # grant from the data owner before modifying their private tier.
        admin_grantee = f"admin:{ctx.user_id}" if ctx.user_id else None
        # v1.2 step 3: per-target leaky bucket rate limit before grant query.
        _enforce_rate_limit(ctx.actor, user_id, "write")
        if admin_grantee and grants.check_grant(
            grantor=user_id, grantee=admin_grantee, required_scope="write"
        ):
            astor_audit(
                actor=ctx.actor, tier="private", action="write",
                user_id=user_id, target="granted",
                metadata={"required_scope": "write"},
            )
            return
        astor_audit(
            actor=ctx.actor, tier="private", action="write",
            user_id=user_id, target="denied_no_grant",
            metadata={"required_scope": "write", "reason": "admin lacks grant"},
        )
        raise PermissionError_(
            f"actor={ctx.actor!r} (role=admin) cannot write private_<{user_id}>; "
            f"user write-grant required (strict privacy model 2026-08-16)"
        )
    # user: only own private
    if user_id != ctx.user_id:
        raise PermissionError_(
            f"actor={ctx.actor!r} (role={ctx.role}) cannot write "
            f"private_<{user_id}>; only own private allowed"
        )


def astor_check_bot_admin() -> None:
    """Pre-flight check for `am bot on/off/add-user/promote/demote`."""
    ctx = astor_current_acl()
    if ctx.role != "first_admin":
        raise PermissionError_(
            f"actor={ctx.actor!r} (role={ctx.role}) cannot run bot admin commands; "
            f"only first_admin"
        )


def astor_actor_id() -> str:
    """Stable actor id for audit rows — derived from context."""
    return _CURRENT.actor


def astor_user_id() -> str | None:
    """Active user_id for private ops."""
    return _CURRENT.user_id


__all__ = [
    "Role", "AccessContext", "PermissionError_",
    "astor_init_acl", "astor_current_acl",
    "astor_check_read", "astor_check_write", "astor_check_bot_admin",
    "astor_actor_id", "astor_user_id",
]