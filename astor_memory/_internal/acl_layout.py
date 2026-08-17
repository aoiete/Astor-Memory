"""
ACL-aware 9-db layout (3 tier × 3 store).

Per Plan § 3-tier isolation + Plan § Per-user DB naming (2026-08-15 supersede):
The 3-tier × 3-store design splits SQLite into 9 physical files instead of
sharing one DB per tier. The layout is:

    ~/.astor/
    ├── public/
    │   └── memory/
    │       ├── astor_bus_public.db
    │       ├── astor_nest_public.db
    │       └── astor_forge_public.db
    ├── source/
    │   └── memory/
    │       ├── astor_bus_source.db
    │       ├── astor_nest_source.db
    │       └── astor_forge_source.db
    └── users/<user_id>/
        └── memory/
            ├── astor_bus_<user_id>.db
            ├── astor_nest_<user_id>.db
            └── astor_forge_<user_id>.db

Every store (bus/nest/forge) keeps its own SQLite file per (tier, optional user).
ACL is enforced by path resolution — once a connection opens at the right path,
the caller can't accidentally read another user's data unless their process has
filesystem access (mode 0700 enforced on `users/<u>/`).

This module is the single source of truth for paths. Stores call into here
instead of computing their own paths.

Lock: 2026-08-15 (turn design discussion); replaces prior per-tier single-db plan.
"""

from __future__ import annotations

import os
import re
from enum import Enum
from pathlib import Path


class Tier(str, Enum):
    """ACL tiers.

    v1.0: public / source / private (3-tier isolation).
    v1.1: REPO added — per-git-repository isolation. Inspired by MemoraX
    `Repo Memory` (.repo_memory/ local-only per worktree). Repo facts are
    scoped to a single git remote URL — when user asks about a repo, only
    that repo's memory surfaces; cross-repo knowledge stays in public.
    """
    PUBLIC = "public"
    SOURCE = "source"
    PRIVATE = "private"
    REPO = "repo"


class Store(str, Enum):
    """3 storage subsystems (plan §3-store triplet)."""
    BUS = "bus"
    NEST = "nest"
    FORGE = "forge"


# Reserved user ids (used in lieu of a real user to keep ACL layout consistent)
SYSTEM_USER = "_system"        # astor system itself (writes source tier)
FIRST_ADMIN_USER = "admin"    # plan §first_admin lives under users/admin/
DEFAULT_USER = "_current"     # single-user mode default (was '_default' in plan)


def get_astor_dir() -> Path:
    """Resolve astor runtime dir.

    Priority (highest first):
      1. $ASTOR_DIR env var (full absolute path)
      2. $ASTOR_DIR_NAME env var (relative to home, e.g. 'Astor-Memory-Runtime')
      3. Default: '~/.astor/' (Unix-style hidden dir; cross-OS standard)

    Examples:
        ASTOR_DIR=/d/Astor-Memory/Runtime   → /d/Astor-Memory/Runtime
        ASTOR_DIR_NAME=Astor-Memory-Runtime → ~/Astor-Memory-Runtime
        (no env)                            → ~/.astor/

    Rationale (turn 2026-08-15): source-code separable from runtime data.
    `pip install astor-memory` users get Unix-style ~/.astor/ by default;
    per-machine customization via env var keeps the code path simple.
    """
    if env_dir := os.environ.get("ASTOR_DIR"):
        return Path(env_dir).expanduser()
    if env_name := os.environ.get("ASTOR_DIR_NAME"):
        return Path.home() / env_name
    return Path("~/.astor").expanduser()


def _tier_dir(tier: Tier, user_id: str | None) -> Path:
    """Map tier to its top-level dir name under ~/.astor/.

    v1.1: tier=repo takes a repo_id (sha256[:16] of git remote URL, or
    explicit 'repo_<name>') and stores under repos/<repo_id>/.
    """
    if tier == Tier.PRIVATE:
        if not user_id:
            raise ValueError(
                f"tier=private requires user_id (got None). "
                f"Use tier=public or tier=source for non-user data."
            )
        return Path("users") / user_id
    if tier == Tier.REPO:
        if not user_id:
            raise ValueError(
                f"tier=repo requires repo_id (got None). "
                f"repo_id = sha256[:16] of git remote URL, or 'repo_<name>'."
            )
        return Path("repos") / user_id
    return Path(tier.value)  # 'public' or 'source'


def normalize_repo_id(remote_url: str | None, name: str | None = None) -> str:
    """Convert a git remote URL (or fallback name) to a stable repo_id.

    Per MemoraX design: Repo Memory is per-worktree, scoped by canonical
    repository identity. We use sha256[:16] of the remote URL to get a
    short, stable, path-safe identifier. Falls back to 'repo_<name>' if
    URL is missing or empty (e.g. local-only repo with no remote).
    """
    import hashlib as _hl
    if remote_url and remote_url.strip():
        h = _hl.sha256(remote_url.strip().encode('utf-8')).hexdigest()[:16]
        return h
    if name:
        # Sanitize: alphanumeric + dash + underscore only, max 32 chars
        import re as _re
        safe = _re.sub(r'[^a-zA-Z0-9_-]', '_', name)[:32]
        return f'repo_{safe}'
    raise ValueError(
        "normalize_repo_id: provide remote_url or name (both empty)"
    )


