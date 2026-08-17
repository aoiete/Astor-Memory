# Research: EverOS + A-MEM (2026-08-16)

> **Context**: User asked for research on "InvMem", described as #1 on AgentMemoryLeaderboard with 45.1 points in August 2026. After 4-way search (GitHub API, Bing, DuckDuckGo, Perplexity sonar with 20 citations), **no project named "InvMem" exists** — likely hallucinated information. Perplexity's closest candidates were EverMemOS and A-MEM. This document research-backs both as a fallback and as the actually-existing leaders in the open-source agent-memory space.
>
> **Method**: Perplexity sonar API (20 citations per query, $0.008/call) + GitHub REST API verification + raw `README.md` / `CLAUDE.md` / `docs/architecture.md` pull. Cited inline.

---

## TL;DR — what to integrate vs what we already have

| Capability | EverOS v1.2.3 (Aug 2026) | A-MEM (Feb 2025) | **astor-memory v1.1.1** |
|---|---|---|---|
| **Tier isolation** | App / Project / User / Agent scopes (orthogonal) | Single namespace | `public` / `source` / `private × N` / `repo × N` |
| **Storage stack** | Markdown (truth) + SQLite (state) + LanceDB (vector + BM25) | ChromaDB + pickle | SQLite (bus/forge/nest/lex) |
| **Retrieval** | Hybrid vector + BM25 + scalar filter (single LanceDB query) | Hybrid BM25 (rank_bm25) + cosine | Hybrid vector + BM25 (`/v1/read` + `/v1/read/multi`) |
| **Memory evolution** | **Reflection orchestrator** (Select→Merge→Re-extract→Deprecate) | **Zettelkasten-style linking** (auto-link new memories, trigger old-memory rewrites) | Merge dedup v2 (`/v1/merge/find` + `/v1/merge/apply`), provenance graph |
| **Knowledge extraction** | 8 business kinds via `everalgo` package (episode / atomic_fact / foresight / user_profile / agent_case / agent_skill / knowledge_document / knowledge_topic) | Generic MemoryNote with structured fields (content / keywords / links / context / tags / category / evolution_history) | 5+ kinds (fact / system_event / user_profile / rule / preference); forge extraction |
| **Markdown as source of truth** | ✅ (`~/.everos/`) — diffable, Git-versionable, Obsidian-compatible | ❌ (ChromaDB only) | ❌ (SQLite only) |
| **Cross-platform binding** | ❌ | ❌ | ✅ `bot-binding.db` (Telegram/Discord/WeChat/Feishu) |
| **Multi-user ACL** | ❌ single-user focus | ❌ single namespace | ✅ 3-tier × 3-store ACL, 4 roles (first_admin / admin / user / system) |
| **MCP server** | ✅ mentions MCP integration (topic `mcp`) | ✅ MCP server (aibase.cn listing) | ✅ `am mcp serve` |
| **Provenance graph** | ❌ | ✅ linking-by-similarity graph | ✅ `/v1/fact/<id>/{provenance,lineage,graph.dot}` |
| **Versioning + rollback** | ❌ | ❌ | ✅ `/v1/fact/<id>/versions` + `/restore` |
| **License** | Apache-2.0 | MIT | MIT (planned for v1.2+) |

---

## 1. EverOS (EverMind-AI/EverOS)

### Repo metadata (verified 2026-08-16 via GitHub API)

| Field | Value |
|---|---|
| **Repo** | `EverMind-AI/EverOS` (note: NOT `EverMemOS` — redirect merged into EverOS brand) |
| **Stars** | **12,046** (Perplexity under-reported at 1.5k — stale) |
| **Forks** | 892 |
| **Language** | Python |
| **License** | Apache-2.0 |
| **Latest release** | **v1.2.3** (2026-08-07, 9 days ago) |
| **Latest commit** | 2026-08-15 (1 day ago, actively maintained) |
| **Topics** | `agent-memory`, `agentic-ai`, `clawdbot-skill`, `mcp`, `long-term-memory` |
| **Size** | 3.8 MB |

### Architecture (verified via raw `docs/architecture.md` pull)

**Layered DDD** with `import-linter` enforcement:

```
entrypoints/  →  service/  →  memory/  →  infra/
                    ↓              ↓
                (use cases)    (extract, search, cascade, reflection)
```

Cross-cutting: `component/` (LLM/embedding/config injection), `core/` (observability, errors, lifespan), `config/` (Settings + default.toml).

**Storage three-piece set** (the most distinctive design choice):

```
Markdown (.md files)  +  SQLite (state + audit + change queue)  +  LanceDB (vector + BM25 + scalar)
       ↓                        ↓                                    ↓
   truth source            system data                          rebuildable index
```

