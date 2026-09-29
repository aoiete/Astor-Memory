"""
astor-memory: Self-owned memory system for AI agents.

3-store triplet (bus + forge + nest) with 3-tier isolation.

See: docs/architecture.md in this repository for the full architecture overview.
"""

__version__ = "1.15.47"  # 2026-09-28 (Ship P0: Hindsight-style Memory Defense. 44-pattern PII scanner (API keys / crypto / emails / phones / chat IDs / tokens / private keys) wired into /v1/write via body['pii_scan'] or ASTOR_PII_SCAN_ON_WRITE=1 env gate. Two policies: 'redact' (default — replace with [BLOCKED:NAME] tag, write proceeds) and 'block' (HTTP 400 with match details, write rejected). Audit-safe via fingerprint() = sha256[:12] (no raw secrets stored in stderr logs). 30 unit tests pass (test_memory_defense.py). Live verified: block policy rejects sk-... keys with structured 400 response. astor_sync.py --src-to-rt syncs the new module + wired server.py to runtime. Ship L: MMR diversity rerank...) TTL was scoped out: astor's bus already serves long-term recall; LLM session context is the working-memory layer; adding a TTL layer would be redundant. Ship M: MMR lambda knob...) mmr_lambda body field overrides ASTOR_MMR_LAMBDA env which overrides mmr_rerank.DEFAULT_LAMBDA. Clamped to [0.0, 1.0]. 1.0 = pure relevance (no MMR); 0.0 = pure diversity. Eval sweep on 110 queries confirms lambda in [0.5, 0.9] is statistically indistinguishable — defaults unchanged. The MMR-on vs MMR-off gap (lifestyle mrr 0.65 vs 0.56, +0.09) is the real win; lambda fine-tuning is secondary. 4 new eval_runner variants (mmr_lambda_05/07/09/off) for future sweeps. No unit tests needed (no new function logic, only parameter plumbing). Ship L: MMR diversity rerank...) Carbonell-Goldstein MMR (lambda=0.7) applied after hybrid_merge to evict near-duplicate phrasings from candidate pool. Diversity metric: token Jaccard on fact content (each CJK char = own token, matches lex_index._tokenize). Pure-token, no LLM cost. Env gate ASTOR_MMR=0 to disable. Triggered when len(merged)>top_k; otherwise hybrid score order is preserved. 24 unit tests pass (test_mmr_reranker.py). Live verified: lifestyle mrr improves after eval baseline re-run. (Ship K: multi-language query expansion...) synonym_expander.py now supports CJK + mixed scripts — 30-entry Chinese synonym dict (健身↔锻炼↔运动, 八字↔四柱, RAG↔知识库↔corpus, etc.), CJK bi-gram anchors for synonym-barren short queries, optional LLM fallback gated by ASTOR_LLM_EXPAND env var. Backward-compatible signature. Enables Chinese short queries to fan out into 2-3 variants instead of single-route cosine. 30 unit tests pass (test_synonym_expander_chinese.py). Live verified: /v1/read 'RAG 知识库 bge-reranker' surfaces fid=9580 first hit. lifestyle mrr unchanged at 0.556; 8 lifestyle queries already matched at ranks 1-7 (rank issue is next ship — MMR rerank).) (v1.15.17: S21 auto-meta-recall. /v1/read now auto-injects top success_pattern + failure_pattern facts into every recall response — caller sees "what worked before / what failed before" without explicitly asking. /v1/audit/health exposes meta_recall_stats counter so dashboard can verify the gate is firing. Verified live: query "ship success verified" returns first result id=12736 kind=success_pattern meta_source=auto-meta-recall-v1.15.17. 18 dashboard tests pass.)      (v1.15.47: Ship F.1 decay sweep skip-list — mental_model + knowledge_page excluded from 90d no-recall tombstone. Operator-authored surfaces never decay; they refresh via explicit CLI upsert. 2 SQL filters guarded in server.py.) (v1.15.46: Ship E.1 /v1/staleness standalone endpoint — operators query tier+threshold+kind filter to find references needing refresh. Returns items sorted by age_days desc with stale_count aggregate. Reuses dashboard_data._compute_staleness helper.) (v1.15.45: Ship P0.1b Memory Defense audit_log integration — PII scan results now persist to bus.audit_log (severity=critical for block, warning for redact). Operators can query history via /v1/audit or direct SQL.) (v1.15.44: Ship P3.1g reflection skip-list — mental_model + knowledge_page are operator-authored, never tombstoned by reflection merge-loser. 2 reflection paths + 1 SQL filter guarded. 184→186 tests pass.)
# 2026-09-25 (v1.15.16: S18+S19 dashboard auto-refresh. S18 — /v1/dashboard cache TTL 300s→30s + /v1/write success-path invalidates _DASHBOARD_CACHE, so hero.last_event_ts refreshes within 30s (or instantly after writes). S19 — build_dashboard_payload unions MAX(events.ts) from public + source buses; _growth_30d aggregates promoted counts across admin + public + source tiers (was admin-only). Verified: dashboard now shows live public bus ts + cross-tier growth counts. 18 dashboard tests pass.)  # 2026-09-25 (v1.15.15: forget(tombstone_only=True) now also deletes nest embeddings — previously vector recall kept surfacing tombstoned facts, verified live with fact 12576. Embeddings are regenerable from content, so delete on every forget path.)  # 2026-09-23 (v1.15.14: astor_capture_intent routes through /v1/write — success_pattern facts now land in memory_canonical)  # 2026-09-22 (Ship v1.15.8 — RRSI pass c: edit-budget alias + roadmap spec. NEW CLI flag `am decay-sweep run --max-sweep-size N` is an alias for `--limit N`, named to match the RRSI paper's "edit budget per round" concept. Same SQL, same audit, same behavior; just the paper's vocabulary. Also: NEW docs/rrsi-roadmap.md — full spec of which of the 7 RRSI constraints are shipped (a=leakage audit, b=noise floor, c=edit budget alias) vs deferred (2=ledger rationale, 3=stagnation, 6=cost rule, 7=pruning). Deferred constraints need paired eval+token tracking or operator-side tooling not yet built; ship decision documented per R12481 (wait for post-ship data before claiming transfer) + R11887 (n<20 statistically unreliable). 468 tests pass. Verified: --max-sweep-size 5 caps eligible to 5 (legacy full sweep returns 310 at max_imp=1.0 max_access=5).)

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