def get_db_path(
    tier: Tier | str,
    store: Store | str,
    user_id: str | None = None,
) -> Path:
    """
    Resolve the canonical SQLite path for a (tier, store[, user_id]) combination.

    Examples (default: ASTOR_DIR not set → ~/.astor/):
        get_db_path('public', 'bus') -> ~/.astor/public/memory/astor_bus_public.db
        get_db_path('source', 'nest') -> ~/.astor/source/memory/astor_nest_source.db
        get_db_path('private', 'bus', 'alice') -> ~/.astor/users/alice/memory/astor_bus_alice.db

    Raises ValueError if tier=private without user_id, or if user_id contains
    forbidden characters (path traversal protection).
    """
    t = Tier(tier) if isinstance(tier, str) else tier
    s = Store(store) if isinstance(store, str) else store

    if user_id is not None:
        _validate_user_id(user_id)

    # DB filename suffix: tier name, user_id for private, repo_id for repo
    if t == Tier.PRIVATE:
        suffix = user_id or ""
    elif t == Tier.REPO:
        if not user_id:
            raise ValueError("tier=repo requires user_id (= repo_id)")
        suffix = user_id
    else:
        suffix = t.value
    filename = f"astor_{s.value}_{suffix}.db"

    return get_astor_dir() / _tier_dir(t, user_id) / "memory" / filename


def get_audit_path() -> Path:
    """
    Audit log lives OUTSIDE the 9-db layout in `~/.astor/audit/astor_audit.db`
    so even a corrupted tier subdirectory cannot lose the audit trail.
    Mode 0600 enforced on the file when first written.
    """
    return get_astor_dir() / "audit" / "astor_audit.db"


def get_admin_lock_path() -> Path:
    """Path to `~/.astor/admin.lock` (plan §first_admin permanence)."""
    return get_astor_dir() / "admin.lock"


def get_install_state_path() -> Path:
    """Path to `~/.astor/install-state.json`."""
    return get_astor_dir() / "install-state.json"


def list_user_ids() -> list[str]:
    """
    Scan ~/.astor/users/*/ and return directory names (user ids).
    Empty list if the directory does not exist (single-user mode before `am bot on`).
    """
    users_dir = get_astor_dir() / "users"
    if not users_dir.is_dir():
        return []
    return sorted([p.name for p in users_dir.iterdir() if p.is_dir()])


def list_repo_ids() -> list[str]:
    """Scan ~/.astor/repos/*/ and return repo_id directory names.

    Repo IDs are sha256[:16] of the git remote URL (or 'repo_<name>'
    fallback). Display name → repo_id mapping lives in the repo's
    `meta.json` written by `am repo register`.
    """
    repos_dir = get_astor_dir() / "repos"
    if not repos_dir.is_dir():
        return []
    return sorted([p.name for p in repos_dir.iterdir() if p.is_dir()])


def ensure_layout(tier: Tier | str, store: Store | str, user_id: str | None = None) -> Path:
    """
    Ensure the parent dir exists for the given (tier, store[, user_id]) db file.
    Returns the resolved path. mkdir with parents=True; mode 0700 on private
    user dirs and repo dirs.

    For private tier user dirs we set directory mode 0700 (Unix) to enforce that
    only the file owner can read/write that user's data. Repo dirs (v1.1) get
    the same treatment — repo memory may contain sensitive architecture notes.
    On Windows the chmod is a no-op for non-root users — Windows ACL would
    replace this in v0.3.
    """
    path = get_db_path(tier, store, user_id)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if Tier(tier) in (Tier.PRIVATE, Tier.REPO):
        try:
            os.chmod(parent, 0o700)
        except (OSError, PermissionError):
            # Windows or non-POSIX FS — chmod is informational, skip silently.
            pass
    return path


# === Validation ===

_USER_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _validate_user_id(user_id: str) -> None:
    """Reject user ids with path-traversal risk or absurd length."""
    if not _USER_ID_RE.match(user_id):
        raise ValueError(
            f"Invalid user_id {user_id!r}: must match {_USER_ID_RE.pattern}"
        )
    if user_id in {".", "..", "public", "source"}:
        raise ValueError(
            f"user_id {user_id!r} is reserved (collides with tier dir name)."
        )


__all__ = [
    "Tier", "Store",
    "SYSTEM_USER", "FIRST_ADMIN_USER", "DEFAULT_USER",
    "get_astor_dir",
    "get_db_path", "get_audit_path", "get_admin_lock_path",
    "get_install_state_path", "list_user_ids",
    "ensure_layout",
]
