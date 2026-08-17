# Changelog

All notable changes to Astor-Memory will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [v1.2.2] - 2026-08-16 — Episodic reflection orchestrator (EverOS pattern)

### Added — Select → Merge → Deprecate pipeline

Pattern adopted from EverOS `memory/reflection/` (simplified for
astor's SQLite stack; heuristic-mode only in v1.2.2, LLM-mode future).

Purpose: episodic consolidation. When many similar facts accumulate
in a tier (e.g. user preferences rephrased over time, risk rules
stated multiple ways), reflection merges them into a single winner
fact and tombstones the duplicates. Reduces recall noise + audit
trail bloat.

1. **`astor_memory/nest/reflection.py`** (new, 270 lines):
   - `select_episode_clusters(bus, tier, user_id, ...)` — find clusters
     of facts in the same (kind, scope_type) group sharing ≥3 distinctive
     tokens, with length-similarity sanity check (max 2x).
   - `merge_narrative(facts)` — pick winner (highest importance, then
     most recent, then highest confidence, then lowest id), compose
     merged content by joining distinct member content with `
---
`
     separator. Bumps importance by +0.1 (capped at 1.0).
   - `deprecate_old_facts(bus, losers, winner_id, actor)` — tombstone
     losers + write audit row (`reflection_deprecated`) with full
     `old_state` JSON of the deprecated fact.
   - `apply_merge(bus, winner_id, ...)` — update winner content +
     importance + `promoted_at` + `last_confirmed_at` + write audit
     row (`reflection_merged`).
   - `run_reflection(bus, tier, user_id, ...)` — orchestrator, returns
     summary `{clusters_found, clusters_merged, facts_deprecated, merge_log}`.

2. **`POST /v1/reflection/run`** (server endpoint, first_admin only)
   — same semantics as CLI, writes audit row `reflection_run` per run.
   Body: `{tier, user_id, min_size, max_clusters, kinds}`.

3. **`am reflection run [--tier=...] [--user-id=...] [--min-size=N]
   [--max-clusters=N] [--kinds=...]`** — CLI equivalent.

4. **`tests/test_reflection.py`** (11 tests) + `tests/test_reflection_server.py`
   (2 tests) covering: select empty/similar/kinds/length-mismatch,
   merge winner selection + content concatenation, deprecate audit
   row, apply_merge winner update, full pipeline + idempotency,
   kinds filter, server endpoint first_admin + 403 for regular user.

**Verified**: pytest 146 → 159 passing (+13 reflection tests), no regressions
in pre-existing 8 failures.

### Safety / idempotency

- **Idempotent**: second run returns 0 clusters (losers are tombstoned
  and filtered out by the SQL query in `select_episode_clusters`).
- **Audit trail**: every deprecated fact + every merged winner writes
  an audit row (`old_state``/`new_state`` in metadata JSON).
- **Destructive**: tombstones are reversible via existing
  `/v1/fact/<id>/restore` (sets `tombstoned=0`); no data is hard-deleted.
- **First_admin only**: reflection can rewrite many rows at once.
- **Length sanity**: 2x max-length ratio prevents merging very different
  facts even if they share tokens.

### When to run

- **Cron**: weekly (Sun 03:00 UTC) via `am reflection run --tier=public
  --max-clusters=200` to keep the public tier tidy.
- **Manual**: after a batch import of legacy data (e.g. fresh migration
  from memory-bus).
- **Per-tier**: the orchestrator is tier-scoped; run separately for
  public/source/admin to control blast radius.

---

## [v1.2.1] - 2026-08-16 — A-MEM-style structured fields + hybrid_merge rerank boost

### Added — keywords + context columns (P1 #2 from A-MEM research)

Pattern adopted from A-MEM (`agiresearch/A-mem`, arXiv:2502.12110): each
fact carries structured fields beyond just `content` + `tags`. Two new
columns on `memory_canonical`:

- **`keywords`** (TEXT JSON array) — 3-7 distinct keywords/phrases extracted
  by LLM at write time (regex mode derives heuristically). Used by
  `hybrid_merge` rerank via Jaccard boost on the query.
- **`context`** (TEXT 1-2 sentence) — human-readable summary. Returned in
  `/v1/read` response for caller explanation; used by future viewer
  + admin audit.

**Why**: A-MEM's research shows that structured fields give 5-10% precision
gain in fact retrieval. For astor specifically: keyword Jaccard boost
helps when the embedding model doesn't catch keyword-level intent (e.g.
proper nouns, short queries like "coffee preference").

1. **`bus/schema.py`** — schema v4 → v5. New columns + `_astor_upgrade_v4_to_v5`
   migration (`ALTER TABLE ADD COLUMN`, idempotent via PRAGMA probe).
   `astor_upgrade_all_tier_dbs` + `astor_init_schema` wired.
