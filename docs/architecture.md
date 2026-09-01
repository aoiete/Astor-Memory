# Architecture

> Deep dive on Astor-Memory's 3-store × 3-tier design, 11 absorbed insights, and lifecycle evolution.

This document explains *why* each component exists. For installation and usage, see [`README.md`](../README.md).

---

## Vision

Astor-Memory is built for **one server shared by a small group** —
typically a household where you, your family, and a few friends each have
their own agent identity but share the same bot process. The design
priorities follow from that:

1. **Data isolation by default.** A user MUST NOT see another user's
   facts even if the bot has them stored. ACL enforces this at the
   matrix level — no caller cooperation needed (see
   [ACL v1.2 hardening](acl-v1.2-hardening.md) for the threat model).

2. **One shared public knowledge base.** All users benefit from skills,
   rules, and reference material that the admin curates. Public tier is
   the answer; private tier is the per-user private tier.

3. **No vendor lock-in.** Each user's memory is just a SQLite file under
   `~/.astor/users/<id>/`. They can `git pull` to back up, `sqlite3` to
   inspect, or move their data to another install. Nothing is
   proprietary storage.

4. **The admin is also a user.** first_admin is itself a tier (source),
   not a privileged "root" outside the model. Same ACL matrix applies.

This is **not** a SaaS memory service. There is no per-seat billing,
no multi-region replication, no per-customer customization. It is a
single-server tool you operate for the people you know.

## Table of contents

