"""
astor-memory: Self-owned memory system for AI agents.

3-store triplet (bus + forge + nest) with 3-tier isolation.

See: docs/architecture.md in this repository for the full architecture overview.
"""

__version__ = "1.15.4"  # 2026-09-22 (Ship v1.15.4 — jev pass e: opt-in recall rerank. NEW CLI flag `am recall --jev-relevance off|on` (default off). When on, takes top candidates from the local hybrid search and asks jev for a relevance verdict (top/mid/low), logs every call to logs/jev_recall_rerank.jsonl for precision-eval data. Errors degrade silently: any exception in jev_call (timeout, missing typesafe_sdk, import error) is caught and the original hybrid ranking is returned unchanged. Per-candidate rerank deferred to v1.15.5 — jev currently returns a SINGLE verdict for the whole candidate set, not per-candidate; logging gives us the data to design per-candidate rerank in the next pass. Requires D:\AI\scripts\admin\jev\ on PYTHONPATH at runtime-call; the CLI auto-detects via importlib path probe. No DB migration, no public API change.)

  # 2026-09-18 (Ship v1.14.73 — Memory Decay policy + WeChat article-driven improvements). NEW CLI: `am decay-sweep run --tier public|source|private|repo [--user-id] [--max-importance 0.5] [--idle-days 30] [--low-access-count 0] [--limit 500] [--execute] [--reason <str>]` + `am decay-sweep stats ...` — auto soft-tombstone facts by importance+access_count criteria. Default dry-run reports only; pass --execute to actually tombstone + write audit log entry (actor='cli:decay-sweep', action='decay_sweep'). Reversible via restore. Inspired by WeChat article "Alan 的记录与分享 - Agent Memory Lifecycle" (mp.weixin.qq.com/s/ntcO59mPowrpiaLNGGd67Q): identifies "哪些信息应该被遗忘" as a separate lifecycle stage requiring automated policy + audit trail. Criteria design choice: dropped `last_confirmed_at` from the SQL because it gets reset on every read (fact 12055), making it useless as an idle proxy. Replaced with `access_count <= N` which actually tracks "nobody cares about this fact anymore". Also fixed 2 pre-existing CLI bootstrap bugs: registered-but-undefined `cmd_peer_allow_search`, `cmd_peer_search`, `cmd_peer_unallow_search` (added v1.14.73 stub returning `[WARN] not yet implemented` + exit 1 to unblock module import + decay-sweep ship verification).
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


