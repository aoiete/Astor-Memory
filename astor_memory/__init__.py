"""


astor-memory: Self-owned memory system for AI agents.





3-store triplet (bus + forge + nest) with 3-tier isolation.





See: docs/architecture.md in this repository for the full architecture overview.


"""





__version__ = "1.17.17"
# v1.17.15: the read cache was hiding the very thing the eval harness measures. _READ_CACHE's key covered query/tier/user/top_k/kinds/wing/session_id/consult/peer_fanout, but not the knobs that change the RANKING: hybrid, bm25_weight, vec_weight, rerank, mmr_lambda, hyde. So a read with bm25_weight=0.4 was served the cached response of an earlier read with the default 0.6 -- the second variant never ran. Measured on the live corpus: every variant of the held-out gate returned identical fids and identical mrr, which looked like "the variants have no effect" when in fact the variant was never executed. Every variant comparison in eval_runner and eval_sweep was measuring the first variant over and over. The key now covers every input that changes the result. Separately, RSIHub's boundary condition: the evaluator belongs to the framework, so a candidate must not be able to raise its score by editing the scoring rules -- eval_runner now records scorer_hash (the scoring code) alongside eval_set_hash (the questions), and a new held-out gate (tests/holdout_gate.py over tests/eval_holdout_set.json, 40 queries verified live and disjoint from eval_set by construction) asks whether an improvement transferred to questions the tuning never saw. The held-out set was previously absent: eval_control_set.json reuses 38 of eval_set's 39 question texts, so it is a second assertion over the same questions, not a held-out set.  # v1.17.0: the memory became readable. Astor stores facts in SQLite, which is good for querying and bad for reading -- you cannot open it, you cannot diff it, and you cannot hand it to another tool. The Agent Memory Repo format (Cognition, 2026-10-05) picks the opposite trade: plain Markdown in a Git repository. `am export` projects the database into that shape -- MEMORY.md as the short entry point, one bullet per memory with open [key: value] tags, [[wiki links]] between memories that share an entity, and a tombstoned.md that separates what was judged wrong from what was merely relocated. The database stays the source of truth; the files are a projection and the next export overwrites them. Two things the database already tracks and the plain format does not: provenance (where each memory came from, not just when) and time (validity window, whether a later revision replaced it), so a reader can tell a live rule from a stale one without trusting the prose.
# v1.16.99: the Hermes installer promised four steps in the docs and did one.
# `am install --ide=hermes` only rewrote memory.provider in config.yaml -- it never
# created the plugin, so a fresh install left hermes reporting the provider as
# selected while nothing was there to load. The install now writes the plugin
# (bundled location when a hermes-agent tree exists, user location otherwise),
# writes plugin.yaml, and in priority mode also switches the built-in file memory
# off. Two guards: an existing plugin is never overwritten, because provider
# plugins get hand-edited and replacing a working one is a downgrade; and
# config.yaml is backed up before the first edit.
# v1.16.98: a deletion without a cause teaches the system nothing, and a miss without an owner sends you to fix the wrong thing. Uni-Mem (arXiv 2610.29671) localises credit to the responsible events and propagates it along the update lineage; astor had the invalidated_reason column and left it empty on 95%+ of the dead rows, on six tombstone paths including /v1/forget. All six now record a cause, and /v1/forget accepts a reason. Separately, every metric astor had was a retrieval metric, so a miss could mean the fact was never written or that the search missed it -- opposite fixes. The new eval_oracle.py asks the store what it holds without the ranking layer, so the harness reports oracle_hit_rate, storage_gap_rate and retrieval_gap_rate. On the live corpus: hit_rate 0.981 against a ceiling of 1.000, storage gap 0.000, retrieval gap 0.019 -- the ranking is within 2% of the ceiling, so more BM25/rerank/MMR tuning buys almost nothing.  # v1.16.97: the debug lines were unconditional, and what they printed was the user's own data. Every /v1/read wrote two lines to stderr ([DEBUG-MM] and [DEBUG-KP]) and every /v1/write wrote four or five, and those lines carried the first 30 characters of the query and the first 80 characters of the text being written. So the volume was noise and the content was a privacy leak: whatever you asked the memory about ended up in the server log without being asked. All seven sites now go through _debug_write(), which is off unless ASTOR_DEBUG=1.  # v1.16.96: two adapter tests failed on CI (test_recall_hybrid_default: count 0, and test_forget_via_query: "no BM25 hit") while passing locally. Both were borrowing their fixtures from a neighbour: the recall test expected *something* to have left a fact in the runtime it reads -- locally the stale ~/.astor tree (9.6 MB of history), on CI nothing at all. And the forget test wrote over REST while the lexical index lagged the canonical write, so the BM25 lookup missed. A test must not depend on the order it runs in: the recall test now seeds its own fact through the REST write path (the one that promotes candidates into memory_canonical and the lexical index), and the forget test retries -- a forget that finds nothing tombstones nothing, so retrying is safe. Verified by running the whole suite against a throwaway server on a clean ASTOR_DIR, i.e. the CI shape: 1068 passed, 0 failed.  # v1.16.95: a missing optional dependency took the whole recall API down, and test/debug residue was allowed to land in the shared tiers. (1) /v1/read called astor_get_embedding_model() unguarded -- when fastembed/onnxruntime was missing (wrong interpreter, deleted model files, onnxruntime crash) the whole endpoint 500'd instead of degrading, even though the warmup path already promised "astor runs without it; semantic recall is disabled". Now the embedding call is wrapped: on failure vec_weight drops to 0 and recall continues BM25-only. (2) HaluMem (arXiv 2511.03506) FMR measurement found 56.9% of the public layer's kind='fact' rows were e2e/debug residue -- 301 of them written by tests/test_bitemporal_e2e.py, which posts to the LIVE server with tier=public and so bypasses the conftest ASTOR_DIR isolation. Their access_count reached 500: they were polluting every recall. /v1/write now auto-demotes test residue to the caller's own private bucket (new astor_memory/forge/hygiene_rules.py, shared with tests/memory_hygiene.py).  # v1.16.93: the request teardown was closing a shared handle. astor_bus() and astor_nest() hand out process-wide singletons, so a request that finished closed the SQLite connection every other concurrent request was using; the in-flight query died with "Cannot operate on a closed database", the request 500'd, and the watchdog then killed a healthy server -- clients saw WinError 10054/10061. A 120-request concurrent load failed 94 before and 0 after. The watchdog's netstat probe also read its own timeout as "the server is dead"; a probe that cannot answer is UNKNOWN, not DEAD. v1.16.92 gave memory a temporary layer. scope_type now carries an evidence-gated tier (scope_policy.py) and short_term facts get an expiry, renewed on recall, promoted after repeated confirmation and demoted when they expire (scope_lifecycle.py) -- the schema allowed all of it and no code used it. Two defects surfaced while wiring it up: a caller's importance was discarded (astor_extract_facts takes no importance argument, so everything was stored at 0.5 and the extractor's own heuristic never took effect), and the success patterns lacked word boundaries, so "unverified" was detected as a verified recipe.


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