1. [The 3-store triplet](#1-the-3-store-triplet)
2. [The 3-tier isolation model](#2-the-3-tier-isolation-model)
3. [Temporal scopes (3 layers × 3 dimensions)](#3-temporal-scopes-3-layers--3-dimensions)
4. [Lifecycle: decay, merge, promote](#4-lifecycle-decay-merge-promote)
5. [Revision tracking (append-only)](#5-revision-tracking-append-only)
6. [Citation-first context packs](#6-citation-first-context-packs)
7. [Search ↔ context-pack separation](#7-search--context-pack-separation)
8. [Cross-LLM adapter (vendor-neutral recall)](#8-cross-llm-adapter-vendor-neutral-recall)
9. [Single-user vs multi-user mode](#9-single-user-vs-multi-user-mode)
10. [Process model](#10-process-model)
11. [Iron rules (default runtime)](#11-iron-rules-default-runtime)
12. [External skill governance](#12-external-skill-governance)
13. [Absorbed insights (11 from literature)](#13-absorbed-insights-11-from-literature)

---

## 1. The 3-store triplet

Astor-Memory decomposes memory into three single-responsibility stores. Each does one thing well; together they form a complete pipeline.

### `bus` — event log (append-only)

```python
# astor_memory/bus.py
class Bus:
    """Append-only event log. SQLite + WAL. Time-series."""

    def append(self, event: Event) -> EventId:
        """Single write. Returns immediately. No extraction, no indexing."""

    def query(self, since: datetime, kind: str) -> list[Event]:
        """Time-range scan. For replay, audit, debugging."""
```

**Why a separate event log?** Because raw events are valuable even when not yet structured. A user typing "user prefers concise replies" creates one event. Three months later, we may discover it relates to a behavior pattern. The bus preserves that original signal.

**Storage**: 3 separate SQLite files with WAL mode (one per store):
- `~/.astor/astor_bus.db` — events + memory_candidates + memory_canonical + audit_log
- `~/.astor/astor_nest.db` — vector embeddings
- `~/.astor/astor_forge.db` — LLM extraction cache

Why 3 files (per user lock 2026-08-15): independent backup/migration, independent lock contention (bus write-heavy vs nest read-heavy), independent schema evolution (HNSW in v2.0+ won't touch bus).

**TTL policy**: events 90 days (configurable), candidates 30 days, canonical + rules permanent.

### `forge` — fact extraction (LLM-async)

```python
# astor_memory/forge.py
class Forge:
    """Extract structured facts from raw events. Async via daemon thread."""

    def extract(self, event_id: EventId) -> Fact:
        """Cloud LLM call. Async. Returns when extraction completes."""

    def provider(self) -> LLMProvider:
        """Currently configured provider. Read from config."""
```

**Why async?** Writes must return immediately (`bus.append()` is sync; `forge.extract()` runs in background). This is the Mem-π insight: agents should not block on memory extraction.

**LLM provider**: configurable. Defaults to the agent's current provider. Supports OpenAI, Anthropic, Gemini, DeepSeek, 智谱, Ollama. Cross-LLM compatible.

**Output**: a `Fact` object with `kind`, `confidence`, `references`, and a `revision_id`. Goes to `nest` for indexing.

### `nest` — vector store (SQLite + NumPy kNN)

```python
# astor_memory/nest.py
class Nest:
    """Vector store. SQLite for text + NumPy for brute-force kNN."""

    def index(self, fact: Fact) -> None:
        """Compute embedding via fastembed. Insert into SQLite."""

    def search(self, query: str, top_k: int) -> list[Hit]:
        """Embed query, cosine similarity, return top_k."""
```

**Why not chromadb?** Three reasons:
1. **Footprint** — chromadb pulls in 80 MB of transitive deps. NumPy + SQLite is 12 MB.
2. **Simplicity** — brute-force kNN is 5 lines of code. Fast up to 5 K docs (1 ms latency).
3. **Predictability** — no daemon, no separate server, no migration scripts.

**HNSW deferred to v2.0** — when `nest` exceeds 100 K docs, we'll add faiss or hnswlib. Not before.

### The write pipeline

```python
def write(text: str, scope: str = "long_term", tier: str = "public") -> FactId:
    eid = bus.append(Event(text=text, scope=scope, tier=tier))
    forge.extract_async(eid)        # background
    return eid                       # immediate
```

The event is durable the moment `bus.append()` returns. Extraction and indexing are best-effort with retry.

### The read pipeline

```python
def read(query: str, top_k: int = 5, tier: str = "public") -> list[Hit]:
    return nest.search(query, top_k=top_k, tier=tier)
```

A read is a vector search. Citations come from the indexed `Fact.references`.

---

## 2. The 3-tier isolation model

Three spatial tiers match the ACL needs of every agent system we've seen in production.

| Tier | Default visibility | Use case |
|---|---|---|
| `public` | Everyone (all users + agent) | Shared knowledge, skills, public rules |
| `source` | Admin only (agent sees, user doesn't) | Admin-private context, agent self-patterns, internal config |
| `private × N` | One user at a time | Per-user private facts (preferences, history, audit) |

### Why exactly 3?

We considered 2 (just public/private) and 5 (PowerContext's profile/private/short/long/shared). The reason 3 wins:

- **2 is too coarse.** Admins need a tier for "I see this but the end user doesn't" — for things like debugging context, internal notes, agent self-patterns. Without it, admins either leak too much (everything in `public`) or hide useful context from the agent (everything in `private`).
- **5 is over-scoped.** PowerContext's 5 tiers mix spatial (where it lives) with temporal (how long it lives) with audience (who sees it). We separated these concerns: spatial = 3 tiers (this section), temporal = 3 scopes (next section), audience = ACL grants (later in this section).

### ACL rules

```yaml
# ~/.astor/config.yaml
tiers:
  default: public              # default tier for writes
  admin_only: source           # only admins can write to source

# Per-rule grants (v1.1+)
grants:
  - rule_id: P-CRON-DATA-010
    grants: [admin]
    # v1.0: hardcoded by tier; v1.1+ configurable
```

Default behavior: writes go to `tier.default`. Reads default to `tier.default` + the user's own `private × N` row.

### ACL enforcement flow (v1.1.1 — per-request actor resolution)

The HTTP server runs Flask multi-threaded. Each request goes through
`before_request` to bind the ACL context for the worker thread:

1. **`_astor_resolve_actor(body.user)`** reads `bot-binding.db user_meta.role`
   and returns `(actor, role)`:
   - `admin` alias or `role='first_admin'` → `(first_admin, first_admin)`
   - `role='admin'` → `(admin:<id>, admin)` — power user per plan §2624
   - `role='user'` → `(user:<id>, user)`
   - unknown / inactive → `(first_admin, first_admin)` fail-closed
2. **`astor_init_acl(actor, role, tier, user_id=actor_id)`** binds the
   thread-local `_CURRENT` context. `user_id` is the **actor's** id, not
   the target — so downstream `astor_check_*` enforces identity correctly.
3. **Cross-user boundary check** (only when `tier='private'` and
   `target_user != body_user`): runs `astor_check_read` + `astor_check_write`
   against `target_user`. If denied, returns `403 cross_user_forbidden`.
   `admin` role passes this per the carve-out in `acl.py`.
4. **`errorhandler(PermissionError_)`** converts any downstream
   `astor_check_*` failure into `403 permission_denied` (instead of
   bubbling as 500).
5. **Default bind for GETs**: `health`, `viewer_stats`, `lex_stats` get
   `actor=first_admin, tier=public` so worker threads don't trip
   `astor_acl not initialized`.

**P0 fix history**: v1.1.0 hardcoded `actor='first_admin'` in
`before_request`, allowing any user to write source tier and read any
user's private DB. Fixed in v1.1.1 (2026-08-16). See CHANGELOG.md for
the full incident writeup.

---

## 3. Temporal scopes (3 layers × 3 dimensions)

Beyond spatial tiers, Astor-Memory adds 3 temporal scopes. The intersection of tier × scope gives 9 cells; we actively use 6 of them.

| Scope | TTL | Default use |
|---|---|---|
| `short_term` | 30 days | Today's tasks, recent context, transient state |
| `long_term` | Permanent | User preferences, decisions, rules |
| `profile` | Permanent (per-user) | User persona, stable facts about a specific user |

### Scope determination (Insight 6)

Default = `long_term` (most common case). Override:

```python
write("today's standup at 3pm", scope="short_term")
write("user prefers Chinese", scope="long_term")
write("alice's timezone is MDT", scope="profile", user_id="alice")
```

### Cross-tier promotion (auto)

A `short_term` fact that gets accessed ≥ 5 times in 30 days auto-promotes to `long_term`. This handles "things that started as transient but turned out to matter" without manual intervention.

---

## 4. Lifecycle: decay, merge, promote

Three automatic processes prevent unbounded growth. Inspired by Ebbinghaus forgetting curve + PowerContext RFC 0020 lifecycle model.

### Decay (relevance decay over time)

```
score = relevance × exp(-age_days / 30) × log(1 + access_count)
```

- `relevance`: raw cosine similarity
- `exp(-age_days / 30)`: half-life of 30 days
- `log(1 + access_count)`: usage bonus (diminishing returns)

A fact that's never accessed decays exponentially. A fact accessed 100 times decays much slower.

### Merge (deduplicate near-duplicates)

Facts with cosine similarity ≥ 0.85 consolidate into one revision. The merged fact inherits the union of references and the max confidence.

Triggered by:
- `am compact` (manual)
- Nightly cron (automatic, off by default; enable via `am config lifecycle.auto_merge=true`)

### Promote (graduate facts to rules)

A fact that appears ≥ 3 times across revisions (different `revision_id` but same conceptual entity) graduates from `kind=fact` to `kind=rule`. Rules get priority in retrieval.

This is how agent behavior patterns become encoded as default policies.

### Configuration

```yaml
# ~/.astor/config.yaml
lifecycle:
  decay_half_life_days: 30
  merge_threshold: 0.85
  promote_threshold: 3
  auto_compact: false  # enable nightly cron
```

---

## 5. Revision tracking (append-only)

Every `update` creates a new `revision`. Old content stays queryable for audit.

```python
# Writing a "update"
write("user prefers concise replies", scope="long_term")
# → f_8a3b2c1d, revision_id=1

# Updating (later)
write("user prefers very concise replies", scope="long_term", update_of="f_8a3b2c1d")
# → f_8a3b2c1d, revision_id=2  (same fact_id, new revision)
# revision_id=1 still exists and can be queried
```

### Why not overwrite?

Three reasons:

1. **Audit** — when did the agent's understanding change? Trace the revision history.
2. **Citation** — `<ref fact_id:revision_id>` lets you pin to a specific historical state.
3. **No data loss on retry** — if `write()` fails after partial commit, the previous revision remains.

### Storage

`memory_canonical` table gains `revision_id` and `parent_revision_id` columns. Index by `(fact_id, revision_id)`. Query latest = `MAX(revision_id)` per `fact_id`.

---

## 6. Citation-first context packs

Every recall() output embeds `<ref>` markers so agents can verify what they read.

```python
hits = read("user preferences", top_k=5)

# Each hit carries:
# - content (the text)
# - references (list of memory_id:revision_id)
# - confidence (0.0 - 1.0)
# - context_pack_inclusion_reason (why this was included)
```

Example output:

```
[0.92] 用户偏好简洁回复
  ref: f_8a3b2c1d:rev_2, f_7c4d9e0a:rev_1
  conf: 0.94
  reason: cosine_match + temporal_decay

[0.78] 用户偏好中文交流
  ref: f_3e1b2f9c:rev_1
  conf: 0.81
  reason: cosine_match
```

### Why citation-first?

**Citation proves locatability, not correctness.** A hit with `ref=f_8a3b2c1d:rev_2` lets the agent call `am verify f_8a3b2c1d:rev_2` to confirm the content still exists and hasn't been superseded.

Low-confidence hits (< 0.7) require explicit human ack before injection into context packs. This prevents hallucination cascades.

---

## 7. Search ↔ context-pack separation

PowerContext RFC 0028 insight: keep "search" pure and "context pack preparation" separate.

### Search (粗排)

```python
def search(query: str, top_k: int = 30) -> list[Hit]:
    """Pure retrieval. No budget control, no trimming."""
```

Returns Top-30 with FTS + vector + time signals fused (Reciprocal Rank Fusion). High recall; precision intentionally loose.

### Context pack (精排)

```python
def prepare_context(query: str, max_bytes: int = 4096) -> ContextPack:
    """Take search hits, trim to budget, add citations, mark omissions."""
```

Returns a `ContextPack` with:
- `content`: the actual text to inject
- `references`: list of `<memory_id:revision_id>` cited
- `truncated`: which hits were trimmed due to budget
- `omitted`: bool, whether any hits were dropped
- `byte_count`: actual bytes used

### Why split?

Without separation, `recall()` dumps all hits into the prompt → long-tail pollution. With separation, the agent explicitly controls how much context to consume.

Mem-π measured: separate search→context_pack achieves 43.1% task success with 138 tokens; naive dump achieves 27% with 200-225 tokens.

---

## 8. Cross-LLM adapter (vendor-neutral recall)

The Mem-π paper showed: a memory generation strategy trained on Qwen2.5-7B still works as guidance for GPT-5-mini (+16 percentage points over RAG +4.3 pp).

**Insight: memory strategy is independent of executor.**

### Implementation

Astor-Memory's `recall()` output is structured (citations, confidence, scope) — never coupled to any specific LLM provider's prompt format.

```python
# This output works for any LLM downstream
pack = recall("user preferences", max_bytes=2048)
# → structured hits + citations, format-neutral

# Agent then injects into ANY LLM:
openai.chat(messages=[{"role": "user", "content": inject(pack)}])
anthropic.messages(messages=[{"role": "user", "content": inject(pack)}])
```

### Configuration

```bash
am config llm.provider=openai        # default
am config llm.provider=anthropic
am config llm.provider=gemini
am config llm.provider=deepseek
am config llm.provider=zhipu
am config llm.provider=ollama         # local
```

Same recall output, different LLM downstream. The 16 iron rules (P-CITATION-015 etc.) ensure every context pack is auditable regardless of provider.

---

## 9. Single-user vs multi-user mode

Astor-Memory supports two modes with the same code path. The mode is determined by config, not by build.

### Single-user mode (default)

```bash
am init
```

Creates:
- `~/.astor/public.db` — shared knowledge
- `~/.astor/source.db` — admin-private (agent + admin)
- `~/.astor/private_admin.db` — self-private (admin's own user, present in both single-user and multi-user modes)

### Multi-user mode

```bash
am bot on
am bot add-user alice
am bot add-user bob
```

Creates:
- `~/.astor/public.db` (shared)
- `~/.astor/source.db` (admin)
- `~/.astor/private_<user_id>.db` — one per user

The CLI command `am bot on` triggers the structure creation; the code path is identical otherwise.

### When to use which

| Scenario | Mode |
|---|---|
| Personal agent (1 user) | Single-user |
| Bot with N users (e.g. Discord, Telegram, WeChat) | Multi-user |
| Personal agent + bot (admin uses bot + has own private) | Multi-user with `user_id=admin` as default |

---

### Bots design philosophy — 1xNxM many-to-many

astor treats **people** (user_id) and **bots** (platform_id) as two
independent dimensions. The relationship is genuinely many-to-many:

    1 person --> 1..N bots (different platforms for the same person)
    1 bot    --> 1..N persons (different users via different chat_ids)

That is why  has TWO separate tables:

-  -- per-bot config (token, base_url, enabled)
-   -- per-chat-id -> user_id mapping

Not one combined table, because the relationships are independent.

**Why WeChat is special (1 chat = 1 user typically):** WeChat's 
protocol only allows 1:1 DMs, so a single WeChat bot instance serves many
users via separate DM chats. Each DM's  binds to one .

**Telegram / Discord are 1:N (one bot, many users):** Both platforms
support many parallel chats from one bot token. One Telegram bot maps
to many bindings, each with a different chat_id.

**The bot process has NO special privilege over private data.** Once a
 binding is established, the bot is just transport:

    Telegram DM (chat_id=C, bound to user_id=alice)
      -> astor_init_acl(actor=user:alice, role=user, tier=private_alice)
      -> acl_check_read passes
      -> reads/writes alice's private DB

If bob's chat_id D sends a read for alice's private:

      -> astor_init_acl(actor=user:bob, role=user, tier=private_alice)
      -> acl_check_read DENIES (user_id mismatch)
      -> 401 user grant required (strict privacy model 2026-08-16)

See  for the full
treatment (anti-patterns, four canonical scenarios, why two tables).

## 10. Process model

Astor-Memory ships as a single daemon, library, or REST server:

```
astord                  # in-process bus + forge + nest
```

Or even simpler — **library mode**:

```python
from astor_memory import AstorMemory
am = AstorMemory()
am.write("...")
am.read("...")
```

No daemon, no port. Library imports directly.

### REST API (optional)

For external integration (e.g. non-Python agents):

```python
# astor_memory/server.py
from flask import Flask
app = Flask(__name__)

@app.route("/v1/write", methods=["POST"])
def write():
    return am.write(request.json["text"])

@app.route("/v1/read", methods=["POST"])
def read():
    return am.read(request.json["query"])
```

Run with `am serve --port=7803` (optional, defaults off in library mode).

### Why in-process?

- **Latency** — in-process calls are 100x faster than HTTP round-trips
- **Simplicity** — one process to start, one to monitor, one to restart
- **Same code path** — library mode and REST mode share the same `AstorMemory` instance; no separate API to maintain

---

## 11. Iron rules (default runtime)

Astor-Memory ships 15 Core runtime iron rules that every agent must obey. Plus 8 Engineering + 5 Docs engineering rules in `CONTRIBUTING.md`. Plus 4 Vendor-neutral + 4 Personal rules opt-in via config.

### Core 15 (default for all agents)

| Category | Rules |
|---|---|
| **Operational** (12) | P-VERIFY-001, P-MULTISRC-002, P-SHIP-004, P-FAIL-005, P-NOLOOP-007, P-CRON-DATA-010, P-CITATION-015, P-IMMUT-016, P-FAIL-NO-DATA-024, P-NO-FABRICATE-026, P-DEDUPE-014, P-FAILOPEN-013 |
| **Communication** (2) | P-CONF-003, P-PUSHBCK-008 |
| **Security** (1) | P-NOSECRET-020 |

### Personal category (opt-in)

- P-CONT-006 — "继续" token workflow (operator-neutral reference)

### Disclosure policy

11 of the 15 Core rules are universal (matched in CoALA, PowerContext RFCs, Anthropic/ADK style guides). 4 rules (P-CONF-003, P-MULTISRC-002, P-CRON-DATA-010, P-DEDUPE-014) are behavioral statements whose implementation details (style granularity, 3-source example, cron enum list, fingerprint algorithm) live in `CONTRIBUTING.md` to avoid leaking personal workflow or attack surface.

Full rule list and implementation details: [`docs/contributing.md`](../docs/contributing.md).

---

## 12. External skill governance

Astor-Memory doesn't own external skills. It can *scan* and *reference* them, but not edit or copy.

```bash
am skill scan                              # index ~/.hermes/skills/
am skill import moomoo-currency-pitfall    # reference into catalog
am skill edit moomoo-currency-pitfall      # REFUSE — external skill, Hermes owns content
```

Only Astor-managed skills (created via `from-experience` in v1.1+) can be edited.

Why: discovery without ownership transfer. PowerContext RFC 0051 principle.

---

## 13. Absorbed insights (11 from literature)

Astor-Memory v1.0 ships these insights, absorbed from papers and open-source projects. Each contributed to a specific design decision.

| # | Insight | Source | Ship status |
|---|---|---|---|
| 1 | Search ↔ context-pack separation | PowerContext RFC 0028 | v1.0 (mandatory) |
| 2 | LLM listwise rerank | PowerContext RFC 0051 | v1.1 (opt-in) |
| 3 | Lifecycle decay + merge + promote | PowerContext RFC 0020, Ebbinghaus | v1.0 (mandatory) |
| 4 | Experience ↔ Skill split | PowerContext RFC 0051 | v1.1 (key architecture) |
| 5 | Artifact revision tracking | PowerContext RFC 0028 | v1.0 (mandatory) |
| 6 | Temporal scope (short/long/profile) | PowerContext 5-layer model | v1.0 (3 of 5 scopes) |
| 7 | Citation-first design | PowerContext citation principle | v1.0 (mandatory) |
| 8 | External skill governance | PowerContext RFC 0051 | v1.0 (boundary) |
| 9 | On-demand generation | Mem-π paper §3 | v1.1 (key architecture) |
| 10 | Abstain mechanism (71% abstention) | Mem-π paper §5 | v1.1 (key architecture) |
| 11 | Cross-LLM adapter | Mem-π paper §6 | v1.0 (mandatory) |

**v1.0 scope**: 10 of 11 (Insights 2, 4, 9, 10 deferred to v1.1).

**Why this matters**: Astor-Memory didn't invent these patterns — we absorbed them and integrated them into a single coherent architecture. Each insight is a battle-tested design from production systems or peer-reviewed papers. Credits in [`ACKNOWLEDGEMENTS.md`](../ACKNOWLEDGEMENTS.md).

---

## Competitive landscape (2026-08-16)

For a research-backed comparison against the two leading open-source agent-memory frameworks (EverOS, A-MEM), see [`docs/integration-research-everos-a-mem-2026-08-16.md`](./integration-research-everos-a-mem-2026-08-16.md). Key findings:

- **EverOS** (`EverMind-AI/EverOS`, 12k ★, Apache-2.0, v1.2.3 2026-08-07): Markdown-truth + SQLite-state + LanceDB-index, 8-kind taxonomy, async cascade with crash-recovery queue, **reflection orchestrator** (Select→Merge→Re-extract→Deprecate). Strongest competitor in single-user / dev-friendly segment.
- **A-MEM** (`agiresearch/A-mem`, 1149 ★, MIT, arXiv:2502.12110): Zettelkasten-style auto-linking, evolution_history per memory, ChromaDB+BM25. Interesting ideas but single-namespace + no ACL.
- **astor-memory moat**: 4-tier × 4-role ACL (no one else does this), cross-platform token binding (Telegram/Discord/WeChat/Feishu), per-fact provenance graph with DOT export, dry-run forget, versioning + restore, repo tier for per-git-repo memory.

P1 follow-ups identified: async cascade write with crash-recovery queue
**(SHIPPED 2026-08-16 as v1.2.0 — see `cascade_state` table + `/v1/cascade/replay`
endpoint in docs/api.md)**, `keywords` + `context` schema columns
**(SHIPPED 2026-08-16 as v1.2.1 — see `hybrid_merge` Jaccard boost + new
canonical columns in docs/api.md)**. P2 #1 reflection orchestrator
**(SHIPPED 2026-08-16 as v1.2.2 — see `nest/reflection.py` + `POST /v1/reflection/run`
in docs/api.md)**. P2 #2 Zettelkasten auto-link
**(SHIPPED 2026-08-16 as v1.2.3 — see `nest/auto_link.py` + write hot path
in docs/api.md)**. All P1 + P2 items shipped. See the research
doc for full P1/P2/P3 priority list.

## Next

- [`docs/api.md`](./api.md) — full REST endpoint reference (18 endpoints)
- [`docs/integration-research-everos-a-mem-2026-08-16.md`](./integration-research-everos-a-mem-2026-08-16.md) — competitive landscape (EverOS + A-MEM research)
- [`docs/migration.md`](./migration.md) — migrate from mem0 / Letta / Zep / MemGPT / ChromaDB / Pinecone / Weaviate / plain files
- [`docs/agent-adapters.md`](./agent-adapters.md) — MCP / LangChain / REST / Python integration
- [`docs/faq.md`](./faq.md) — frequently asked questions
- [`docs/troubleshooting.md`](./troubleshooting.md) · [中文](troubleshooting.zh-CN.md) — common errors and fixes
- [`docs/contributing.md`](./contributing.md) — for contributors


## Related design docs

- [ACL v1.2 hardening (2026-09-01)](acl-v1.2-hardening.md) · [中文](acl-v1.2-hardening.zh-CN.md)
- [Success-pattern auto-detect (v1.13.0, 2026-09-02)](pattern-detector.md) · [中文](pattern-detector.zh-CN.md)
