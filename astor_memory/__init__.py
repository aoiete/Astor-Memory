"""
astor-memory: Self-owned memory system for AI agents.

3-store triplet (bus + forge + nest) with 3-tier isolation.

See: docs/architecture.md in this repository for the full architecture overview.
"""

__version__ = "1.14.72"  # 2026-09-17 (Ship v1.14.72 — Phase 3 MVP (transport + signed msg flow). NEW: peer_relationships.endpoint column (TEXT, holds HTTP/HTTPS URL). update_endpoint() helper validates URL prefix. endpoint preserved through apply_rekey. NEW CLI: `am peer endpoint set|get|clear <peer_id> [url]`. `am peer friend add --endpoint=<url>` one-shot. NEW CLI: `am peer send-rekey <peer_id> [--old=<pid>] [--dry-run]` + `am peer send-topic <peer_id> [--dry-run]` — build signed msg + POST to peer's endpoint/v1/peer/recv (urllib, 10s timeout, HTTPError handling). NEW SERVER: /v1/peer/recv endpoint dispatches msg_type=rekey|topic_index. rekey: verify_rekey_message, decision matrix (trust>=70 auto_accept, 30-69 manual_pending, <30 reject, None manual_pending), record_rekey (does NOT auto-apply — admin applies via CLI). topic_index: auto-create peer if new (kind=pending trust=30), set_topic for each entry with source='peer_recv'. 10 new tests in tests/test_peer_phase3.py (4 endpoint CRUD + 6 recv endpoint logic via Flask test client). 113/113 in ship subset. Phase 3 MVP = 3.1 peer_endpoint + 3.2 /v1/peer/recv + 3.3 send-rekey + send-topic. Closes the 3-branch from S1 extension discussion. Cross-runtime test path: A runs `am peer friend add B-pid --endpoint=https://B...`, A runs `am peer send-rekey B-pid`, B's /v1/peer/recv verifies + records, admin applies via `am peer rekey-apply`.)
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