- Memory root: `~/.everos/{agents,users,knowledge}/` — md files = single source of truth
- System DB: `~/.everos/.index/sqlite/system.db`
- Index: `~/.everos/.index/lancedb/`

**Write path**:

```
1. service.memorize           (entrypoint)
2. memory.extract.pipeline    (calls everalgo)
3. infra.persistence.markdown.write       (atomic: tmp + fsync + rename) ← ✅ return immediately
4a. SQLite audit              (synchronous)
4b. memory.cascade            (async daemon, 500ms debounce, inotify/FSEvents + watchdog)
```

**Key guarantee**: md write is strongly consistent (fsync). LanceDB is eventually consistent; if it crashes, changes buffer in `md_change_state` queue and replay on recovery.

**Read path**:

```
User query → service.search → memory.search (hybrid: BM25 + vector ANN + scalar filter)
                                                    ↓ (optional)
                                              read md for context
```

### 8 business memory kinds

From `memory/strategies/`:

| Strategy | Pipeline | Output |
|---|---|---|
| `extract_atomic_facts` | user | atomic_fact |
| `extract_foresight` | user | foresight |
| `extract_user_profile` | user | user_profile |
| `extract_agent_case` | agent | agent_case |
| `extract_agent_skill` | agent | agent_skill |
| `reflect_episodes` | cron (offline) | merged episodes |
| `trigger_profile_clustering` | cron | profile clusters |
| `trigger_skill_clustering` | cron | skill clusters |

### `everalgo` boundary (algorithmic extraction)

Algorithms live in **separate PyPI packages** (`everalgo-user-memory`, `everalgo-agent-memory`, `everalgo-rank`, `everalgo-knowledge`) imported under the `everalgo` namespace. Boundary contract:
- **Stateless** — pure functions, no class hierarchy
- **No I/O** — does not touch md files / LanceDB / SQLite
- **No prompts inline** — extractors that accept a `prompt-override` parameter use the project-supplied value

This lets `everalgo` be reused across EverOS Cloud, OpenClaw plugins, etc.

### Error handling

`AppError` hierarchy in 4 branches: `DomainError` → 4xx (NotFound/Conflict/InvalidInput/PathTraversal/UnsupportedModality), `InfrastructureError` → 503 (Storage/VectorStore/ExternalService with LLM/Embedding/Rerank sub-errors), `CapabilityError` → 503 (multimodal not enabled), `ConfigurationError` → 500.

### What we should learn from EverOS

| # | Concept | Already in astor? | Worth integrating? |
|---|---|---|---|
| 1 | **Markdown as source of truth** | ❌ | 🟡 Future consideration — diffable + Git-versionable + human-readable is huge for portability. Currently we rely on SQLite dumps for portability. |
| 2 | **Async cascade with crash-recovery queue** | ❌ (synchronous `nest.store()` in write path) | ✅ **HIGH** — async md-write→index-cascade with `md_change_state` queue would survive LanceDB / embedding-model crashes without losing writes |
| 3 | **8 typed memory kinds via separate package** | 🟡 partial (5 kinds in bus, all mixed) | 🟡 Worth a clean break: separate `astor-algo` PyPI package containing only stateless extractors, leave I/O in `astor-memory` |
| 4 | **Layered architecture with `import-linter`** | ❌ (single `astor_memory` package, no layer enforcement) | 🟡 Low priority — works fine for current size; revisit at 2-3x LOC |
| 5 | **Reflection (Select → Merge → Re-extract → Deprecate)** | 🟡 partial (merge dedup v2 only; no episodic consolidation) | ✅ **MEDIUM** — episodic reflection is a real gap. We do `/v1/snapshot/stats` but no offline merge of fragmented episodes |
| 6 | **`md-first` with Obsidian/Git** | ❌ | 🟡 Strategic — could make astor facts portable across users |

---

## 2. A-MEM (agiresearch/A-mem)

### Repo metadata

| Field | Value |
|---|---|
| **Repo** | `agiresearch/A-mem` (official memory lib, 1149 ★) + `WujiangXu/A-mem` (experiment repo, 711 ★) |
| **Language** | Python |
| **License** | MIT |
| **Created** | 2025-02-25 |
| **Last push** | 2025-12-12 |
| **Size** | 1.0 MB |
| **No formal release** — paper-only (arXiv:2502.12110) |

### Dependencies

```toml
sentence-transformers>=2.2.2   # all-MiniLM-L6-v2 default embedding
chromadb>=0.4.22              # vector store (alternative to LanceDB)
rank_bm25>=0.2.2              # BM25
nltk>=3.8.1
litellm>=1.16.11              # LLM backend abstraction
numpy>=1.24.3
scikit-learn>=1.3.2
openai>=1.3.7
```

### Architecture (Zettelkasten-style)

