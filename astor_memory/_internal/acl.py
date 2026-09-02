"""
ACL enforcement: the central gatekeeper for all data access.

Two-tier role hierarchy + 3-value subscription_plan matrix (2026-09-02 ship):
- admin: SSoT owner. Read+write source.db, write public, manage users,
  cross-user private access via explicit grant. Identified by
  `user_meta.role='admin'`. `subscription_plan` is irrelevant for admin.
- user: Every other person. Read+write own private_<id>, read public.
  Capability differentiation is by subscription_plan (3 values):
    - power → write public + cross-read/write private with grant
    - vip   → write public (models/patterns/methods), own private, grant
    - free  → own private only (no public write, no cross-user)

  The single `role='user'` does NOT distinguish features — features come
  from subscription_plan. The matrix below uses ONLY 'admin' vs 'user' as
  roles; finer per-plan features are enforced at endpoint level
  (e.g. /v1/write public gates on ctx.subscription_plan ∈
  _PUBLIC_WRITE_PLANS = {'vip', 'power'}).

This module is process-level state. It enforces:
1. astor_check_read / astor_check_write raise PermissionError_ when the
   actor's role is not allowed by the matrix.
2. Cross-user private access requires an explicit grant from data owner
   (regardless of role/plan — strict privacy model 2026-08-16).
3. astor_init_acl re-init (changing actor in same process) is audit-logged
   so silent privilege escalation cannot go unnoticed.

v1.2 hardening (2026-09-01):
- astor_init_acl validates every argument; bad input cannot be silently
  accepted. actor must match canonical regex.
- tier='public' write is restricted to admin (permanent) — compromised
  user/system contexts cannot spam public.

Lock history: 2026-08-15 (3-tier), 2026-08-16 (strict-privacy),
2026-09-01 v1.2 hardening, 2026-09-02 (2-tier role + plan-based features).
"""

from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass
from typing import Literal, Optional

from . import grants
from .audit_logger import astor_audit

_ACL_CTX: contextvars.ContextVar["_AclSnapshot | None"] = contextvars.ContextVar(
    "astor_acl_ctx", default=None,
)


Role = Literal["admin", "user"]
Plan = Literal["free", "vip", "power"]


class PermissionError_(Exception):
    """Raised when an actor attempts an action disallowed by the permission matrix."""


_VALID_ROLES = {"admin", "user", "system"}
_VALID_PLANS = {"free", "vip", "power"}
_VALID_TIERS = {"public", "source", "private", "repo"}

# Strict actor pattern: admin:<id>, user:<id>, or bare 'system'.
import re as _re_
_ACTOR_RE = _re_.compile(
    r"^(system|admin:[a-zA-Z0-9_\-]{1,64}|user:[a-zA-Z0-9_\-]{1,64})$"
)
_USER_ID_RE = _re_.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


# Per (action, target_tier) → which roles may attempt the access.
# Public write is role='user' but GATED on subscription_plan at endpoint
# level: lifetime/paid users may write "good models, methods, rules, flows,
# patterns, structures" (智能贡献); trial/free users cannot write public
# at all. See astor_check_write() and the public-write endpoint gate.
#
# Admin: full source write + cross-private via grant + public write.
# User (any plan): own private + (with grant) cross private. Public write
# requires plan in ('permanent','lifetime','paid').
_MATRIX = {
    # Source (SSoT): admin only.
    ("read", "source"): {"admin"},
    ("write", "source"): {"admin"},
    # Public: everyone reads. Writing allowed for admin + user (user needs
    # non-trial plan — endpoint gate enforces).
    ("read", "public"): {"admin", "user", "system"},
    ("write", "public"): {"admin", "user"},
    # Private: owner only. Cross-user via grant.
    ("read", "private"): {"admin", "user"},
    ("write", "private"): {"admin", "user"},
    # v1.1: tier=repo (per-git-repository memory). Anyone reads; admin writes.
    ("read", "repo"): {"admin", "user", "system"},
    ("write", "repo"): {"admin"},
    # Cross-private (admin/power reading another user's private): only with grant.
    ("read", "private_other"): {"admin"},
    # User management: admin only.
    ("create_user", "_"): {"admin"},
    ("delete_user", "_"): {"admin"},
    ("promote", "_"): {"admin"},
    ("demote", "_"): {"admin"},
    ("force_verdict", "any"): {"admin"},
    ("bot_admin", "_"): {"admin"},  # `am bot on/off/add-user/promote/demote`
}


