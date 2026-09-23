# Astor-Memory

> **Self-owned memory system for AI agents.** Three stores, three tiers, federated public tier, zero vendor lock-in.

> **中文文档:** [README.zh-CN.md](README.zh-CN.md) | **Architecture 中文:** [docs/architecture.zh-CN.md](docs/architecture.zh-CN.md) | **API 中文:** [docs/api.zh-CN.md](docs/api.zh-CN.md) | **Dashboard:** [docs/dashboard.md](docs/dashboard.md)

> **Coming: peer-to-peer public tier federation (ADR 0008).** Multiple astor instances owned by trusted peers will sync their `public` tier directly — no central server, no remote-direct RPC. Per-peer trust (0-100) + per-topic weights let curating admins decide what to share with whom. Private / source tiers never cross the boundary. F1 lands `/v1/peer/*` REST endpoints + `am peer` CLI on top of the existing `peer_relationships.py` schema.

---

## Who this is for

You run one bot server. **Your family and friends each get their own private
agent** — they DM the bot from their own WeChat / Telegram / Discord account
and the bot talks back with **a memory that only they can see**. Mom's
birthday notes never leak to the friend group, and the poker-game brief
you wrote for a buddy does not contaminate your cousin's career advice.

The deployment shape that this enables is the one we actually run
ourselves and ship-tested first:

```
+--------------------------------------+
|  One astor-memory server (port 7803) |
|                                       |
|   first_admin    first_admin tier     |
|   mom            private_mom tier     |
|   friend_a       private_friend_a     |
|   friend_b       private_friend_b     |
|   cousin         private_cousin       |
|                                       |
|   shared memory = public + source     |
|   per-user memory = private_<user_id> |
+--------------------------------------+
```