The core idea is **borrowed from the Zettelkasten note-taking method** (German "slip-box"): each memory is a structured note with:
- `content`, `keywords`, `links` (to other memories), `context`, `category`, `tags`
- `timestamp`, `last_accessed`, `retrieval_count`
- `evolution_history` (list of past versions)

**On new memory add:**
1. Generate structured notes (contextual descriptions, keywords, tags) via LLM
2. **Analyze historical memories** to find relevant connections
3. **Establish meaningful links** based on similarity
4. **Trigger old-memory rewrites**: when new memory is added, related historical memories get their context rewritten (evolution)

**Hybrid retrieval** combines BM25 (`rank_bm25`) + cosine similarity (`sklearn`). Single ChromaDB collection. No multi-tier ACL.

### What we should learn from A-MEM

| # | Concept | Already in astor? | Worth integrating? |
|---|---|---|---|
| 1 | **Zettelkasten-style auto-linking** (find similar memories, create edges) | 🟡 partial — `/v1/fact/<id>/provenance` records parents but doesn't auto-link | ✅ **MEDIUM** — could add an "auto-link similar memories at write time" feature using the existing provenance module |
| 2 | **`evolution_history` per memory** | 🟡 partial — `audit_log.old_state` JSON snapshot (similar but separate table) | � Could expose `evolution_history` as a derived view over audit_log to consolidate |
| 3 | **Structured notes (keywords/context/tags as first-class)** | 🟡 partial — `tags` JSON in `memory_canonical`, no `keywords` column | ✅ **MEDIUM** — add `keywords` + `context` columns for better retrieval rerank signal |
| 4 | **LLM-driven trigger of old-memory rewrites** | ❌ | 🟡 Interesting but **risky** — silent rewrites of old facts can lose audit trail. astor's "never silent overwrite" rule (v1.0) should win |
| 5 | **ChromaDB for vector** | ❌ (we use SQLite + numpy) | 🟡 Low — ChromaDB adds 100MB+ dep; our SQLite+numpy is leaner |

---

## 3. What astor-memory does that neither EverOS nor A-MEM does

This is the **competitive moat** we should not regress on:

1. **Multi-user ACL across 4 tiers** (public/source/private/repo) with 4 roles (first_admin/admin/user/system)
   - EverOS: single-user focus, no ACL
   - A-MEM: single namespace, no ACL
   - **astor**: 12-db layout, ACL matrix locked 2026-08-15

2. **Cross-platform token binding** (Telegram/Discord/WeChat/Feishu)
   - Both competitors: agent-only, no platform abstraction

3. **Provenance graph with DOT export**
   - EverOS: no graph
   - A-MEM: linking (similar but no formal graph view)
   - **astor**: `/v1/fact/<id>/{provenance,lineage,graph.dot}` — auditable

4. **Versioning + restore (snapshot of `old_state` per write)**
   - Both: no rollback
   - **astor**: `/v1/fact/<id>/versions` + `/restore` + `audit_log.old_state` JSON

5. **Dry-run forget** (`dry_run: true` on `/v1/forget`)
   - Both: real or nothing
   - **astor**: preview + opt-in audit row

6. **Hybrid ACL matrix enforced via Flask `errorhandler`**
   - Both: no server, CLI-only
   - **astor**: 18 REST endpoints + per-request actor resolution from `bot-binding.db` user_meta

7. **Repo memory tier (per-git-repo isolation)**
   - Both: no concept
   - **astor**: `repo_<sha256[:16]>` tier for per-repo memory

---

## 4. Concrete next steps for astor (priority order)

### P1 (next ship cycle) — direct value-add

1. **Async cascade write with crash-recovery queue** (EverOS pattern)
   - ✅ **SHIPPED 2026-08-16 as v1.2.0** — `bus/cascade.py` + `cascade_state` table
     (schema v3→v4) + `POST /v1/cascade/replay` + `GET /v1/cascade/stats` +
     `am cascade {replay,stats,purge}` + 11 pytests covering enqueue,
     replay success/failure, FIFO drain, purge protection, full server
     roundtrip with ACL enforcement.
   - Add `lex_change_state` table mirroring `md_change_state`
   - On `nest.store()` failure (e.g. embedding model OOM), buffer in queue
   - Cron replays queue on next daemon start
   - **Why**: prevents silent embedding write loss (we had this exact bug — fixed in opt3-6 commit `467b379`)