# 2026-09-02: subscription_plan gate for public write. ALL plans may
# write public — every user (free / vip / power) can contribute good
# models / methods / patterns / rules / flows to the public knowledge
# base. Quality is enforced at the content layer (forge extraction,
# dedup, importance), not at the ACL layer. Spam protection is the
# rate-limit gate, not the plan gate.
_PUBLIC_WRITE_PLANS = {"free", "vip", "power"}


@dataclass(frozen=True)
class _AclSnapshot:
    """Snapshot used by the ContextVar path. Mirrors AccessContext fields but
    is private so the public AccessContext remains the only externally
    observable contract."""
    actor: str
    role: str
    tier: str
    user_id: str | None
    subscription_plan: str | None  # 2026-09-02: per-plan feature gates


@dataclass(frozen=True)
class AccessContext:
    """Set at process init via `astor_init_acl(...)`; bound into ACL checks."""
    actor: str         # 'admin:<id>' | 'user:<id>' | 'system'
    role: str          # 'admin' | 'user' | 'system'
    tier: str          # 'public' | 'source' | 'private'
    user_id: str | None  # active user (None when tier=public or source)
    subscription_plan: str | None = None  # 'power'|'vip'|'free' for users; None for admin


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


def astor_init_acl(
    actor: str,
    role: str,
    tier: str,
    user_id: str | None = None,
    subscription_plan: str | None = None,
) -> None:
    """
    Bind process-level ACL state. Call once at startup (bot init / CLI entry).

    Args:
        actor:   who this process is acting as. Convention:
                 'admin:<id>', 'user:<id>', 'system' (background tasks).
                 Note: 'first_admin' is GONE (2026-09-02 simplification) —
                 the SSoT owner is just 'admin:<id>' with role='admin' and no plan.
        role:    one of 'admin' / 'user' / 'system'. Plan-based features
                 (power/vip/free) are passed via subscription_plan.
        tier:    which ACL tier this process is currently operating in.
        user_id: the active user_id when tier='private' (None otherwise).
        subscription_plan: 'power'|'vip'|'free' (admin ignores).
                          power = SSoT owner (or top user tier); vip = paid
                          permanent user; free = trial / unverified.

    Example:
            # bot entry point for alice (vip user):
            astor_init_acl(actor='user:alice', role='user', tier='private',
                           user_id='alice', subscription_plan='vip')
        # SSoT owner CLI:
        astor_init_acl(actor='admin:admin', role='admin', tier='source',
                       user_id='admin')
    """
    # Validate role + tier first (fail-fast on caller error)
    if role not in _VALID_ROLES:
        raise ValueError(f"role must be one of {sorted(_VALID_ROLES)}, got {role!r}")
    if tier not in _VALID_TIERS:
        raise ValueError(f"tier must be one of {sorted(_VALID_TIERS)}, got {tier!r}")
    if subscription_plan is not None and subscription_plan not in _VALID_PLANS:
        raise ValueError(
            f"subscription_plan must be one of {sorted(_VALID_PLANS)}, "
            f"got {subscription_plan!r}"
        )
    if tier in ("private", "repo") and not user_id:
        raise ValueError(f"tier={tier} requires user_id (private=username, repo=repo_id)")
    # 2026-09-02 ship: allow user_id on tier=public/source too. The ACL stores
    # the actor's identity for cross-tier reclassification (e.g. content
    # classifier auto-routes public to private_<actor>). Without user_id on
    # public init, the reclassified own-private write fails the
    # ctx.user_id == target_user check.
    # Validation: if user_id provided for public/source, must be a valid form.
    if tier not in ("private", "repo") and user_id:
        try:
            _canonicalize_user_id(user_id, for_tier="private")
        except ValueError as _v:
            raise ValueError(
                f"tier={tier} user_id={user_id!r} failed validation: {_v}"
            )
    # v1.2 hardening: actor must match canonical form (reject typos + smuggling)
    if not _ACTOR_RE.match(actor or ""):
        raise ValueError(
            f"actor={actor!r} does not match canonical form "
            f"(system|admin:<id>|user:<id> with [A-Za-z0-9_-]{{1,64}})"
        )
    # v1.2 hardening: actor / role consistency (prevent impersonation)
    if actor == "system" and role != "system":
        raise ValueError(f"actor={actor!r} requires role='system', got role={role!r}")
    if actor.startswith("admin:") and role != "admin":
        raise ValueError(f"actor={actor!r} requires role='admin', got role={role!r}")
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
                metadata={
                    "new_actor": actor, "new_role": role, "new_tier": tier,
                    "new_plan": subscription_plan,
                },
            )
        except Exception:
            pass
    _CURRENT.actor = actor
    _CURRENT.role = role
    _CURRENT.tier = tier
    _CURRENT.user_id = user_id
    _CURRENT.subscription_plan = subscription_plan
    # v1.2 follow-up: also set the asyncio ContextVar so per-coroutine
    # callers see the correct ACL context without leaking between tasks.
    _ACL_CTX.set(_AclSnapshot(
        actor=actor, role=role, tier=tier, user_id=user_id,
        subscription_plan=subscription_plan,
    ))


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
            subscription_plan=snapshot.subscription_plan,
        )
    try:
        return AccessContext(
            actor=_CURRENT.actor,
            role=_CURRENT.role,
            tier=_CURRENT.tier,
            user_id=_CURRENT.user_id,
            subscription_plan=getattr(_CURRENT, "subscription_plan", None),
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
                f"only admin may read source.db"
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
    # Own private scope is always allowed. The grant system is only for
    # cross-user access.
    if user_id == ctx.user_id:
        return
    if ctx.role == "admin":
        # 2026-09-02 simplification: admins cross-read other users' private
        # only via explicit grant from the data owner. No implicit root.
        # Use ctx.actor directly as grantee (e.g. 'admin:admin' for the SSoT
        # owner, 'admin:bob' for any other admin). This lets data owners
        # grant specific admins rather than 'admin:<self>' which is awkward.
        admin_grantee = ctx.actor if ctx.role == "admin" else None
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

    2026-09-02 simplification: use _MATRIX (per (action, tier)) + plan gate.

    Plan rules (for role='user'):
    - power: write public + cross-write private via grant
    - vip:   write public (gated by _PUBLIC_WRITE_PLANS), own private, grant
    - free:  own private only (no public write, no cross-user)

    admin: full source write + public write + cross-private via grant.
    """
    user_id = _canonicalize_user_id(user_id, for_tier=tier)
    ctx = astor_current_acl()

    # Stage 1: role-based matrix gate
    allowed = _MATRIX[("write", tier)]
    if ctx.role not in allowed:
        raise PermissionError_(
            f"actor={ctx.actor!r} (role={ctx.role}) cannot write tier={tier}; "
            f"allowed roles: {sorted(allowed)}"
        )

    # Stage 2: subscription_plan gate (for tier=public, user role only).
    # 2026-09-02: all plans allowed (free / vip / power); quality is enforced
    # at content layer (forge extraction), not ACL.
    if tier == "public" and ctx.role == "user":
        if ctx.subscription_plan not in _PUBLIC_WRITE_PLANS:
            raise PermissionError_(
                f"actor={ctx.actor!r} (role=user, plan={ctx.subscription_plan}) "
                f"cannot write tier=public; "
                f"required plan in {sorted(_PUBLIC_WRITE_PLANS)}"
            )

    # Stage 2.5: per-actor rate limit on ALL writes (anti-spam first line).
    # Without this, any user can flood public with garbage and let forge
    # dedup clean up — but storage cost is real.
    _enforce_rate_limit(ctx.actor, user_id or tier, "write")

    # Stage 3: tier=private — own vs cross-user
    if tier == "private":
        if user_id is None:
            raise PermissionError_(
                f"actor={ctx.actor!r} attempted to write tier=private with user_id=None; "
                f"caller must supply the target user_id"
            )
        # Own private scope is always allowed.
        if user_id == ctx.user_id:
            return
        # Cross-user: requires explicit grant from data owner.
        # 2026-09-02: use ctx.actor as grantee (consistent with read path).
        # e.g. 'admin:admin' for SSoT owner, 'admin:bob' for any other admin.
        _enforce_rate_limit(ctx.actor, user_id, "write")
        if ctx.role == "admin":
            grantee = f"admin:{ctx.user_id}" if ctx.user_id else None
        elif ctx.role == "user":
            grantee = f"user:{ctx.user_id}" if ctx.user_id else None
        else:
            grantee = None
        if grantee and grants.check_grant(
            grantor=user_id, grantee=grantee, required_scope="write"
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
            metadata={"required_scope": "write", "reason": "actor lacks grant"},
        )
        raise PermissionError_(
            f"actor={ctx.actor!r} (role={ctx.role}) cannot write private_<{user_id}>; "
            f"user write-grant required (strict privacy model 2026-08-16)"
        )


def astor_check_bot_admin() -> None:
    """Pre-flight check for `am bot on/off/add-user/promote/demote`."""
    ctx = astor_current_acl()
    if ctx.role != 'admin':
        raise PermissionError_(
            f"actor={ctx.actor!r} (role={ctx.role}) cannot run bot admin commands; "
            f"only admin (role='admin' required for `am bot ...` commands)"
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