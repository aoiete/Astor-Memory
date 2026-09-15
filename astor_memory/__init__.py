"""
astor-memory: Self-owned memory system for AI agents.

3-store triplet (bus + forge + nest) with 3-tier isolation.

See: docs/architecture.md in this repository for the full architecture overview.
"""

__version__ = '1.14.31'  # 2026-09-15 (Ship S3: schema v9→v10 adds created_at column + time_range proximity sort on legacy facts without event_date; /v1/read response surfaces created_at)  # 2026-09-15 (v1.14.30 Ship S2) eval_ripple_compare writes A/B result to bus via ASTOR_WRITE=1 env var + X-Actor=admin header)  # 2026-09-15 (v1.14.29 Ship S1) recall_log captures latency_ms per /v1/read; weekly usage_stats can now report p50/p95)  # 2026-09-15 (v1.14.28 Ship J) /v1/read appends usage JSON to recall_log.jsonl; scripts/astor_usage_stats.py weekly aggregator with Ship C justification recommendation)  # 2026-09-15 (v1.14.27 Ship I) time_range reorders enriched to deprioritize legacy facts without event_date; current corpus has 0% event_date so impact zero today, but locks in correct behavior)  # 2026-09-15 (v1.14.26 Ship D) dependency-aware Chinese person extraction; ~30% noise -> ~5% via context postfix/prefix/possessive rules + expanded stopword set)  # 2026-09-15 (v1.14.25 Ship G) dashboard_data now exposes entities_coverage field; 98.25% corpus coverage)  # 2026-09-15 (v1.14.24 Ship F) 10 multi-hop eval queries M01-M10 + eval_ripple_compare.py A/B harness; verdict: params optional, not promoting to default — admin corpus is dense single-session, RippleMem's gain was on sparse multi-session LoCoMo)  # 2026-09-15 (v1.14.23 Ship E)  # 2026-09-15 (v1.14.22 Ship B)  # 2026-09-15 (v1.14.21): Ship A — RippleMem evidence-gap hint interface (/v1/read now accepts missing_hint, entity_filter, time_range). 2026-09-14 (v1.14.20): backfill CHANGELOG + bump pyproject to match runtime SSoT (was 1.14.19, now 1.14.20); SIGSEGV fix in vector_store.py shipped in commit 4bddc76  # 2026-09-13 (v1.14.19): access_count + last_confirmed_at tracking on /v1/read; decay sweep gated by ASTOR_DECAY_SWEEP=1 (30d no-recall halve, 90d no-recall tombstone)  # 2026-09-08 (v1.14.6): lifestyle fix + dual-merge off + eval 100 + auto-rollback + sweep tier fix  # 2026-09-07 (v1.14.3): vector_store conn property detects closed-but-not-None and auto-reopens

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


