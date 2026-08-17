# Astor-Memory

> **Self-owned memory system for AI agents.** Three stores, three tiers, zero vendor lock-in.

---

## Why we built this

Modern AI agents need memory. The current options all force a trade-off:

| Option | What you get | What you give up |
|---|---|---|
| **RAG-only** (vector store) | Simple retrieval | No event log, no fact extraction, no per-user isolation |
| **Letta** (Memory Blocks) | Read-only protection + archival | Heavy runtime, opinionated architecture |
| **mem0** (4-level ACL) | Multi-tenant + scope tags | Cloud-coupled, async-by-default |
| **PowerContext / PowerMem** | Search↔context-pack separation | Pre-1.0, Chinese-language-first, no self-host |
| **Hand-rolled** (chromadb + memu.ai SDK) | Total control | 3 GB venv, 3 server processes, fragile upgrades |

**Astor-Memory** exists because we hit all four pain points in production across 33 ship sessions and 50+ cron jobs running on a self-hosted agent. We learned:

1. **Three stores are the minimum viable decomposition.** An append-only event log (`bus`), an LLM fact extractor (`forge`), and a vector store (`nest`) map cleanly onto "what happened → what to remember → what to recall." More layers create coordination overhead; fewer collapse semantics.
2. **Three tiers of isolation match real ACL needs.** Public knowledge (skills, rules) + admin-private (agent sees, user doesn't) + per-user private (N isolated DBs) — no more, no less.
3. **Vendor lock-in is the silent killer.** chromadb migrations, memu.ai SDK breaking changes, transformers eating 3 GB of venv — every dependency we picked bit us within 6 months. The lesson: own the code or own the risk.

If you've felt any of these, Astor-Memory is built for you.

---

## What makes Astor-Memory different

| Differentiator | What it means |
|---|---|
| **3-store triplet** | `bus` (append-only event log) + `forge` (LLM fact extraction) + `nest` (vector store). Each does one thing well. |
| **3-tier isolation** | `public` + `source` (admin-private) + `private × N` (per-user). Opt into multi-user mode with one command. |
| **Self-owned code** | Pure Python + SQLite + NumPy. No chromadb, no memu.ai SDK, no transformers, no torch. Install footprint < 50 MB. |
| **Vendor-neutral LLM** | `forge` works with OpenAI / Anthropic / Gemini / DeepSeek / 智谱 / Ollama. Same recall output, any provider. |
| **Citation-first** | Every context-pack output embeds `<ref memory_id revision_id>` so agents can verify what they read. |
| **Lifecycle that self-evolves** | Ebbinghaus-style decay + cosine-merge + promote-after-3-occurrences. Agents actively forget, merge, and graduate facts to rules. |
| **Append-only + revision tracking** | Updates create new revisions; old content stays queryable for audit. No silent overwrites. |
| **Cross-LLM adapter** | A recall() output trained on Qwen2.5-7B still works as guidance for GPT-5-mini. Insight from Mem-π paper. |

---

## Three stores, three tiers (the architecture in 60 seconds)

```
┌─────────────────────────────────────────────────────────────┐
│                      astor_memory                           │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │     bus      │  │    forge     │  │     nest     │      │
│  │  (events)    │─→│ (extraction) │─→│   (vector)   │      │
│  │              │  │              │  │              │      │
│  │  SQLite WAL  │  │  cloud LLM   │  │ SQLite+numpy │      │
│  │  append-only │  │  async       │  │ kNN brute    │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
│         │                  │                  │             │
│         └──────────────────┴──────────────────┘             │
│                            │                                │
│                   ┌────────▼─────────┐                      │
│                   │  3-tier ACL      │                      │
│                   │ public / source  │                      │
│                   │ / private × N    │                      │
│                   └──────────────────┘                      │
└─────────────────────────────────────────────────────────────┘
```

- **`bus`** records everything that happens. Append-only SQLite with WAL. Time-series.
- **`forge`** turns raw events into structured facts via cloud LLM. Async, so writes never block.
- **`nest`** indexes facts as vectors. SQLite + NumPy brute-force kNN — fast enough up to ~5 K docs; HNSW deferred to v2.0.
- **3-tier ACL** wraps all three stores. `public` is shared knowledge; `source` is admin-private (agent sees, end-user doesn't); `private × N` is one DB per user.

Single-user mode = `public + self-private`. Multi-user mode = `am bot on` creates `private × N` on demand.

### Bots (multi-platform) design

astor treats **people** (user_id) and **bots** (platform_id) as two independent dimensions. Relationship is many-to-many:

- 1 person can have N bots (e.g. TG on phone + DC on desktop + WX for friends, all bind to the same user_id)
- 1 bot can serve M persons (e.g. one WeChat bot where 12 friends each DM independently, each chat_id binds to a different user_id)
- 1 person can have N chat_ids per bot (DMs, groups, threads)

That is why `bot-binding.db` has TWO separate tables:

- `platforms` -- per-bot config (token, base_url, enabled)
- `bindings`  -- per-chat-id → user_id mapping

Why WeChat is special: the `im.bot` protocol is 1:1, meaning one bot serves N users via separate DM chats (chat_id = user). Telegram / Discord are 1:N (one bot, many users, parallel chats).

See `$ASTOR_DIR/bots/DESIGN.md` for the full treatment including the four canonical scenarios (solo / multi-channel / bot-operator / multi-platform-service).

The bots/ directory also holds retired single-platform DBs (archive/) and reserved space for future session history (sessions/) and checks (check/).

## Inspired by, not copied from

Astor-Memory stands on shoulders. We explicitly learned architecture from and avoided copying code from:

- **PowerContext / PowerMem** (oceanbase/powercontext) — search↔context-pack separation (RFCs 0011, 0028, 0051), lifecycle decay, experience↔skill split
- **Letta** (formerly MemGPT) — read-only memory blocks, archival memory pattern
- **mem0** (mem0ai/mem0) — 4-level ACL + scope tags as inspiration for our 3-tier isolation
- **CoALA paper** (arXiv:2309.02427) — cognitive architecture framing for memory systems
- **Mem-π paper** — adaptive memory + cross-LLM transfer insight ("memory strategy is independent of executor")
- **Memory-in-the-Age-of-LLMs** (arXiv:2512.13564) — taxonomy of agent memory types

Full acknowledgements in [`ACKNOWLEDGEMENTS.md`](./ACKNOWLEDGEMENTS.md).

---

## Quickstart

Install:

```bash
pip install astor-memory
```

Initialize a single-user memory store:

```bash
am init
```

Write your first fact:

```python
from astor_memory import write, read

fact_id = write("user prefers concise replies")
print(fact_id)  # → f_8a3b2c1d

# Recall later
hits = read("what does the user prefer?")
for hit in hits:
    print(hit.content, hit.references)
```

CLI equivalent:

```bash
am write "user prefers concise replies"
am read "user preferences"
```

Multi-user mode (optional):

```bash
am bot on           # create per-user private DBs
am bot add-user alice
am bot add-user bob
```

Health check:

```bash
am doctor
# → bus: OK (847 events)
# → forge: OK (provider=openai, latency=320ms)
# → nest: OK (1,247 docs indexed)
```

That's the whole API. Five CLI commands, three Python functions. Everything else is configuration.

---

## What's in the box

Astor-Memory v1.0 ships:

| Feature | Status |
|---|---|
| 3-store triplet (bus + forge + nest) | ✅ |
| 5 CLI commands (`init`, `write`, `read`, `doctor`, `config`) | ✅ |
| REST API + Python import (same code path) | ✅ |
| Env compat (`MEMU_URL` → `ASTOR_FORGE_URL`, etc.) | ✅ |
| 3-tier isolation (public / source / private × N) | ✅ |
| Search ↔ context-pack separation | ✅ |
| Lifecycle: decay + merge + promote | ✅ |
| Revision tracking (append-only, no overwrites) | ✅ |
| Temporal scopes (short_term / long_term / profile) | ✅ |
| Citation-first recall (every `<ref>` embedded) | ✅ |
| **Verdict field** (settled / contested / thin) | ✅ |
| External skill scan/import (read-only reference, no copy) | ✅ |
| Cross-LLM adapter (works with OpenAI / Anthropic / Gemini / DeepSeek / 智谱 / Ollama) | ✅ |
| 15 Core runtime iron rules (default) | ✅ |

Deferred to v1.1+:
- Multi-user dashboard (`am ui`)
- MCP server (FastMCP wrapper)
- LangChain `BaseMemory` adapter
- LLM listwise rerank (opt-in)
- Experience ↔ Skill split
- On-demand generation (Mem-π style)
- Abstain mechanism (Mem-π style)

Deferred to v2.0:
- HNSW index (when `nest` > 100 K docs)

---

## Concepts

The 8 concept blocks below explain *why* each design decision exists. Read them in order — each builds on the last.

### 1. Memory is the foundation of agency

An agent that forgets what it learned yesterday is not an agent — it's a function call. Astor-Memory treats memory as a first-class subsystem with its own runtime guarantees (fail-fast, append-only, revision-tracked), not as a sidecar database.

### 2. Three stores map to three lifecycle stages

- **bus** = "what happened" (raw events)
- **forge** = "what to remember" (extracted facts)
- **nest** = "what to recall" (indexed retrieval)

A write flows bus → forge → nest. A read flows nest ← bus. The three stages are decoupled so each can fail independently without taking down the others.

### 3. Three tiers match real ACL needs

- **public** — shared knowledge, skills, public rules (everyone sees)
- **source** — admin-private (agent sees, end-user doesn't)
- **private × N** — one DB per user (isolation)

We considered 2 tiers (split between public/private only) and 5 tiers (PowerContext's profile/private/short/long/shared). 3 is the minimum that handles 100% of our production scenarios.

### 4. Temporal scope + spatial tier = 6 dimensions

Beyond the 3 spatial tiers, Astor-Memory adds 3 temporal scopes:
- **short_term** — 30-day TTL (today's tasks, recent context)
- **long_term** — permanent (user preferences, decisions)
- **profile** — per-user persona facts

Default is `long_term`. Writes can override with `am write --scope=short_term`.

### 5. Lifecycle self-evolves

Three automatic processes keep memory from growing unbounded:
- **Decay** — `score = relevance × exp(-age_days / 30) × log(1 + access_count)`. Old, unused facts drop in retrieval rank.
- **Merge** — facts with cosine ≥ 0.85 consolidate into one revision. Triggered by `am compact` or nightly cron.
- **Promote** — facts appearing ≥ 3 times across revisions graduate to "rule" layer.

Forgetting is not a defect — being unable to forget is.

### 6. Single-user vs multi-user is a config, not a fork

```bash
am init           # single-user mode (public + self-private)
am bot on         # multi-user mode (public + source + private × N)
```

Same code path. The `bot on` command creates the per-user DB structure on demand. No shim layer.

### 7. Documentation follows the project, not the trend

- 9 doc files total
- GitHub-native Markdown rendering (no SSG)
- lychee CI for link checking
- Per-version docs deferred until project grows > 30 docs (v1.1+)

We deliberately did not adopt zensical / mkdocs / sphinx for v1.0. Smaller surface area = fewer upgrade surprises.

### 8. Acknowledgements are honest

See [`ACKNOWLEDGEMENTS.md`](./ACKNOWLEDGEMENTS.md) for the full list of projects we learned from, depended on, and replaced. Open-source projects we replaced (chromadb, memu.ai SDK, transformers) are credited for the architecture we learned from, not blamed for our divergence.

---

## API

Python (primary):

```python
from astor_memory import write, read, recall, configure

# Write — async-safe, returns fact_id immediately
fid = write("user prefers concise replies", scope="long_term", tier="public")

# Read — returns ranked list with citations
hits = read("user preferences", top_k=5, tier="public")
for hit in hits:
    print(f"[{hit.score:.2f}] {hit.content}")
    print(f"  ref: {hit.references}")  # e.g. ['f_8a3b2c1d:rev_2', 'f_7c4d9e0a:rev_1']

# Recall — context-pack with budget control
pack = recall("what does the user know about X?", max_bytes=4096)
print(pack.content)        # truncated, ranked
print(pack.omitted)        # facts dropped due to budget

# Configure — runtime config (CLI flag > env > yaml > defaults)
configure(llm_provider="anthropic", dedup_window_hours=48)
```

CLI:

```bash
am init                           # Initialize ~/.astor/
am write "text" [--scope=...] [--tier=...]
am read "query" [--top-k=5]
am doctor                         # Health check for bus/forge/nest
am config llm.provider=anthropic  # Set runtime config
am bot on|off                     # Toggle multi-user mode
am compact                        # Run lifecycle (decay/merge/promote)
```

REST (optional):

```bash
# Write — `user` field is required for private tier; ACL resolves role
# from bot-binding.db user_meta.role automatically
curl -X POST http://localhost:7803/v1/write \
  -H "Content-Type: application/json" \
  -d '{"text": "user prefers red", "scope": "long_term", "tier": "private", "user": "alice"}'

# Read — `user_id` filters to one user's private DB
curl -X POST http://localhost:7803/v1/read \
  -H "Content-Type: application/json" \
  -d '{"query": "user preferences", "top_k": 5, "tier": "private", "user": "alice"}'

# Cross-user reads by admin (power user per plan §2624)
curl -X POST http://localhost:7803/v1/read \
  -H "Content-Type: application/json" \
  -d '{"query": "support ticket", "user": "admin", "tier": "private", "user_id": "alice"}'
# → 200 OK (admin can cross-read for support)

# Cross-user reads by regular user (denied)
curl -X POST http://localhost:7803/v1/read \
  -H "Content-Type: application/json" \
  -d '{"query": "anything", "user": "alice", "tier": "private", "user_id": "bob"}'
# → 403 cross_user_forbidden
```

See [`docs/architecture.md`](./docs/architecture.md) § "ACL enforcement flow"
for the full per-request binding pipeline.

**Full REST endpoint reference** (18 endpoints incl. opt3-6 merge dedup v2,
provenance, versioning, restore): see [`docs/api.md`](./docs/api.md).

---

## CLI

### Core
| Command | Purpose |
|---|---|
| `am init` | Initialize `~/.astor/` (or `$ASTOR_DIR`) with 3-tier default config |
| `am write "<text>"` | Append a fact to `bus`; `forge` extracts in background |
| `am recall "<query>"` | Search `nest`; return ranked hits with citations |
| `am doctor` | Health check: bus/forge/nest status, event count, latency |
| `am config <key>=<value>` | Set runtime config (provider, dedup window, etc.) |
| `am compact` | Run lifecycle: decay + merge + promote |
| `am migrate from-memory-bus` | One-shot migrate legacy `memory-bus` SQLite → 9-db layout |

### Multi-user bot management (`am bot ...`)
| Command | Purpose |
|---|---|
| `am bot on / off` | Toggle multi-user mode |
| `am bot add-user <id> [--role user\|admin]` | Create 9-db layout for user |
| `am bot list-users` | List users + roles + on-disk status |
| `am bot promote / demote <id>` | Role transitions |
| `am bot bind-platform <u> <p> <chat_id>` | Lock chat_id to user |
| `am bot unbind <p> <chat_id>` | Release binding |
| `am bot status` | Show mode + bindings |

### Admin tools (`am admin ...`, first_admin only)
| Command | Purpose |
|---|---|
| `am admin whoami` | Show current first_admin lock |
| `am admin audit-log [--actor] [--user] [--action] [--since]` | Query audit db |

### Platform / token management (`am platform ...`, bot-binding.db)
| Command | Purpose |
|---|---|
| `am platform list` | List platforms (TG/DC/WeChat accounts + tokens) |
| `am platform list-users` | List user_meta rows |
| `am platform list-bindings` | List active bindings |
| `am platform resolve <platform_id> <chat_id>` | Resolve chat_id → user_id |
| `am platform token-get / token-set <platform_id> [token]` | Read or update token |
| `am platform bind <platform_id> <chat_id> <user_id>` | New binding |
| `am platform unbind <platform_id> <chat_id>` | Revoke binding |
| `am platform add-user <user_id> <alias> [--role] [--plan]` | Add user_meta row |
| `am platform verify` | Verify 6 invariants on bot-binding.db |

All commands support `--json` for structured output. v0.3.0 ships **21 subcommands** across 3 namespaces.

---

## Configuration

Priority: **CLI flag > env > `~/.astor/config.yaml` > defaults**

```yaml
# ~/.astor/config.yaml
llm:
  provider: openai   # openai | anthropic | gemini | deepseek | zhipu | ollama
  model: gpt-4o-mini
  api_key_env: OPENAI_API_KEY

dedup:
  window_hours: 24

tiers:
  default: public
  admin_only: source

cron:
  role: operator     # operator | admin
  # operator role: type enum-validated (config-defined whitelist)
  # admin role: unrestricted
```

Environment variable overrides:

```bash
export ASTOR_LLM_PROVIDER=anthropic
export ASTOR_LLM_MODEL=claude-3-5-sonnet
export ASTOR_DEDUP_WINDOW_HOURS=48
am read "..."
```

---

## Acknowledgements

Open-source projects we deliberately do **not** depend on (inspired our design):
- **chromadb** — inspired `astor_memory.nest`; we built native SQLite + NumPy kNN
- **memu.ai SDK** — inspired `astor_memory.forge`; we built cloud-LLM-agnostic extractor
- **transformers + torch** — avoided to keep install footprint < 50 MB

Open-source projects we depend on (vendor-neutral, OSI-approved):
- **flask** (BSD), **requests** (Apache-2.0), **numpy** (BSD), **fastembed** (Apache-2.0), **pydantic** (MIT), **pytest** (MIT)

Projects we learned architecture from (concept borrow only):
- **PowerContext**, **Letta**, **mem0**, **CoALA paper**, **Mem-π paper**, **Memory-in-the-Age-of-LLMs** paper

Full credits in [`ACKNOWLEDGEMENTS.md`](./ACKNOWLEDGEMENTS.md).

---

## How we compare

Independent research and products validate our architecture choices:

**Activity Frames paper** (arXiv:2608.05784, 2026-08-06) measured Agent re-derivation cost at **60-343×** the original routine cost. Astor-Memory's revision tracking + append-only event log eliminates this penalty by making decisions durable and citable — once a fact is in `bus`, it's never re-derived.

**Atlaso's verdict system** (atlaso.ai, ProductHunt #4 2026-08-05) tags memories as `settled` / `contested` / `thin` to expose epistemic uncertainty. We adopted this as a `verdict` field on every Fact, complementing our 3-tier ACL (spatial: who sees it) with confidence-grading (qualitative: how sure are we).

We don't compete with these — we absorb what works and ship it self-hosted under MIT. Atlaso is closed cloud-sync; Inventory (myinventory.site) is a Mac-only local indexer; Activity Frames is a research paper. Astor-Memory is the self-owned, vendor-neutral, multi-platform synthesis.

---

## License

MIT — see [`LICENSE`](./LICENSE).

---

## See also

- [`docs/architecture.md`](./docs/architecture.md) — deep dive on 3-store × 3-tier + 11 absorbed insights
- [`docs/migration.md`](./docs/migration.md) — upgrade guide from old `memory-bus` system
- [`docs/agent-adapters.md`](./docs/agent-adapters.md) — MCP / LangChain / REST / Python integration
- [`docs/faq.md`](./docs/faq.md) — frequently asked questions
- [`docs/troubleshooting.md`](./docs/troubleshooting.md) — common errors and fixes
- [`docs/contributing.md`](./docs/contributing.md) — for contributors
- [`CHANGELOG.md`](./CHANGELOG.md) — release history
