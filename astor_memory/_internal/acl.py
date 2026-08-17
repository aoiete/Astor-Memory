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

Lock: 2026-08-15 (turn design discussion); 2026-08-16 strict-privacy ship.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from typing import Literal

from .acl_layout import Tier, get_db_path, _validate_user_id
from . import grants
from .audit_logger import astor_audit


Role = Literal["first_admin", "admin", "user", "system"]


class PermissionError_(Exception):
    """Raised when an actor attempts an action disallowed by the permission matrix."""


_VALID_ROLES = {"first_admin", "admin", "user", "system"}


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
    # Write public: any authenticated role
    ("write", "public"): {"first_admin", "admin", "user", "system"},
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
class AccessContext:
    """Set at process init via `astor_init_acl(...)`; bound into ACL checks."""
    actor: str         # 'first_admin' | 'admin:<id>' | 'user:<id>' | 'system'
    role: str          # 'first_admin' | 'admin' | 'user' | 'system'
    tier: str          # 'public' | 'source' | 'private'
    user_id: str | None  # active user (None when tier=public or source)


_CURRENT = threading.local()


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
        # bot entry point for user_e:
        astor_init_acl(actor='user:user_e', role='user', tier='private', user_id='user_e')
        # first_admin CLI:
        astor_init_acl(actor='first_admin', role='first_admin', tier='source')
    """
    if role not in _VALID_ROLES:
        raise ValueError(f"role must be one of {sorted(_VALID_ROLES)}, got {role!r}")
    if tier not in ("public", "source", "private", "repo"):
        raise ValueError(f"tier must be public/source/private/repo, got {tier!r}")
    # v1.1: tier=repo requires user_id (= repo_id). Both private and repo
    # use user_id to disambiguate, but the namespace prefix differs.
    if tier in ("private", "repo") and not user_id:
        raise ValueError(f"tier={tier} requires user_id (private=username, repo=repo_id)")
    if tier not in ("private", "repo") and user_id:
        raise ValueError(f"tier={tier} requires user_id=None (system scope)")
    _CURRENT.actor = actor
    _CURRENT.role = role
    _CURRENT.tier = tier
    _CURRENT.user_id = user_id


def astor_current_acl() -> AccessContext:
    """Return the currently bound ACL context. Raises if not init'd yet."""
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
    """
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
    """
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
    if ctx.role == "first_admin":
        # 2026-08-16 strict-privacy ship: first_admin must hold a 'write' or
        # 'admin' grant from the data owner before writing their private tier.
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