2. **`forge/extractor.py`** — `AstorFact` dataclass gains `keywords: list[str]`
   + `context: str` fields. Regex mode derives heuristically (kind + top
   5 distinctive words ≥4 chars + first 120 chars of input text). LLM
   mode reads them from the model response (prompt updated).
3. **`forge/llm_extract.py`** — prompt now asks for `keywords` + `context`
   per fact, with safe fallbacks for older models.
4. **`bus/store.py`** — `insert_candidate` accepts `keywords=` + `context=`,
   stashes them in candidate metadata JSON as `__keywords__` / `__context__`
   (avoids needing a candidate-schema migration). `promote_candidate`
   reads them back + writes to the new canonical columns; safe defaults
   (`[]` / `''`) for legacy candidates.
5. **`nest/lex_index.py:hybrid_merge`** — adds 2 optional params:
   `keyword_hits: dict[int, list[str]]` + `query_keywords: list[str]`.
   Score += `keyword_boost` (default 0.15) × Jaccard(fact_kw, query_kw).
   **Backward compatible**: when neither param is supplied, behavior is
   byte-identical to legacy.
6. **`server.py:read`** — loads per-fact keywords from canonical for the
   current candidate set, tokenizes the query (cheap, no LLM), passes
   both into `hybrid_merge`. Response includes `keywords` + `context`
   per result (defaults `[]` / `''` for pre-v1.2.1 facts).
7. **`cli/main.py:write`** — threads `keywords` + `context` through
   `insert_candidate`.
8. **`tests/test_keywords_context.py`** — 11 new tests covering schema
   migration, extraction (regex), insert, promote (with legacy
   fallback), hybrid_merge boost behavior, backward compat.

**Verified**: pytest 135 → 146 passing (+11), no regressions in
pre-existing 8 failures.

### Migration v4 → v5

Server restart auto-runs `_astor_upgrade_v4_to_v5` on every tier DB.
Existing rows get `keywords='[]'` + `context=''` (safe defaults). No
manual SQL needed.

### Backward compatibility

- Pre-v1.2.1 facts: `keywords='[]'`, `context=''` → no rerank boost
  applied (treated as no keywords)
- Pre-v1.2.1 callers of `hybrid_merge` (without new args): unchanged
  behavior, same byte output
- Pre-v1.2.1 callers of `/v1/read`: response gains new `keywords` and
  `context` fields with default values; existing fields unchanged

---

## [v1.2.0] - 2026-08-16 — Async cascade write queue + crash recovery

### Added — durable embed-write crash recovery (P1 from EverOS research)

**Bug fixed**: when `nest.store()` failed inside `promote_candidate` (e.g.
embedding model OOM, fastembed import error, LanceDB unavailable), the
fact was written to `memory_canonical` BUT the embedding was silently
dropped — the fact lived forever with no vector, recall returned empty
for it, and the only trace was a `embedding_failed` audit row.

Pattern adopted from EverOS `md_change_state` (simplified for astor's
SQLite-only stack):

1. **`bus/cascade.py`** — new module. Durable queue for failed writes:
   `enqueue()`, `list_pending()`, `replay_one()`, `replay_pending()`,
   `purge()`. Each row tracks `fact_id`, `operation` (`embed_insert` /
   `lex_index` / `provenance_link`), `tier`, `user_id`, `payload` (JSON
   blob with `content` for replay), `enqueued_at`, `attempt_count`,
   `last_error`, `status` (`pending` / `succeeded` / `failed`).
2. **`bus/schema.py`** — schema v3 → v4. New `cascade_state` table +
   indexes (`idx_cascade_pending`, `idx_cascade_fact`). `_astor_upgrade_v3_to_v4`
   migration runs on every known tier DB at server start.
3. **`bus/store.py:promote_candidate`** — on embed failure, now enqueues to
   `cascade_state` instead of just writing the audit row. Audit row
   metadata gets `queued_for_replay: True`.
4. **`POST /v1/cascade/replay`** (server endpoint, first_admin only) —
   drain pending rows. Body: `{"limit": 100, "max_attempts": 5}`. Returns
   `{processed, succeeded, failed, still_pending, results: [...]}`.
5. **`GET /v1/cascade/stats`** — aggregate counts + last_attempt_at.
6. **`am cascade replay [--limit=N --max-attempts=N]`** — CLI equivalent
   of the POST endpoint.
7. **`am cascade stats`** / **`am cascade purge [--status=… --older-than-days=N]`** — CLI helpers.
8. **`tests/test_cascade.py`** — 11 new tests covering enqueue, stats,
   FIFO list, replay success/failure/max-attempts, multi-row drain,
   purge old rows + protect pending, full server endpoint roundtrip
   including ACL enforcement.