Each user has their own SQLite database, their own ACL grants, their own
bazi/trading/personal facts, and (their own bot binding). The admin (you)
sees source.db (the agent's own self-pattern + general skills) plus any
private tier the user has explicitly granted.

This is not a multi-tenant SaaS. It is **a single-server memory for a small
group of people who trust each other enough to share the bot**. Privacy
is enforced by ACL at the matrix level (see [ACL v1.2 hardening](docs/acl-v1.2-hardening.md)),
not by trusting each user to behave.

---

## Why we built this

Modern AI agents need memory. The current options all force a trade-off:

| Option | What you get | What you give up |
|---|---|---|
| **RAG-only** (vector store) | Simple retrieval | No event log, no fact extraction, no per-user isolation |
| **Letta** (Memory Blocks) | Read-only protection + archival | Heavy runtime, opinionated architecture |
| **mem0** (4-level ACL) | Multi-tenant + scope tags | Cloud-coupled, async-by-default |
| **PowerContext / PowerMem** | Search↔context-pack separation | Pre-1.0, Chinese-language-first, no self-host |
| **Hand-rolled** (DIY vector + extract stack) | Total control | Heavy venv, multiple server processes, fragile upgrades |

**Astor-Memory** exists because we hit all four pain points in production across 33 ship sessions and 50+ cron jobs running on a self-hosted agent. We learned:

1. **Three stores are the minimum viable decomposition.** An append-only event log (`bus`), an LLM fact extractor (`forge`), and a vector store (`nest`) map cleanly onto "what happened → what to remember → what to recall." More layers create coordination overhead; fewer collapse semantics.
2. **Three tiers of isolation match real ACL needs.** Public knowledge (skills, rules) + admin-private (agent sees, user doesn't) + per-user private (N isolated DBs) — no more, no less.
3. **Vendor lock-in is the silent killer.** Vector-DB migrations, proprietary SDK breaking changes, transformer runtime bloat — every dependency we picked bit us within 6 months. The lesson: own the code or own the risk.

If you've felt any of these, Astor-Memory is built for you.

---

## What makes Astor-Memory different

| Differentiator | What it means |
|---|---|
| **3-store triplet** | `bus` (append-only event log) + `forge` (LLM fact extraction) + `nest` (vector store). Each does one thing well. |
| **3-tier isolation** | `public` + `source` (admin-private) + `private × N` (per-user). Opt into multi-user mode with one command. |
| **Self-owned code** | Pure Python + SQLite + NumPy. No proprietary vector DB, no proprietary memory SDK, no transformer runtime, no torch. Install footprint < 50 MB. |
| **Vendor-neutral LLM** | `forge` works with OpenAI / Anthropic / Gemini / DeepSeek / 智谱 / Ollama. Same recall output, any provider. |
| **Citation-first** | Every context-pack output embeds `<ref memory_id revision_id>` so agents can verify what they read. |
| **Lifecycle that self-evolves** | Ebbinghaus-style decay + cosine-merge + promote-after-3-occurrences. Agents actively forget, merge, and graduate facts to rules. |
| **Append-only + revision tracking** | Updates create new revisions; old content stays queryable for audit. No silent overwrites. |
| **Cross-LLM adapter** | A recall() output trained on Qwen2.5-7B still works as guidance for GPT-5-mini. Insight from Mem-π paper. |
| **Peer-to-peer public tier sync** | Multiple Astor instances sync their `public` tier directly — no central server. Per-peer trust (0-100) + per-topic weights let you curate what to share with whom. Private / source tiers never cross the boundary. See [ADR 0008](docs/adr/0008-peer-public-network.md). |

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

### 3-zone architecture (success / failure / lesson)

Beyond the spatial tier (public/source/private × N), astor classifies
every fact into an **outcome zone** that drives the recall query
language. The three zones are:

- **`success_pattern`** — a reusable method / pattern / experience that
  worked. Examples: "patch tool 替换 write_file 走通了 — 不再被
  indent drift 卡住", "unlock_trade: 调 SDK 的 unlock_trade + .env
  MOOMOO_PASSWORD".
- **`failure_pattern`** — a wasted path / known-bad idea / R-class
  rule. Examples: "走不通 debug 客户端 fallback", "patch tool 加 4
  空格误判 — 改用 write_file".
- **`lesson`** — a complete failure → root-cause → fix triplet (e.g.
  "critical bug — fix is null check" or "崩溃 + 根因是 X").

The `forge/extractor.py` outcome → kind mapping is wired end-to-end:
`astor_classify_outcome` runs on every `/v1/write` body, and the
extractor overrides the LLM-output kind with the zone kind. Recall
filters by zone via the `kinds=` parameter:

```python
# Auto-suggested zone from query keywords (hermes-side heuristic):
#   "失败 / 出错 / 走不通 / crash / fail / 报错" → failure_pattern + lesson
#   "成功 / ship / verified / 接通 / working"   → success_pattern + user_preference
#   "教训 / 记得 / 切记 / lesson"               → lesson + failure_pattern
am recall --kinds failure_pattern "上次 patch tool 走不通"
am recall --kinds success_pattern "verify 模式怎么配"
```

#### Zone shortcuts (v1.15.1)

For agents that think in outcome zones rather than raw kind lists, the
`--zone` flag is a one-word shortcut for `--kinds`:

| Zone       | What it covers                                                 | When to recall                                                                 |
|------------|-------------------------------------------------------------------|---------------------------------------------------------------------------------|
| `failure`  | `failure_pattern` — things that didn't work, don't repeat them    | I hit a wall, want to see if this approach was tried before                    |
| `success`  | `success_pattern` — verified approaches, recipes that ship        | I want a proven recipe for this problem                                        |
| `lesson`   | `postmortem,lesson` — root-cause + fix triplets, severity ≥ 0.90 | After a critical bug — what to do next time, what NOT to do                    |
| `all-zones`| `user_preference,failure_pattern,success_pattern,postmortem,lesson` | Cross-zone sweep (rare; default zone filters usually beat this)              |

```bash
am recall --zone failure "上次 patch tool 走不通"
am recall --zone success "verify 模式怎么配"
am recall --zone lesson  "OpenD Watchdog 服务挂了 怎么搞"
am recall --zone all-zones "zone 类召回怎么用"
```

`--kinds` always wins when both are passed (escape hatch for advanced
callers who need a raw kind list). Agent-loop recommendation: **when you
hit a wall, `am recall --zone failure "<keywords>"` first — if that
returns nothing, escalate to `--zone lesson` for postmortem entries.**

### Recall discipline for agents (v1.15.1)

The `--zone` flag is one half of a recommended agent loop. The other
half is **when to recall at all** — recall is a backend tool, not a
default preamble. The discipline below applies to any agent that calls
astor, regardless of task domain:

```text
┌──────────────────────────────────────────────────────────────┐
│ Default: do NOT pre-warm recall. Run the task on LLM reasoning.│
│                                                              │
│   ↓ hit a wall (error / timeout / wrong result / 反爬)        │
│                                                              │
│ am recall --zone failure "<task-domain keywords>"             │
│   ↓ empty                                                    │
│ am recall --zone lesson  "<task-domain keywords>"             │
│   ↓ empty                                                    │
│ am recall --zone success "<task-domain keywords>"             │
│   ↓ empty                                                    │
│ THEN: fall back to web_search / re-derive from first principles│
└──────────────────────────────────────────────────────────────┘
```

Three rules worth internalizing:

1. **Default off.** If the task goes smoothly, do not recall. Recall
   is for failure recovery, not for warming up context.
2. **One recall per failure.** Do not loop on `--zone failure` with
   the same query — if it returns empty, escalate the zone (failure
   → lesson → success), not the keywords.
3. **Zone over keyword.** `--zone failure` is a signal-density
   shortcut that beats raw `--kinds failure_pattern` only because
   it carries semantic intent ("I am here because I failed"). The
   `--kinds` form is an escape hatch, not the default.

Concrete examples (any task domain works — only the keywords change):

| Task                                  | Trigger signal       | Recall call                                              |
|---------------------------------------|----------------------|----------------------------------------------------------|
| Fetch a WeChat article                | 反爬 / captcha / 抓不到 | `am recall --zone failure "微信公众号 抓"`                |
| Place a moomoo order                 | EOrder / timeout     | `am recall --zone failure "opend place_order"`           |
| Schedule a cron                      | 不触发 / 静默失败    | `am recall --zone failure "hermes cron 不触发"`           |
| Patch Python source                  | IndentationError     | `am recall --zone failure "patch tool 缩进漂移"`          |
| Pull stock quote                     | 接口错 / 空数据      | `am recall --zone failure "akshare 接口错"`              |

The shell is generic; only the keyword list is task-specific. That is
the point — `--zone failure "X"` is a one-line mental model an agent
can carry into any new domain without learning a new skill.

This is the bridge between the LLM and the human operator's mental
model — without it, every fact lands as `kind=fact` and the 3-zone
taxonomy never gets populated (Ship F, v1.13.1, was dead code until
the capture_intent hook wired it up in v1.14.21, then re-tuned in
v1.14.36 to call `success_pattern` instead of `user_preference`).

### Explicit-public principle (admin-aware)

Public tier is the most permissive — anyone in the deployment can
read it. Wittingly or unwittingly, that's where accidental data
leaks land. The principle is:

> **Public only contains content that is explicitly meant to be
> shared: methods, patterns, workflows, reference info, plus a
> genuine "I want this public" signal (the `workflow / 方法 / 接口 /
> SDK / api / 步骤 / 怎么 / 如何` keyword set).**

`server.py._astor_classify_intent` enforces this at write time:

- **Strong-signal demote** (force `tier=private`): text contains
  personal pronouns (`我 / 我的 / 自己 / my / i am`), financial
  markers (`$ / € / bought / sold / AAPL / TSLA / NVDA / SPY`),
  daily markers (`今天 / 昨天 / today / tonight / 10am`), or emotion
  words (`happy / sad / 累了 / 崩溃`).
- **Method signal** (`_METHOD_PATTERNS`): text contains any of the
  15+ method-intent keywords or the `怎么 \w{2,}` how-to pattern
  or one of the SDK verb allowlist (`place_order`, `unlock_trade`,
  `accinfo_query`, etc.). Stays `public`.
- **No signal**: defaults to `private` (was `public` pre-fix).
- **Admin also applies** (Ship v1.14.36): the pre-fix R236 short-
  circuit `if user == 'admin': return None` was a leak vector
  ("我的 TFSA 余额是 $1234" landed as public fact under admin).
  Now admin and users go through the same classify logic.

This is **explicit-public**, not default-public. Operators must
demonstrate method intent at write time to land in `public`; the
absence of that signal defaults to `private`.

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

## Four integration paths (verified 2026-09-16)

Astor-Memory is agent-agnostic. **Four verified entry points**, all using the same REST surface at `/v1/*`:

| Path | How | When to use |
|---|---|---|
| **1. Telegram bot** | `AstorClient(transport='bot', platform='telegram')` via `bot-binding.db` | Telegram DM bot forwarding user requests to astor |
| **2. Discord bot** | `AstorClient(transport='bot', platform='discord')` via `bot-binding.db` | Discord DM bot forwarding user requests |
| **3. WeChat (5 bots)** | `AstorClient(transport='bot', platform='weixin')` via `bot-binding.db` | WeChat iLink bot — 1 bot → N users, chat_id = user |
| **4. EvoX / Claude Code / Codex direct** | `AstorClient(transport='direct')` | Non-Hermes agents integrating without a bot platform |

`AstorClient` (Python) auto-derives the `X-Actor` header from `self.user_id` — server resolves it via `bot-binding.db.user_meta.role` (admin / user) and `subscription_plan` (free / vip / power). Free users can write `public` tier; only admin can write `source`; private writes require `acc_id == caller.user_id` ACL guard.

All four paths verified end-to-end 2026-09-16: recall works (200 + hits), write works (returns `fact_ids: [int]`), cross-user private facts do NOT leak (probe-owner-only recall, zero hits across other user accounts).

### MCP server integration (for AI agents)

`astor_memory/mcp_server_extension.py` is a drop-in monkey-patch for any MCP gateway that exposes `list_tools()` / `call_tool()` server-side. After install, the gateway surfaces `astor_auto_observe` as a native tool — every tool call is auto-captured to astor bus for audit + downstream learning.

```python
# Phase E (commit 459296f) install snippet
import sys, os
_p = os.environ.get("ASTOR_MEMORY_SRC")
if _p:
    sys.path.insert(0, _p)
    sys.path.insert(0, os.path.join(_p, "astor_memory"))
    import mcp_server_extension as _ext
    _ext.install(sys.modules["__main__"])
```

Works for Codex / Claude Code / any MCP-compliant agent runtime.

## Roadmap — peer-to-peer public tier sync

The detailed design lives in [ADR 0008](docs/adr/0008-peer-public-network.md) (status: accepted, 2026-09-19). Key decisions:

- **Identity**: ed25519 keypair per peer, `peer_id` format `astor:<32hex>`. Storage at `~/.astor/identity/relationships.db` (separate from memory DBs).
- **Trust**: 0-100 scale with tiered semantics — 0 = blacklist (auto-reject), 30 = default for new peers (quarantine), 50+ = auto-accept, 70+ = KEEP trust on rekey, 90+ = broadcast-back. Per-topic weights further modulate trust.
- **Sync model**: pull-based manifest diff on the **public** tier only. Private / source / repo tiers never cross the boundary. Conflict policy: last-write-wins by `last_confirmed_at`, ties keep both + dedup at read time by `stable_id`.
- **Companion**: demand-driven PPS (Peer Public Search) for on-demand cross-peer search — signed ed25519 request, trust ≥ 50 gate, friend opt-in, 7-day timestamp freshness, 1MB / 3s per-peer caps. Read-only; manual adopt to persist.
- **Schedule**:
  - **F1** (current): git-track `peer_relationships.py` + `peer_search.py`, add `/v1/peer/*` REST endpoints + `am peer` CLI.
  - **F2**: background sync daemon (manifest pull every 5 min per peer).
  - **F3**: gossip overlay, vector clocks, broadcast-back for trust ≥ 90.
  - **F4** = GA: multi-region replication, CRDT merges.

The earlier R12593 design (single shared token in `bot-binding.db.peers` table) was replaced by this ADR — see commit log for the rewrite history. Implementation files for F1: `astor_memory/_internal/peer_relationships.py` (friend + trust + rekey), `astor_memory/_internal/peer_search.py` (PPS).

## Inspired by, not copied from

Astor-Memory stands on shoulders. We explicitly learned architecture from and avoided copying code from:

- **PowerContext / PowerMem** (oceanbase/powercontext) — search↔context-pack separation (RFCs 0011, 0028, 0051), lifecycle decay, experience↔skill split
- **Letta** (formerly MemGPT) — read-only memory blocks, archival memory pattern
- **mem0** (mem0ai/mem0) — 4-level ACL + scope tags as inspiration for our 3-tier isolation
- **CoALA paper** (arXiv:2309.02427) — cognitive architecture framing for memory systems
- **Mem-π paper** — adaptive memory + cross-LLM transfer insight ("memory strategy is independent of executor")
- **Memory-in-the-Age-of-LLMs** (arXiv:2512.13564) — taxonomy of agent memory types
- **akitaonrails/ai-memory** (2026-09 survey) — shared-wiki-as-Source-of-Truth framing; "typed edges" (`fixes`/`causes`/`contradicts`) reinforced our `tags` design for cross-fact relations. See [docs/research-survey-2026-09-08.md](./docs/research-survey-2026-09-08.md) for the architecture comparison + what we deliberately did not copy.

Full acknowledgements in [`ACKNOWLEDGEMENTS.md`](./ACKNOWLEDGEMENTS.md).

---

## Quickstart

Install with pip:

```bash
pip install astor-memory
```

Or use a bundled install script — creates a venv, fetches from GitHub,
initializes the data dir, and sets up `am` in one step:

**Linux / macOS** (`install.sh`):
```bash
curl -fsSL https://raw.githubusercontent.com/<repo_owner>/<repo_name>/main/scripts/install.sh | bash
# or pin a version:
curl -fsSL https://raw.githubusercontent.com/<repo_owner>/<repo_name>/main/scripts/install.sh | bash -s v1.13.1
# interactive: prompts for data dir
./scripts/install.sh
# non-interactive (CI/automation):
./scripts/install.sh --non-interactive
# custom data dir:
./scripts/install.sh --dir /opt/astor/data
# verify:
./scripts/install.sh --check
# uninstall:
./scripts/install.sh --uninstall
```

**Windows** (`install.ps1`, PowerShell 5.1+):
```powershell
# From GitHub raw (one-liner)
iwr -useb https://raw.githubusercontent.com/<repo_owner>/<repo_name>/main/scripts/install.ps1 | iex
# Or local
.\scripts\install.ps1
.\scripts\install.ps1 v1.13.1
.\scripts\install.ps1 -NonInteractive
.\scripts\install.ps1 -Dir 'D:\astor\data'
.\scripts\install.ps1 -Check
.\scripts\install.ps1 -Uninstall
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

## MCP server (for AI agents)

Astor-Memory ships a generic MCP (Model Context Protocol) server adapter
in the ``astor_memory_mcp`` sub-package. Any MCP-compatible agent
framework (EvoX, Hermes, Claude Desktop, Cursor, Aider, ...) can expose
the local Astor REST API as MCP tools by installing this package.

### Install

```bash
# Install everything (core + MCP sub-package) in one go.
pip install "astor-memory[mcp]"

# Or if you already have astor-memory installed, the MCP server ships
# in the same wheel when you upgrade.
pip install --upgrade "astor-memory[mcp]"
```

### Configure

The MCP server picks up standard env vars. Defaults work for any
single-user install:

```bash
# Where the local astor REST server listens.
export ASTOR_MEMORY_URL="http://127.0.0.1:7803"

# Whose data the MCP server acts as. Defaults to "admin".
export ASTOR_MEMORY_USER_ID="admin"

# Optional: only needed if you have multiple astor installs and want
# a non-default bot-binding.db path.
export ASTOR_BOT_BINDING_DB="/path/to/bot-binding.db"
```

### Register with your agent framework

EvoX (canonical example — works for any other framework with the same JSON-RPC + stdio protocol):

```json
{
  "mcp_servers": {
    "astor-memory": {
      "command": "python",
      "args": ["-m", "astor_memory_mcp.server"]
    }
  }
}
```

After registering, the agent framework can call these six tools:

| Tool | Purpose |
|---|---|
| `astor_health` | Reachability check + dashboard summary |
| `astor_read` | Single-record fetch by `memory_id` |
| `astor_recall` | Semantic / text search over facts |
| `astor_context` | Return the trusted identity + capability summary |
| `astor_capabilities` | Advertise the boolean capability matrix |
| `astor_resolve_binding` | Resolve `(platform_id, chat_id)` to its bot-binding |

The MCP server is **read-only by design** — write/forget are deliberately
not exposed. To capture new facts, use the CLI (`am write`) or your
operator shell; never through MCP.

### Bot-binding integration

When a bot (Telegram / Discord / WeChat / Slack / ...) forwards a user
message to your agent, the MCP server resolves the binding through your
local ``bot-binding.db`` (a separate SQLite SSoT). It then tags the
result with the binding's ``user_id`` so astor reads from the right
namespace. See ``astor platform bind --help`` for managing bindings.

### Source layout

The MCP server source lives in the same git repo as ``astor-memory``:

```
astor-memory/
├── astor_memory/        ← core package (REST server, agent_identity, ...)
├── mcp/
│   └── astor_memory_mcp/  ← MCP server sub-package
│       ├── server.py
│       ├── tests/
│       └── README.md
└── pyproject.toml       ← one version, both packages
```

Both packages share the same version number from ``pyproject.toml``
(``astor-memory`` 1.14.35 at the time of writing).

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
| **Zero-LLM mode** (FTS5 BM25 + lexical-only recall, no embedding / no API key required) | ✅ |
| **3-zone architecture** (success_pattern / failure_pattern / lesson — outcome-driven kind routing) | ✅ |
| **Explicit-public principle** (admin-aware demote: no method signal → `private`, never `public`) | ✅ |
| **Zone-filtered recall** (`/v1/read kinds=success_pattern,failure_pattern` + auto-suggested zone from query keywords) | ✅ |

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
| `am recall "<query>"` | Search `nest`; return ranked hits with citations (default `--zone success`) |
| `am recall-auto "<error>"` | Paste an error or pipe via stdin; auto-walks failure → lesson → success zones |
| `am doctor` | Health check: bus/forge/nest status, event count, latency |
| `am config <key>=<value>` | Set runtime config (provider, dedup window, etc.) |
| `am compact` | Run lifecycle: decay + merge + promote |

### Recall zones (v1.15.x)

`am recall` filters by outcome zone so the most relevant facts surface
first. Pass `--zone` to switch:

| `--zone` | Filters to kinds |
|---|---|
| `success` (default) | `success_pattern` — proven recipes |
| `failure` | `failure_pattern` — failed approaches, don't repeat them |
| `lesson` | `postmortem,lesson` — root-cause + fix triplets |
| `all-zones` | `user_preference,failure_pattern,success_pattern,postmortem,lesson` |
| `none` | raw fact rows (escape hatch) |

For error-driven debugging, `am recall-auto "<error message>"` walks
the zones automatically — failure first, then lesson, then success —
and prints the top-3 hits from the first non-empty zone. Returns
`rc=0` on hit, `rc=2` on miss.

### Decay sweep (v1.15.3)

`am decay-sweep run --since-canonical-id <N>` only sweeps facts with
`memory_canonical.id > N`, making it cheap to run after every cron
tick. Pair with `scripts/astor_decay_event_trigger.py` for an
event-driven trigger that no-ops when no new facts have been written.

### Optional: jev relevance rerank (v1.15.4, opt-in)

`am recall --jev-relevance on` enables an external rerank via the
[jev](https://d.ai/jev) shadow infrastructure. **Default off.** When
on, the CLI calls `jev_client.jev_call` with the top candidate
snippets and logs the verdict for precision analysis. **Requires**:

- `D:\AI\scripts\admin\jev\` (or equivalent) on `PYTHONPATH`
- `typesafe-sdk` Python package installed
- `TYPESAFE_API_KEY` env var set
- Running `jev` server reachable

If any of the above is missing, the flag is a silent no-op — astor
returns the original hybrid ranking unchanged. **Installs of
`astor-memory` without the jev shim see no behavior change**; the
flag exists only for operators running the jev shadow stack.

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
- [`docs/migration.md`](./docs/migration.md) — migrate from mem0 / Letta / Zep / MemGPT / ChromaDB / Pinecone / Weaviate / plain files
- [`docs/agent-adapters.md`](./docs/agent-adapters.md) — MCP / LangChain / REST / Python integration
- [`docs/faq.md`](./docs/faq.md) — frequently asked questions
- [`docs/troubleshooting.md`](./docs/troubleshooting.md) — common errors and fixes
- [`docs/contributing.md`](./docs/contributing.md) — for contributors
- [`CHANGELOG.md`](./CHANGELOG.md) — release history
