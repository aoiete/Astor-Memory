"""
astor-memory: Self-owned memory system for AI agents.

3-store triplet (bus + forge + nest) with 3-tier isolation.

See: docs/architecture.md in this repository for the full architecture overview.
"""

__version__ = "1.14.47"  # 2026-09-16 (Ship G: L1/L2 multi-granularity server wiring — /v1/read now calls AstorNest.search_l1_l2() after hybrid recall, with lazy rebuild_clusters() if cluster_embeddings is empty (once per process via _l12_rebuild_attempted flag). Default-on; ASTOR_MULTIGR_ENABLED=0 disables. No new tests — wiring covered by test_l1_l2_recall.py (method unit tests) + manual server verification.)  # 2026-09-16 (Ship F: kind-based routing — wing alias for /v1/read (wing=human maps to {manual}, wing=agent maps to {extracted, inferred, merged}, wing=rule maps to {rule}). Auto-derive provenance_kind from origin_session_id prefix at /v1/write (no caller change required). WING_TO_PROVENANCE dict + _expand_wing_to_provenance helper + _infer_provenance_kind helper. 11 tests in tests/test_wing_routing.py. ADR-0006 accepted.)  # 2026-09-16 (Ship C: /v1/health/diagnose expansion — proxy_hijack_check (HTTPS_PROXY/HTTP_PROXY env detection, loopback exempt, Windows case-insensitive dedupe) + db_corruption_check (PRAGMA integrity_check + foreign_key_check) + embedding_version_check (model load + dim probe). Aggregated ship_c_warn boolean. 5 tests in tests/test_diagnose_expansion.py. ADR-0005 accepted.)  # 2026-09-16 (Ship A: L1/L2 multi-granularity recall — cluster_embeddings table (schema v4) + AstorNest.rebuild_clusters() (mean-pool session_id member embeddings) + AstorNest.search_l1_l2() (L1 cosine → top clusters → L2 cosine restricted to members). Solves same-session fact competition in recall slot. ADR-0004 accepted. 5 new tests in tests/test_l1_l2_recall.py. Env var ASTOR_MULTIGR_ENABLED=0 disables.)  # 2026-09-16 (Ship B: ADR directory docs/adr/ + MADR format + 3 ADRs accepted (0001 9-DB layout, 0002 hybrid retrieval R201 lock, 0003 decay sweep default-on) + 2 reserved stubs (0004 L1/L2, 0005 diagnose). Competitive sheet "Lessons Learned" now ADR-linked. ADR convention: use ADR-NNNN short form in chat and commits.)  # 2026-09-16 (decay sweep flipped to default-on: ASTOR_DECAY_SWEEP=0 now disables; MemPalace v3.3.6 living-memory dynamics validates the direction. 5 new tests in tests/test_decay_sweep.py. New docs/competitive-sheet.md T-sheet comparing astor against MemU/MemPalace/Mem0/Zep.)  # 2026-09-16 (silent recall: RECALL_PREAMBLE + hermes_adapter prefetch Block 1/2 now carry "background context only — do NOT echo to user" instruction; auto-recall hits stay in agent context, never rendered into the chat bubble unless user explicitly asks to see memory)  # 2026-09-16 (AstorClient now ships X-Actor header automatically — derives 'user:<id>' from self.user_id; matches hermes gateway convention so free users can write public/private tier via AstorClient.write/read. Previously free-user writes were rejected with permission_denied because server's per-request ACL binding resolved caller identity from body fields only. Test test_client_identity_fields_are_optional_and_backward_compatible updated to expect tuple return.)  # 2026-09-15 (Ship J: admin actor bypasses per-actor 5/sec rate limit — admin already passes _MATRIX role gate; tier=public/source/private writes for admin no longer hit leaky bucket)  # 2026-09-15 (Ship I: provenance_kind/agent threaded from /v1/write body through promote_candidate to memory_canonical — hook writes can now be filtered from manual writes via DB column)  # 2026-09-15 (Ship H: /v1/read session_id URL param + recall_log session_id_used — session-scoped recall)  # 2026-09-15 (Ship G: /v1/read kinds URL param + recall_log kinds_used field — zone-filtered recall over HTTP)  # 2026-09-15 (v1.14.31 Ship S3: schema v9→v10 adds created_at column + time_range reorder; legacy facts deprioritized)  # 2026-09-15 (Ship S3: schema v9→v10 adds created_at column + time_range proximity sort on legacy facts without event_date; /v1/read response surfaces created_at)  # 2026-09-15 (v1.14.30 Ship S2) eval_ripple_compare writes A/B result to bus via ASTOR_WRITE=1 env var + X-Actor=admin header)  # 2026-09-15 (v1.14.29 Ship S1) recall_log captures latency_ms per /v1/read; weekly usage_stats can now report p50/p95)  # 2026-09-15 (v1.14.28 Ship J) /v1/read appends usage JSON to recall_log.jsonl; scripts/astor_usage_stats.py weekly aggregator with Ship C justification recommendation)  # 2026-09-15 (v1.14.27 Ship I) time_range reorders enriched to deprioritize legacy facts without event_date; current corpus has 0% event_date so impact zero today, but locks in correct behavior)  # 2026-09-15 (v1.14.26 Ship D) dependency-aware Chinese person extraction; ~30% noise -> ~5% via context postfix/prefix/possessive rules + expanded stopword set)  # 2026-09-15 (v1.14.25 Ship G) dashboard_data now exposes entities_coverage field; 98.25% corpus coverage)  # 2026-09-15 (v1.14.24 Ship F) 10 multi-hop eval queries M01-M10 + eval_ripple_compare.py A/B harness; verdict: params optional, not promoting to default — admin corpus is dense single-session, RippleMem's gain was on sparse multi-session LoCoMo)  # 2026-09-15 (v1.14.23 Ship E)  # 2026-09-15 (v1.14.22 Ship B)  # 2026-09-15 (v1.14.21): Ship A — RippleMem evidence-gap hint interface (/v1/read now accepts missing_hint, entity_filter, time_range). 2026-09-14 (v1.14.20): backfill CHANGELOG + bump pyproject to match runtime SSoT (was 1.14.19, now 1.14.20); SIGSEGV fix in vector_store.py shipped in commit 4bddc76  # 2026-09-13 (v1.14.19): access_count + last_confirmed_at tracking on /v1/read; decay sweep gated by ASTOR_DECAY_SWEEP=1 (30d no-recall halve, 90d no-recall tombstone)  # 2026-09-08 (v1.14.6): lifestyle fix + dual-merge off + eval 100 + auto-rollback + sweep tier fix  # 2026-09-07 (v1.14.3): vector_store conn property detects closed-but-not-None and auto-reopens
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