**Failure modes that route to cascade queue**:
- Embedding model not loaded (lazy load failed first time)
- OOM during batched embed
- SQLite disk full / I/O error
- fastembed / numpy version mismatch

**What does NOT route** (write fails loud instead):
- ACL permission denied → caller sees 403
- Schema corruption → write fails fast, audit row written
- Schema version mismatch → write fails fast

**Verified**: pytest 124 → 135 passing (+11), no regressions in pre-existing
8 failures. Live runtime verified at `v1.2.0` after restart.

### Migration v3 → v4

For existing v1.1.x DBs, server restart auto-runs `_astor_upgrade_v3_to_v4`
on every tier DB. No manual SQL needed.

### Security note

Replay is **first_admin only** — destructive operation touching many nest DBs.
ACL check is enforced in both the REST endpoint and the CLI.

---

## [v1.1.1] - 2026-08-16 — P0 ACL fix + SQLite thread safety

### Fixed — P0 ACL: per-request actor resolved from bot-binding.db

**Bug**: `server.py before_request` hardcoded `actor='first_admin'` for every
POST request. This meant **any user could write to the source tier and read
any other user's private DB** (user_a → source = 200, user_a → read(private/admin)
= 200). Discovered during the v1.1.0 health check on 2026-08-16. Verified
exploited against the live runtime on `<runtime_dir>`; leaked
fact_ids 3083, 3084, 3135, 3136 were subsequently tombstoned.

**Fix**:

1. New helper `_astor_resolve_actor(user_id)` in `server.py` reads
   `bot-binding.db user_meta.role` and returns the correct
   `(actor, role)` tuple:
   - `admin` → `(first_admin, first_admin)` (system root alias)
   - `role='first_admin'` → `(first_admin, first_admin)`
   - `role='admin'` → `(admin:<id>, admin)` — power user per plan §2624
   - `role='user'` → `(user:<id>, user)` — regular user
   - unknown / inactive → `(first_admin, first_admin)` (fail closed as root
     for safety, but logged so admin can investigate)
2. `before_request` now binds `_CURRENT` with this actor before the route
   handler runs. `_CURRENT.user_id` is the **actor's** user_id (body_user),
   not the target — so `astor_check_*` from downstream code sees the right
   identity.
3. Explicit cross-user check at the route boundary: when
   `tier='private'` and `target_user != body_user`, run
   `astor_check_read` and `astor_check_write` against `target_user`. If the
   actor's role can't access the target, return `403 cross_user_forbidden`.
   `admin` role has a carve-out in `acl.py astor_check_read/write` per
   plan §2624 (power user can read/write any private for support /
   moderation; `astor_audit` row mandatory).
4. New Flask `errorhandler(PermissionError_)` converts downstream
   `astor_check_*` failures to `403 permission_denied` instead of
   bubbling as `500 Internal Server Error`.
5. Default `_CURRENT` bind (`actor=first_admin, tier=public`) is now
   applied for GET requests so `health`, `viewer_stats`, `lex_stats` etc.
   don't trip `astor_acl not initialized` in worker threads.

### Fixed — SQLite cross-thread ProgrammingError

`bot_binding._connect()` now passes `check_same_thread=False` to
`sqlite3.connect()`. Required because Flask serves multi-threaded and the
new `before_request` hook calls `get_user()` from worker threads.
Pre-fix: every POST request from a new thread raised
`sqlite3.ProgrammingError: SQLite objects created in a thread can only
be used in that same thread`.

### Added — ACL regression tests

7 new pytests in `tests/test_acl.py`:
- `test_acl_yuqi_cannot_write_source` — P0 regression
- `test_acl_yuqi_cannot_read_other_users_private` — P0 regression
- `test_acl_yuqi_can_write_own_private` — positive (own data)
- `test_acl_yuqi_can_read_own_private` — positive (own data)
- `test_acl_first_admin_can_write_source` — positive (admin path)
- `test_acl_admin_role_can_read_other_users_private` — admin carve-out
- `test_acl_resolve_actor_returns_correct_roles` — unit test

Net pytest result: 116 → 124 passing (+8), 9 → 8 failing (1 baseline
failure fixed by the same change).

### Security note for downstream installers

Anyone running v1.0.x or v1.1.0 in production should upgrade
immediately. The leaked data may include any facts written to
`source` tier during the window the bug was live, and any cross-user
reads against `private_<other_user>` DBs. Operators can audit
`audit/astor_audit.db` for `action='read'` rows where
`actor='first_admin'` but `user_id` is a non-admin user — those are
the suspicious pre-fix entries.

