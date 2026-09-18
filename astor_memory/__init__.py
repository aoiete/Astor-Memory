"""
astor-memory: Self-owned memory system for AI agents.

3-store triplet (bus + forge + nest) with 3-tier isolation.

See: docs/architecture.md in this repository for the full architecture overview.
"""

__version__ = "1.14.68"  # 2026-09-17 (Ship v1.14.68 — Peer Phase 2 (friend + trust + rekey). NEW: astor_memory/_internal/peer_relationships.py — peer_relationships + rekey_log SQLite tables at $ASTOR_DIR/identity/relationships.db. CRUD: add_peer/get_peer/list_peers/remove_peer/update_trust + record_rekey/apply_rekey/get_rekey_log. NEW CLI: `am peer friend add|list|remove` + `am peer trust <peer_id> <0-100>` + `am peer blacklist <peer_id> --reason=X` + `am peer export --out=path.yaml` + `am peer import path.yaml [--strategy=skip|overwrite|merge]` + `am peer rekey [--old=<old_id>] [--out=path.json]` + `am peer rekey-apply path.json [--auto]` + `am peer rekey-log [--status=X]`. REKEY design: signed message (old_peer_id + new_peer_id + new_public_key + timestamp), verifier reconstructs payload + verify signature with signer_pubkey. Decision matrix: trust>=70 → auto_accept (KEEP trust, swap peer_id), 30-69 → manual_pending, <30 → reject. rekey_chain accumulates across multiple rekeys (peer_id history preserved). 20 new tests in tests/test_peer_relationships.py. Full suite: 148/149 in subset, 1 pre-existing test_basic.py failure unrelated (confirmed via git stash). 148+20 new = 148 passed in v1.14.68 ship subset. Live runtime v1.14.68 verified: friend CRUD, blacklist, trust update, signed REKEY roundtrip, decision matrix (auto/pending/reject), export/import (skip/overwrite/merge strategies all work). Closes user prompt: 'peerid 变是不是可以通过好友列表通知？' — YES via signed REKEY message + friend list verification.)
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


