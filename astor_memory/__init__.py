"""
astor-memory: Self-owned memory system for AI agents.

3-store triplet (bus + forge + nest) with 3-tier isolation.

See: docs/architecture.md in this repository for the full architecture overview.
"""

__version__ = "1.14.64"  # 2026-09-17 (Ship v1.14.64: R-class classifier guard — astor_classify_outcome now short-circuits to 'success' when text starts with 'R-class' marker, even if body contains failure keywords like 卡/报错/hang/timeout; consolidator _classify_content also adds R-class as SUCCESS_PATTERN and removes from FAILURE_PATTERNS; 6 new tests in tests/test_classify_rclass_guard.py; Fact 12642 (SSH push) and Fact 3241 (Agent×classifier meta-rule) re-classified from failure_pattern to success_pattern on disk. Last ship was v1.14.63 LOCK rule + stderr hardening.)
    # 2026-09-16 (v1.14.40) Recent Capture panel: dashboard `/v1/dashboard` now exposes `recent_capture` grouped by 3 axes (kind / tier / platform) plus an 'all' flatten view. Frontend tab-toggle UI (By Kind / By Tier / By Platform / All). Each bucket capped at 10 rows. Discord/Telegram/WeChat/Cron/Manual/Other routing via `origin_session_id` prefix.

# Top-level singleton accessors. Per Plan § Naming:
# astor_bus() / astor_forge() / astor_nest() are the unified public API entry points.
from .bus import astor_bus as _astor_bus_func
from .nest import astor_nest as _astor_nest_func
from . import forge as _forge_module


def astor_bus(tier: str = "public", user_id: str | None = None):
    """Return the bus singleton (events + canonical facts).

    2026-08-15 ship: tier is REQUIRED for write safety. Default is 'public'
    (read-mostly) so CLI tools that just inspect public state don't break,
    but agents writing private data must explicitly pass tier='private',
    user_id=<id>. The legacy "no tier" path was removed because it
    silently regenerated a root db bypassing 3-tier ACL.
    """
    return _astor_bus_func(tier=tier, user_id=user_id)


def astor_nest(tier: str = "public", user_id: str | None = None):
    """Return the nest singleton (vector store for facts)."""
    return _astor_nest_func(tier=tier, user_id=user_id)


def _cleanup_nest_singleton(instance) -> None:
    """v1.10.8 (2026-08-26): remove `instance` from the nest singleton dict.

    Called from AstorNest.close() so that subsequent astor_nest() calls
    with the same (tier, user_id, db_path) key rebuild the handle instead
    of returning a closed instance whose _conn is None.
    """
    from .nest import vector_store as _vs
    with _vs._nest_lock:
        singleton = _vs._nest_singleton
        if isinstance(singleton, dict):
            # Walk all keys, remove those pointing to `instance`
            stale = [k for k, v in singleton.items() if v is instance]
            for k in stale:
                del singleton[k]


def astor_forge():
    """Return the forge module (LLM fact extraction).

    Forge is a pure-functions module (no stateful singleton).
    This wrapper exists for API parity with astor_bus() / astor_nest().
    """
    return _forge_module


__all__ = ['__version__', 'astor_bus', 'astor_nest', 'astor_forge']