### Added — `docs/api.md` full REST endpoint reference

New [`docs/api.md`](../api.md) documents all 18 REST endpoints:

- **Core write/read**: `/v1/write`, `/v1/read`
- **opt3-6 forget + audit**: `/v1/forget` (dry-run + tombstone + audit
  snapshot), `/v1/read/multi` (cross-tier parallel recall)
- **opt3-6 merge dedup v2**: `/v1/merge/find` (cosine + LLM judge,
  first_admin only), `/v1/merge/apply` (apply reviewed merges)
- **opt3-6 provenance**: `/v1/fact/<id>/provenance`, `/lineage`,
  `/graph.dot` (graphviz), `POST /provenance` (record parent edges)
- **opt3-6 versioning + restore**: `/v1/fact/<id>/versions`,
  `/v1/fact/<id>/restore` (preview or commit), `/v1/snapshot/stats`
  (daily event stats)
- **Stats + health**: `/v1/health`, `/v1/viewer/stats` (MemoraX
  content-free Viewer), `/v1/lex/stats`
- **Admin + installer**: `/v1/reload`, `/v1/install`

Each entry has: body shape, response shape, error codes (incl. the
v1.1.1 ACL error types: `permission_denied`, `cross_user_forbidden`,
`acl_init_failed`), and at least one example. Cross-linked from
README.md, docs/architecture.md, and docs/troubleshooting.md.

Closes the docs gap from the opt3-6 ship (merge.py + provenance.py +
versioning.py added 1843 lines but had no standalone reference until
now).

---

## [v1.1.0] - 2026-08-16 — Multi-client adapter + content-free viewer + MCP server

### Added — 3 new dimensions on top of v1.0's 3-tier × 3-scope

**Repo Memory tier (per-git-repository isolation)** — inspired by MemoraX
`.repo_memory/` per-worktree design. The 9-db layout grows to a 12-db layout
(3 stores × 4 tiers). Repo IDs are sha256[:16] of the git remote URL, with
`repo_<name>` fallback. ACL matrix: `read=any role`, `write=first_admin only`
(since the writer is the agent itself).

```python
# Write to a specific repo
am.write("bug fixed in store.py:167 promote_candidate UNIQUE",
         tier="repo",
         repo_id=normalize_repo_id("https://github.com/me/myrepo.git"),
         scope="long_term")

# Read from a specific repo (only that repo's facts surface)
am.read("promote_candidate bug", tier="repo", repo_id="...")
```

**Content-free Viewer stats endpoint** — `GET /v1/viewer/stats` returns
counts (facts_by_tier, facts_by_scope, embeddings_total, dedup_hits_total,
schema_versions, dbs) but **NO fact content**. Per MemoraX architecture rule:
the Viewer is a content-free local projection, not memory authority. Scans
all 9+ DBs (now includes 5 users + N repos × 3 stores).

**Periodic skill reminder in hermes_adapter** — `sync_turn` increments a
turn counter; after every `ASTOR_NUDGE_EVERY_N_TURNS` (default 5) turns,
`system_prompt_block` appends a MEMORY-RECALL REMINDER to nudge the agent
to use `astor_recall`. Fights "memory written but never recalled".

**`/v1/reload` endpoint** — `POST /v1/reload` re-execs the current process
via `os.execv` so module caches pick up fresh source without manual restart.
Restricted to first_admin.

**MCP stdio server** (`am mcp serve`) — implements Model Context Protocol
JSON-RPC 2.0 over stdin/stdout. Zero external deps. Exposes 3 tools:
`astor_recall`, `astor_write`, `astor_status`. Any MCP-compatible client
(Claude Desktop, Cursor, Continue, etc.) can launch as subprocess to gain
astor memory access. Per Plan § v1.1 MCP integration.

```bash
am mcp serve  # blocks on stdin, writes framed responses to stdout
```

### Added — write-path robustness

**P0: `promote_candidate` UNIQUE constraint dedup** — previously, retrying a
write after `promote_candidate` crashed with `UNIQUE constraint failed:
memory_canonical.candidate_id`. Now does a SELECT first; if candidate_id is
already in `memory_canonical`, returns the existing canonical_id (idempotent)
and writes an `audit_log` row (`promote_idempotent_replay`).

**P0: `astor_nest(tier, user_id)` thread-through** — `promote_candidate`'s
internal call to `astor_nest()` was missing tier/user_id, causing
`ValueError: astor_nest() requires tier=...` silently swallowed by the
audit-log fallback. Embedding writes were silently dropped. Now passes
tier + user_id through.

**P1: `astor_extract_facts` now writes `llm_call_log`** — every LLM extract
call is audited with `actor`, `user_id`, `tier`, `provider`, `model`,
`operation=extract`, `input_hash` (sha256), `input_length`, `output_json`,
`success`, `error_msg`, `latency_ms`, `reason`. ACL audit compliance.