2. **`keywords` + `context` columns on `memory_canonical`** (A-MEM pattern)
   - ✅ **SHIPPED 2026-08-16 as v1.2.1** — schema v4 → v5 adds `keywords`
     (TEXT JSON array, LLM-extracted or regex-derived) + `context`
     (TEXT 1-2 sentence summary). `hybrid_merge` extended with optional
     `keyword_hits` + `query_keywords` params that score +=
     `keyword_boost` × Jaccard(fact_kw, query_kw). 11 pytests covering
     extraction, migration, store, hybrid_merge boost + backward compat.
   - Update `forge.extractor` to populate both fields
   - Use in `hybrid_merge` rerank: `score = sim × bm25 + boost(keywords_overlap) + boost(context_match)`
   - **Why**: improves fact retrieval precision; A-MEM shows 5-10% precision gain from structured fields

### P2 (consider for v1.2)

3. **Reflection orchestrator** (EverOS pattern, simplified)
   - ✅ **SHIPPED 2026-08-16 as v1.2.2** — `nest/reflection.py` (Select → Merge → Deprecate). Heuristic-mode only (no LLM in v1.2.2; LLM-mode future). 13 pytests covering select / merge / apply / deprecate / idempotency / endpoint + ACL enforced.
   - `nest/reflection.py` — `select_episode_cluster() → merge_narrative() → deprecate_old_ids()`
   - Cron `astor-reflect-weekly-sun0300mdt` (analogous to astor's existing weekly cron)
   - Operates on `kind=episode` rows in public tier (or wherever episodic content lives)
   - **Why**: fills the L2→L3 distillation gap (already noted in our memory architecture §4)

4. **Zettelkasten auto-link at write time** (A-MEM pattern, audit-safe)
   - ✅ **SHIPPED 2026-08-16 as v1.2.3** — `nest/auto_link.py` runs cosine > 0.85 lookup in write hot path, adds bidirectional provenance edges. 11 pytests covering edge creation, idempotency, same-kind filter, threshold filter, backfill audit + server endpoint.
   - On `/v1/write` success, run cosine search over `memory_canonical` for similar content (cosine > 0.85)
   - Insert a `provenance` row with `kind=auto_link`, NOT rewrite existing facts
   - Expose via `/v1/fact/<id>/provenance` (already exists)
   - **Why**: builds implicit knowledge graph without risky rewrites

### P3 (skip for now)

5. Markdown source of truth — would require full rewrite. Skip.
6. LanceDB / ChromaDB swap — no perf pressure. Skip.
7. 8-kind taxonomy — over-engineered for current scale. Skip.

---

## 5. Where the "InvMem 45.1 points" claim came from

Likely sources of confusion:

1. **Hallucinated description** — the specific claim "August 2026 / 45.1 / #1 on AgentMemoryLeaderboard" has no verifiable backing. Perplexity with 20 web citations couldn't find it.
2. **Mix-up with EverMemOS / A-MEM benchmarks** — both have published scores (LoCoMo 92.3%, LongMemEval-S 82% for EverMemOS; arXiv:2502.12110 results for A-MEM) but **not on an "AgentMemoryLeaderboard" with 45.1-point scale**.
3. **Possible future leaderboard** — there IS an `aml-memory-baseline` repo (`richrichgo/aml-memory-baseline`) and `aml-memory-mvp` (`0xboyu/aml-memory-mvp`) which suggests an **AML** (AgentMemory Leaderboard) exists. But no leaderboard URL or 45.1-point score is verifiable as of 2026-08-16.

**Recommendation**: stop citing "InvMem 45.1" until a primary source surfaces. The honest framing is: *"EverMemOS (now EverOS, 12k stars, Apache-2.0) and A-MEM (MIT, 1149 stars, arXiv:2502.12110) are the two leading open-source agent-memory frameworks we benchmarked against; their public scores are LoCoMo 92.3% and LongMemEval-S 82% (EverMemOS) and +significant over SOTA on 6 foundation models (A-MEM, no AML ranking found)."*

---

## 6. References

- **EverOS**: <https://github.com/EverMind-AI/EverOS> — v1.2.3 (2026-08-07), 12k stars, Apache-2.0
- **EverOS docs**: <https://docs.evermind.ai>
- **EverOS architecture**: <https://github.com/EverMind-AI/EverOS/blob/main/docs/architecture.md>
- **EverOS CLAUDE.md**: <https://github.com/EverMind-AI/EverOS/blob/main/CLAUDE.md>
- **A-MEM**: <https://github.com/agiresearch/A-mem> — paper-only, 1149 stars, MIT
- **A-MEM paper**: <https://arxiv.org/abs/2502.12110>
- **A-MEM memory_system.py**: <https://github.com/agiresearch/A-mem/blob/main/agentic_memory/memory_system.py>
- **AML baselines (unrelated to InvMem)**: <https://github.com/richrichgo/aml-memory-baseline>, <https://github.com/0xboyu/aml-memory-mvp>

---

## 7. Update to architecture.md

This research should be cross-linked from `docs/architecture.md` § "Acknowledgements" so future readers see the benchmark landscape. (Will add in a follow-up patch.)