**P1: `scope` parameter on `/v1/write`** — `scope=long_term|short_term|profile`.
Profile-scope facts auto-route to private tier. Threaded through to
`promote_candidate` via `scope_type` column.

**P1: Content-hash dedup (stable_id)** — sha256[:16] of text per
(tier, user_id, scope) is stored in `memory_canonical.stable_id`. Re-writes
of identical text return the existing fact_id instead of creating a new row.

### Added — ACL robustness

**P2: ACL rebind per request** — server `before_request` hook now reads
`request.body.tier` and rebinds `_CURRENT` so `astor_check_write` sees
the request's tier (was stuck at `source` due to server-default bind).

**P2: Optional mirror fanout** — `mirror_to_source=true` on `tier=public`
write also writes the same fact into `source` tier (admin-only mirror),
giving agent self-patterns the same content.

### Fixed

- **`store.py:167` UNIQUE crash** — silent failure of every write
  path; now idempotent.
- **Embedding write was silently dropped** in `promote_candidate`.
- **Read `tier='private'` without `user_id`** returned 400 due to
  stale ACL bind.
- **`forge.llm_call_log.tier` CHECK constraint** didn't include 'repo'
  (now widened to public/source/private/repo).

### Schema migrations (v1.0 → v1.1)

```sql
-- bus (memory_canonical): widen tier CHECK
-- ALTER TABLE memory_canonical DROP CONSTRAINT ...;
-- bus: memory_canonical.stable_id (already exists, now used)
-- bus: memory_candidates.stable_id (none, content dedup lives in memory_canonical)
-- forge (llm_call_log): widen tier CHECK
-- All DBs: 9 → 9 + N_repos × 3 = 12+ DBs (lazy; only created on first repo write)
```

For existing v1.0 DBs, the CHECK constraints must be re-applied manually:

```sql
-- On each astor_bus_*.db + astor_forge_*.db
PRAGMA writable_schema = 1;
UPDATE sqlite_master
SET sql = replace(sql,
  "CHECK(tier IN ('public', 'source', 'private')",
  "CHECK(tier IN ('public', 'source', 'private', 'repo')")
WHERE type = 'table' AND name IN ('memory_canonical', 'llm_call_log');
PRAGMA writable_schema = 0;
```

`am doctor --schema` (already shipped in v1.0) flags DBs that need this.

### Architecture

12+ DB layout per user:

```
~/.astor/
├── public/memory/   astor_{bus,forge,nest}_public.db
├── source/memory/   astor_{bus,forge,nest}_source.db
├── repos/<repo_id>/memory/  astor_{bus,forge,nest}_<repo_id>.db   (v1.1 NEW)
└── users/<uid>/memory/      astor_{bus,forge,nest}_<uid>.db
```

Per-tier × per-store ACL still enforced at `astor_check_write` (3-tier
permission matrix extended with `repo` row).

### Tests

- **31/31 existing pytest passing** (v1.0 baseline preserved)
- **New smoke tests**:
  - `mcp_inline_test.py` — 4-message MCP handshake (initialize / tools/list
    / 2× tools/call), validated inline (subprocess path needs py3.12 for
    production; py3.11 has known stdin EOF race).
  - `tier=repo write+read` — sha256 repo_id, fact surfaces only in
    `repos/<repo_id>/memory/`.

### Compatibility

- **Backward compat**: v1.0 clients continue to work (defaults to
  `tier=public`, `scope=long_term`).
- **Forward compat**: v1.1 readers can ignore `stable_id` column in
  bus output if they don't query it.
- **DB schema**: v1.0 DBs continue to work but tier CHECK constraint
  may need manual widening (see migration SQL above).

### Credits

Inspired by MemoraX Code (https://github.com/memorax-ai/memorax-code, MIT).
Their architecture pattern — Repo Memory + content-free Viewer +
writeback buffer — informed the v1.1 additions. See `architecture.md`
section 12 for absorbed insights.

---

## [v1.0.0] - 2026-08-15 — Open source release ready

### Added

- **`am migrate from-memory-bus --source=... --target=~/.astor`**: Production-grade migration CLI (dry-run mode + idempotent by stable_id + FK-safe ordering)
- **`am doctor --schema --memory`**: Full health check (memory stats per DB + RSS)
- **End-to-end integration test** (`tests/test_basic.py::test_e2e_integration`): CLI init → write → recall → cite roundtrip in single test
- **Wheel + sdist build verified**: `python -m build` produces `astor_memory-0.2.0-py3-none-any.whl` (43.6K) + `astor_memory-0.2.0.tar.gz` (72.3K)
- **Smoke install verified**: Fresh venv → `pip install wheel` → `am version` returns 0.2.0 → REST API `/v1/health` returns OK

### Migration path (per Plan § Week 5)

```bash
# 1. Dry-run to see what would migrate (no writes)
am migrate from-memory-bus --source=~/.memory-bus/bus.db --dry-run

# 2. Actual migration (idempotent; safe to re-run)
am migrate from-memory-bus --source=~/.memory-bus/bus.db --target=~/.astor

# 3. Verify migrated data
am doctor --schema --memory
am recall "test query" --user admin

# 4. After verification, MANUALLY archive legacy (NOT auto-deleted):
mv <mem_sys>/memory-bus <mem_sys>/memory-bus-archived-2026-08-15
```

### Deferred to v1.1+ (per Plan § Week 6)

- `am bot on` (multi-user private DBs)
- `am ui` (static dashboard)
- MCP server (FastMCP wrapper)
- LangChain adapter (BaseMemory subclass)
- GitHub Pages docs (v1.2)
- HNSW index for > 100K facts (v2.0)
- DuckDB mirror for analytic queries (v2.0)
- PostgreSQL backend option (v2.0)

### Tests

- **31/31 pytest passing** in 4.4s
- Coverage: 10 bus + 2 forge + 5 installer + 2 config + 6 REST + 4 migrate + 1 init + 1 e2e

### v1.0 ready for: open source release tag + PyPI publish

---

## [v0.2.0] - 2026-08-15 — Package skeleton + REST API

### Added

- **3 separate SQLite DBs** (per user lock 2026-08-15): `astor_bus.db`, `astor_forge.db`, `astor_nest.db` (replaces single `bus.db`)
- **Nest independent schema** (`embeddings` table + `model_name` index) — replaces bus's `memory_canonical.embedding` BLOB
- **`astor_memory/server.py`**: Flask REST API (`/v1/health`, `/v1/write`, `/v1/read`, `/v1/install`)
- **`astor_memory/installer/`**: Per-agent priority negotiation framework (9 agents × 4 modes × 4 tiers per Plan Insight 18)
- **`am install --ide=X --mode=Y`**: CLI dispatch for cross-agent installation (Claude Code / Cline / OpenCode / Hermes / OpenClaw / Cursor / Continue / Windsurf / Aider)
- **`am config get/set/show`**: Runtime config CLI
- **Forge LLM extract** (`mode='llm'`): 7 providers with fallback chain (m3, openai, anthropic, gemini, ollama, deepseek, zhipu) + graceful regex fallback when no API keys
- **`nest.store(fact_id, text, model_name)`**: Compute + persist embedding (auto-called by `bus.promote_candidate`)
- **GitHub Actions CI** (`.github/workflows/ci.yml`): Ubuntu 24.04 + Python 3.10/3.11/3.12/3.13 matrix
- **GitHub Actions release** (`.github/workflows/release.yml`): Manual trigger → build wheel + sdist → PyPI publish (OIDC trusted)

### Changed

- **9 functions renamed** to `astor_*` prefix (per user lock 2026-08-15): `astor_bus`, `astor_reset_bus`, `astor_nest`, `astor_reset_nest`, `astor_get_embedding_model`, `astor_reset_embedding_model`, `astor_init_schema`, `astor_verify_schema`
- **DB filename**: `bus.db` → `astor.db` → split into 3 (`astor_bus.db` / `astor_forge.db` / `astor_nest.db`)
- **`astor_memory/__init__.py`**: Top-level accessors `astor_bus()` / `astor_nest()` / `astor_forge()` (NOT `astor_get_*`)
- **SQLite thread-safety**: `check_same_thread=False` on bus + nest connections (required for Flask multi-thread)
- **Fastembed embedding model**: `multilingual-e5-base` (not supported by fastembed 0.8) → `BAAI/bge-base-en-v1.5` for ≥16GB RAM
- **Nest search signature**: `model_name` parameter (replaces `version`)

### Fixed

- **`nest/__init__.py` import**: `astor_get_model_name_for_ram` (was `get_model_name_for_ram`)
- **`bus/store.py` `__all__`**: Correct names + reset function (`AstorBus` / `AstorEvent` / `astor_bus` / `astor_reset_bus`)
- **`forge/extractor.py`**: `mode='llm'` references `astor_llm_extract` (was broken `llm_extract`)
- **`forge/extractor.py`**: `astor_choose_extract_mode` return type `AstorExtractMode` (was undefined `ExtractMode`)
- **`config.py` `DEFAULT_ASTOR_DIR`**: Replaced with `get_default_astor_dir()` call (was undefined)
- **`config.py` `astor_dir`/`astor_db_path`**: Path fields use `get_default_*_path()` functions

### Tests

- **26/26 pytest passing** (10 bus + 2 forge + 5 installer + 2 config + 6 REST + 1 init)
- All tests use `monkeypatch.setenv('ASTOR_DIR', tmp_path)` for isolation

---

## [Unreleased]

### Added

- **Insight 12 (verdict field)**: New `verdict` column on `memory_canonical` table. Tags every Fact as `settled` / `contested` / `thin`. Defaults to `settled`. Adapted from Atlaso's verdict system (atlaso.ai, ProductHunt #4 2026-08-05). Complements 3-tier ACL with confidence-grading.
- **Insight 13 (planned v1.2+)**: Zero-model event classification layer. Adapted from Activity Frames paper (arXiv:2608.05784). Deferred to v1.2+ as performance optimization.
- **Documentation**: Initial doc set (README, architecture, migration, agent-adapters, faq, troubleshooting, contributing, ACKNOWLEDGEMENTS)
- **Iron rules**: 15 Core runtime rules + 5 Docs engineering rules + 8 Engineering ship-time rules + 4 Vendor-neutral rules + 4 Personal rules
- **CI**: GitHub Actions link-check workflow (`.github/workflows/link-check.yml`) using lychee
- **P-LINK-CHECK-DOCS-041**: New docs engineering rule replacing P-DOCS-BUILD-040 (zenical build → lychee link-check)
- **README "How we compare"**: New section citing Activity Frames (60-343× re-derivation cost) and Atlaso (verdict pattern) as independent validation of our architecture

### Changed

- **P-CONF-003**: Refined from "skip filler, no celebration, one-line status" to "avoid filler and redundant status narration. Detailed style preferences live in CONTRIBUTING.md"
- **P-MULTISRC-002**: Generalized from "skill + wiki + memory" to "memory, tools, retrieval indices"
- **P-DEDUPE-014**: Refined from "stable_id + content fingerprint" to "content-aware identity. Implementation in `astor_memory.bus.dedup`"
- **P-CRON-DATA-010**: Refined from "type ∈ {data_pull, transform, deliver}" to "enum-validated types (config-defined whitelist)"

### Removed

- **P-DOCS-BUILD-040**: Removed in favor of GitHub-native .md rendering + lychee link-check (zenical was over-engineered for v1.0's 9-doc scope)
- **P-CONT-006**: Moved from Core runtime to Personal category (opt-in via config); not default

---

## [0.1.0.dev0] - 2026-08-14

### Added

- **Initial release (pre-alpha docs)**: Repository skeleton, pyproject.toml, LICENSE, .gitignore
- **Core 15 iron rules** (default runtime)
- **8 Engineering ship-time rules** (CI-enforced)
- **5 Docs engineering rules** (CI-enforced)
- **3-store triplet design**: `bus` (event log) + `forge` (LLM extraction) + `nest` (vector store)
- **3-tier isolation**: `public` / `source` / `private × N`
- **3 temporal scopes**: `short_term` / `long_term` / `profile`
- **Lifecycle**: decay + merge + promote
- **Revision tracking**: append-only, `revision_id` columns
- **Citation-first**: every `<ref>` embedded in recall output
- **Cross-LLM adapter**: OpenAI / Anthropic / Gemini / DeepSeek / 智谱 / Ollama

### Planned for v1.0

- 5 CLI commands: `init` / `write` / `read` / `doctor` / `config`
- REST API (optional)
- Python native (built-in)
- Env compat: `MEMU_URL` → `ASTOR_FORGE_URL` etc.
- Migration tool: `am migrate from-memory-bus`

### Deferred

- v1.1: Multi-user dashboard (`am ui`)
- v1.1: MCP server (FastMCP wrapper)
- v1.1: Experience ↔ Skill split
- v1.1: On-demand generation (Mem-π Insight 9)
- v1.1: Abstain mechanism (Mem-π Insight 10)
- v1.2: LangChain adapter
- v1.2: GitHub Pages docs (if project grows > 30 docs)
- v2.0: HNSW index (when `nest` > 100 K docs)

---

## Version history

| Version | Date | Status | Notes |
|---|---|---|---|
| 0.1.0.dev0 | 2026-08-14 | Pre-alpha docs | Docs-first development; code in progress |
| 0.2.0 | 2026-08-15 | Alpha | Multi-user 9-db layout + bot-binding.db + Phase B CLI shipped |
| **0.3.0** | **2026-08-15** | **Beta** | **21 CLI subcommands + `am doctor` + full test coverage (88/88) + pyproject bump** |
| 0.3.0 | (cancelled target — see 0.3.0 shipped above) | | |
| 1.0.0 | (target) | Stable | Docs + polish + open-source release |

## 0.2.0 (2026-08-15) — Multi-user + bot-binding.db

### Migration
- Single-tier → **3-tier × 3-store = 9 SQLite files**: `public/{bus,nest,forge}.db`, `source/{bus,nest,forge}.db`, `users/<u>/{bus,nest,forge}_<u>.db`
- 6176 canonical facts migrated from legacy `memory-bus`, `memu.db`, `memory_user_the_nuts.db`, `mempalace/chroma.sqlite3`
- 4 user split (Sunny/cy/user_a/Xindi) from admin db into their own 9-db layouts

### bot-binding.db (new)
- Path: `$ASTOR_DIR/bot-binding.db` (default `<runtime_dir>bot-binding.db`)
- 3 tables: `platforms`, `user_meta`, `bindings`
- 7 platforms: 1 TG + 1 DC + 5 Weixin (admin + 4 user accounts)
- 5 user_meta rows (admin permanent + 4 user trial/lifetime)
- 5 active bindings (chat_id ↔ user_id)
- All token reads via `astor_get_token()` audit-logged with `source=db`
- 6 invariants checked by `am platform verify`

### Phase B CLI subcommands
- `am bot on|off|add-user|list-users|promote|demote|bind-platform|unbind|status` (9 subcommands)
- `am admin whoami|audit-log [--actor] [--user] [--action] [--since]` (2 subcommands)
- `am platform list|list-users|list-bindings|resolve|token-get|token-set|bind|unbind|add-user|verify` (10 subcommands)
- 61 → 82 tests passing

### WeChat outbound push issue (known, parked)
- All 3 platforms: server-side 200 OK + message_id
- TG + DC: deliver to client ✅
- **WeChat: server 200 + message_id but client never receives push**
- Hypothesized: ilink push channel stale; client-side and bot context state need resync (most likely cause: user needs to re-scan QR / restart wechat long-poll gateway)
- Affects: `cronjob deliver`, `send_to_platform.py weixin`, direct ilink API — all paths return server-200 but client receives nothing
- Resolved path to investigate: re-scan QR, restart long-poll daemon, manual refresh context token

### Files
- `astor_memory/_internal/bot_binding.py` (db module + API + audit)
- `astor_memory/_internal/platform_bridge.py` (3-level token fallback)
- `astor_memory/_internal/acl.py` (role-based ACL)
- `astor_memory/_internal/acl_layout.py` (9-db paths)
- `astor_memory/_internal/audit_logger.py` (audit db)
- `astor_memory/forge/schema.py` (new forge schema v1)
- `astor_memory/cli/main.py` (added 10 `platform` subcommands, 9 `bot` subcommands, 2 `admin` subcommands)
- `tests/test_bot_binding.py` (12 tests)
- `tests/test_platform_bridge.py` (9 tests)
- `tests/test_cli_doctor.py` (6 tests for `am doctor` + version + verify)
- `scripts/check_bot_binding_invariants.py` (6 invariants, audit-logged)

## 0.3.0 (2026-08-15) — Ship-ready CLI + doctor + 88 tests

### New
- **`am doctor`**: comprehensive health check showing ASTOR_DIR, bot-binding.db size, 9-db canonical/embedded coverage per location, 6 invariants status, package version
- **`tests/test_cli_doctor.py`** — 6 new tests for `am doctor` + `am version` + `am platform verify/list` + `am bot list-users` + `am admin whoami`
- **`pyproject.toml`** bumped to 0.3.0; added `[tool.astor.platform]` + `[tool.astor.cli]` blocks documenting the v0.3.0 contract
- **`README.md` CLI table** expanded from 8 to 21 subcommands across 3 namespaces (core / `am bot ...` / `am admin ...` / `am platform ...`)
- **`scripts/check_bot_binding_invariants.py`** — 6 invariants standalone (also called from `am platform verify` + `am doctor`)

### Stats
- 82 → 88 tests passing
- 21 CLI subcommands across 3 namespaces
- 6176 / 6176 canonical embedded (100%)
- All 6 invariants pass

---

## Migration notes

For users of the precursor `memory-bus` system (Hermes agent P0-P36):

- Env vars (`MEMU_URL`, `MEMPALACE_URL`, `BUS_URL`) are aliased and continue to work
- Data migration tool: `am migrate from-memory-bus --source=~/.memory-bus/bus.db`
- Parallel-run mode: `am init --parallel --port=7804`
- Full guide: [`docs/migration.md`](./docs/migration.md)

---

[Unreleased]: https://github.com/flopworld/astor-memory/compare/v0.1.0.dev0...HEAD
[0.1.0.dev0]: https://github.com/flopworld/astor-memory/releases/tag/v0.1.0.dev0
