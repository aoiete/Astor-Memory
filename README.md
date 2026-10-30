# Astor-Memory

[![CI](https://github.com/aoiete/ASTOR-Memory/actions/workflows/ci.yml/badge.svg)](https://github.com/aoiete/ASTOR-Memory/actions/workflows/ci.yml)
[![link-check](https://github.com/aoiete/ASTOR-Memory/actions/workflows/link-check.yml/badge.svg)](https://github.com/aoiete/ASTOR-Memory/actions/workflows/link-check.yml)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> **The peer-to-peer public memory layer for AI agents.** Your private memory stays on your machine; public knowledge is shared peer-to-peer — no central server, no vendor lock-in.

> **中文文档:** [README.zh-CN.md](README.zh-CN.md) · **Dashboard:** [docs/dashboard.md](docs/dashboard.md) · **Architecture:** [docs/architecture.md](docs/architecture.md) · **API:** [docs/api.md](docs/api.md)

---

## What this is

Every AI agent relearns the same things in isolation. Astor-Memory is a
self-hosted memory server with a peer-to-peer public layer: your agents'
private memory stays on your machine, while knowledge you choose to
share gossips across a network of trusted peers — no central server, no
single custodian of the commons.

```
[node A — your server] <== gossip (public tier only) ==> [node B — peer's server]

private_<you>, source    private_<peer>, source
(never leave this machine) (never leave this machine)
```

### 1. One machine, many users, many bots

Not only a single-user install. A node serves several users at once:
each gets their own SQLite database, their own ACL grants and their own
bot bindings (Telegram, Discord, WeChat, Feishu, MCP clients, plain
HTTP). The ACL is enforced at the matrix level before any read or
write, so users cannot see each other's memory and one user's bot
cannot reach another user's tier. The operator sees `source` plus
whatever a user has explicitly granted.

### 2. Public and private are different things, and the code knows it

The tiers are separated in the store, not by convention: a personal
fact cannot be promoted into the public tier at all. What reaches the
public tier is the shareable part — methods, patterns, models,
workflows, lessons, successes and failures — while what is personal
stays in its owner's private tier, with paths and identifiers scrubbed
on the way out. Distillation mines the private tier for what is worth
publishing; nothing leaks by accident.

### 3. Peers search each other's public tier

A node can search the public memory of peers it trusts, and the
results come back the way a local search would. A method, a model or a
pattern found on one node becomes available on the others — that is the
point of the network.

Trust is explicit, not assumed: unknown peers land in quarantine for
operator review instead of being rejected outright, and peer traffic is
rate-limited per peer so one noisy node cannot flood the commons.
Per-peer trust (0–100, default 30) plus per-topic weights decide what
flows where.

### 4. And a relay for the nodes behind NAT

Two nodes that cannot reach each other directly can still talk through
a relay. The relay is a postman, not a reader: messages are sealed
before they arrive, and it has no way to read them or to call anybody
else. A public relay can carry traffic without being able to see it.

### Interfaces

CLI (`am`), REST (`/v1/...`), MCP tools, and a dashboard. The same
server also acts as the endpoint for external agent platforms (Muse,
custom HTTP clients, Slack adapters), giving them a persistent memory
backend with ACL-enforced user isolation and per-call audit logging.
Privacy is enforced by the ACL at the matrix level (see
[ACL hardening](docs/acl-v1.2-hardening.md)), not by trusting each user
to behave.

---

## Why we built this

Modern AI agents need memory. Existing solutions force you to choose
between capability and autonomy:

| Option | You get | You lose |
|---|---|---|
| **Pure RAG** (vector store) | Simple retrieval | No event log, no fact extraction, no user isolation |
| **Letta** (Memory Blocks) | Read-only protection + archival | Heavy runtime, strict architecture |
| **mem0** (4-tier ACL) | Multi-tenant + scope labels | Tight cloud coupling, async by default |
| **Astor-Memory** | Three SQLite stores + three tiers + ACL + event log + audit + PII defense + decay + dashboard + REST + peer-to-peer `public` gossip + open data on disk | Your private tiers stay on your machine (by design, not by policy) |

What you actually get:

- **Bus / Forge / Nest** three-store architecture with cross-tier
  promotion (per-user → source / public).
- **ACL matrix** at the per-actor × per-tier cell, gate enforced before
  any DB read or write.
- **Event log** — every fact write is appended as an immutable event
  with provenance (origin platform, agent, kind). Replayable.
- **PII defense** — 44-pattern scanner (API keys, emails, phones, chat
  IDs, tokens) wired into the write endpoint with `redact` and `block`
  policies. Audit-safe via `sha256[:12]` fingerprinting.
- **Decay + HOT promotion** — facts that are never recalled decay
  faster; facts that hit 3+ successful invocations promote to HOT
  with a bounded relevance boost. Operator-authored surfaces
  (mental_model, knowledge_page) are excluded from decay.
- **Memory recall composition** — lexical (BM25) + vector (multilingual
  embed) + MMR diversity rerank + HOT boost + cross-tier promotion +
  ECV chain boost + meta-recall (success / failure / lesson patterns
  auto-injected into every read). Tunable per env var.
- **Dashboard** — real-time health, recent-capture panel (kind / tier
  / platform axes with chunked pagination), peer-friends panel,
  Recall debugger with live /v1/read echo, Knowledge Pages panel,
  Mental Models panel, growth + distribution aggregates.
- **Public REST** — `/v1/{health, identity, dashboard, write, read,
  consult, skill, peer, binding, episode, bitemporal, audit, staleness,
  forget}` plus dashboard HTML at `/dashboard/`.

---

## Deployment shape

One server, one port (default 7803), one `ASTOR_DIR` directory on disk:

```
<ASTOR_DIR>/
├── public/memory/      # astor_bus_public.db + astor_nest_public.db
├── source/memory/      # astor_bus_source.db + astor_nest_source.db
├── users/<id>/memory/  # astor_bus_<id>.db + astor_nest_<id>.db  (per user)
├── audit/              # audit_log.sqlite (cross-tier)
├── logs/                 # server.log + side-log + watch logs
└── identity/            # keypair.json (auto-generated on first boot)
```

The server binds to `127.0.0.1:7803` by default. For remote access,
front it with a Cloudflare Tunnel, nginx, or a peer-anything reverse
proxy — the server does not bind to `0.0.0.0` unless you pass
`--host 0.0.0.0`.

Install from source (not yet published to PyPI) with a single
command; `am` becomes available as a console script:

```bash
git clone https://github.com/aoiete/Astor-Memory.git
cd Astor-Memory
pip install -e .
```

Install it editable. A non-editable install puts a copy of the package
in site-packages, and then a checkout, a site-packages copy and a
runtime tree all hold code, and the question "which one is the server
running" has no good answer. Editable makes the checkout the only
copy: `git pull` is the upgrade.

Optional `[muse]` extra installs the Muse adapter package. Once the
project is published to PyPI this becomes plain `pip install
astor-memory`.

Requires Python 3.10–3.13 (3.14 is not yet accepted by the build
metadata).

---

## Use it from Claude / Cursor (MCP)

Three tools, no API key, no HTTP:

```
astor_recall   semantic recall across the public / source / private tiers
astor_write    append a fact -> bus event -> candidate -> canonical
astor_status   counts only, never fact content
```

```bash
am mcp serve          # stdio transport, starts immediately
```

**Claude Desktop** — add to `claude_desktop_config.json`
(`~/Library/Application Support/Claude/` on macOS,
`%APPDATA%\Claude\` on Windows):

```json
{
  "mcpServers": {
    "astor-memory": {
      "command": "am",
      "args": ["mcp", "serve"]
    }
  }
}
```

**Claude Code**:

```bash
claude mcp add astor-memory -- am mcp serve
```

**Cursor** — `.cursor/mcp.json` in your project:

```json
{
  "mcpServers": {
    "astor-memory": {
      "command": "am",
      "args": ["mcp", "serve"]
    }
  }
}
```

If `am` is not on PATH, use the absolute path to the console script
your install produced (`python -c "import shutil; print(shutil.which('am'))"`).

`astor_recall` searches every tier the caller may read and labels each
result with its tier. `astor_write` takes `"tier"` explicitly and
defaults to `public`; pass `"tier": "private"` with `"user"` to write
one account's private store. ACL denies cross-user access in both
directions, so a client cannot reach another user's memory.

---

## Quick start

```bash
# Install (not yet on PyPI — install from a checkout, editable)
git clone https://github.com/aoiete/Astor-Memory.git
cd Astor-Memory
pip install -e .
# or, with the Muse adapter
pip install -e ".[muse]"

# Windows: check the host can run astor BEFORE starting anything.
# Takes a second. If the Visual C++ runtime is too old for onnxruntime,
# `am preflight --fix` downloads and installs it for you.
am preflight --fix

# Initialize a fresh runtime
export ASTOR_DIR=/var/lib/astor
mkdir -p "$ASTOR_DIR"
python -m astor_memory.cli.main init

# Add an admin user
python -m astor_memory.cli.main user add admin --role first_admin

# Start the server
python -m astor_memory.server --host 127.0.0.1 --port 7803

# Health
curl http://127.0.0.1:7803/v1/health
# { "status": "ok", "astor_dir": "<dir-name>", "dbs": {"bus":"ok","nest":"ok"}, ... }

# Write a fact (admin)
curl -X POST http://127.0.0.1:7803/v1/write \
     -H 'Content-Type: application/json' \
     -d '{"text":"my favorite color is teal","user":"admin","tier":"private"}'

# Read with recall
curl -X POST http://127.0.0.1:7803/v1/read \
     -H 'Content-Type: application/json' \
     -d '{"query":"favorite color","user":"admin","tier":"private","top_k":5}'
```

Full per-endpoint reference: [docs/api.md](docs/api.md).
Per-store schema and ACL matrix: [docs/architecture.md](docs/architecture.md).

---

## Muse and external agent platform integration

External agent platforms (Muse, custom HTTP clients, Slack adapters,
Discord relay bots) integrate via the **binding API**. The flow is
four steps — all via `POST /v1/binding/*`:

### 1. Register the platform

```bash
curl -X POST http://127.0.0.1:7803/v1/binding/platform \
     -H 'Content-Type: application/json' \
     -d '{
       "platform_id": "muse_main",
       "kind": "muse",
       "endpoint": "https://muse.example.com",
       "auth_token_ref": "env:MUSE_AESK"
     }'
```

The server stores the platform under `platform_id`. The
`auth_token_ref` is a pointer (`env:<NAME>` or `vault:<path>`); the
actual token never lands in the database.

### 2. Register the user that the platform speaks for

```bash
curl -X POST http://127.0.0.1:7803/v1/binding/user \
     -H 'Content-Type: application/json' \
     -d '{
       "user_id": "alice",
       "role": "user",
       "subscription_plan": "free",
       "platform_id": "muse_main"
     }'
```

`role` ∈ {`first_admin`, `admin`, `vip`, `power`, `user`}. The role
determines ACL grant scope: `first_admin` and `admin` see `source`,
`vip` and `power` see `public` plus their own `private`, `user` sees
`public` plus their own `private`.

### 3. Bind a chat session to the user

```bash
curl -X POST http://127.0.0.1:7803/v1/binding/bind \
     -H 'Content-Type: application/json' \
     -d '{
       "platform_id": "muse_main",
       "chat_id": "session-uuid-abc123",
       "user_id": "alice",
       "role_inherit": "user"
     }'
```

Returns the active binding; subsequent calls from the same `chat_id`
resolve to `user_id=alice` automatically. Bindings are looked up by
`POST /v1/binding/lookup` with `{platform_id, chat_id}` — the canonical
"who is this?" resolver at every served level.

### 4. Read / write via the platform-scoped endpoint

```bash
# Write a fact as Alice (resolved from chat_id)
curl -X POST http://127.0.0.1:7803/v1/write \
     -H 'Content-Type: application/json' \
     -d '{
       "platform_id": "muse_main",
       "chat_id": "session-uuid-abc123",
       "text": "alice prefers dark roast coffee",
       "kind": "fact"
     }'

# Read with full recall composition
curl -X POST http://127.0.0.1:7803/v1/read \
     -H 'Content-Type: application/json' \
     -d '{
       "platform_id": "muse_main",
       "chat_id": "session-uuid-abc123",
       "query": "coffee preferences",
       "top_k": 5
     }'
```

The server resolves `platform_id + chat_id` to `user_id` via the
binding lookup, then enforces ACL for that user. Tier routing is
server-side — Muse does not pick tiers; the server maps
`platform_id + user_role` to the appropriate tier combination.

### Skill chaining

External platforms can run multi-skill pipelines via
`POST /v1/skill/chain`:

```bash
curl -X POST http://127.0.0.1:7803/v1/skill/chain \
     -H 'Content-Type: application/json' \
     -d '{
       "platform_id": "muse_main",
       "chat_id": "session-uuid-abc123",
       "skills": ["coref_resolve", "match_experiences", "consult"],
       "context": {"text": "alice said she is moving to Berlin next month"}
     }'
```

Each skill sees the prior skill's output via the threaded `context`
dict. Result includes per-skill timing + any meta-recall lessons that
fired during the chain.

### Audit + admin endpoints for the platform exposing operational hooks

```bash
# List all bindings (admin only)
curl http://127.0.0.1:7803/v1/binding/list

# Per-tier event audit
curl "http://127.0.0.1:7803/v1/audit/health?user=alice"

# PII gate stats (lifetime)
curl http://127.0.0.1:7803/v1/audit/health
```

Full Muse integration recipe: [docs/MUSE_INTEGRATION.md](docs/MUSE_INTEGRATION.md).

---

## Dashboard

The dashboard is served from the same port as the API, at `/dashboard/`
or `/dashboard/` root path. Features include:
  - Health panel — embedding failures, audit warnings, audit total
  - Hero panel — total facts, active facts, last event
  - Per-user breakdown — facts, high-importance, last event, tombstoned
  - Recent capture — chunked pagination, 5 rows per page, switchable
    axes (kind / tier / platform / all-flat)
  - Recent facts — 5 latest
  - Knowledge pages — operator-authored topic sheets
  - Mental models — operator-authored recall surfaces
  - Recall debugger — live /v1/read with tier / user / top_k controls
  - Peer friends panel — list / trust / blacklist / per-peer copy
  - Auto-refresh every 60s, cache TTL 30s, raw JSON endpoint at
    `/v1/dashboard`

The dashboard HTML ships as a generic template in the public repo;
the local instance is the operator's own. Path strings returned by
the API are masked (only the dir basename is exposed in
`/v1/health`; sqlite file paths are not surfaced anywhere in the
public API surface).

---

## Public API surface

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/health` | GET | Server health + bus stats |
| `/v1/identity` | GET | Server peer_id + fingerprint |
| `/v1/dashboard` | GET | Full dashboard payload (JSON) |
| `/v1/health/diagnose` | GET | Detailed health breakdown per user |
| `/v1/write` | POST | Append a fact (with ACL + PII gate) |
| `/v1/read` | POST | Recall with cross-signal composition |
| `/v1/forget` | POST | Tombstone a fact |
| `/v1/consult` | POST | Reactive meta-recall (success / failure / lesson) |
| `/v1/skill` | GET | List registered skills |
| `/v1/skill/<name>` | GET | Skill metadata |
| `/v1/skill/<name>/invoke` | POST | Run one skill |
| `/v1/skill/chain` | POST | Run multiple skills in sequence |
| `/v1/skill/recommend` | POST | Proactive skill + fact recommendations |
| `/v1/binding/platform` | POST | Register external platform |
| `/v1/binding/user` | POST | Register user |
| `/v1/binding/bind` | POST | Bind chat session to user |
| `/v1/binding/lookup` | GET | Resolve chat_id → user_id |
| `/v1/binding/list` | GET | List all bindings (admin) |
| `/v1/peer/list` | GET | List peer-friends (PPS) |
| `/v1/peer/add` | POST | Add peer-friend |
| `/v1/peer/<peer_id>/trust` | POST | Adjust trust score (0–100, default 30) |
| `/v1/peer/<peer_id>/blacklist` | POST | Block a peer |
| `/v1/peer/quarantine/list` | GET | Unknown peers awaiting operator review |
| `/v1/peer/unquarantine` | POST | Approve a quarantined peer |
| `/v1/peer/health` | GET | Peer network health |
| `/v1/episode` | POST | Append raw episode (L0 cone) |
| `/v1/episode/<id>` | GET | Get episode by id |
| `/v1/episode/list` | GET | List recent episodes |
| `/v1/bitemporal/invalidate` | POST | Invalidate a fact with reason |
| `/v1/bitemporal/active` | POST | Mark fact active again |
| `/v1/audit/health` | GET | PII gate + meta-recall counters |
| `/v1/audit/orphans` | GET | List low-signal candidates for cleanup |
| `/v1/backfill_memory_class` | POST | One-shot backfill (admin) |
| `/v1/staleness` | GET | Find references needing refresh |

Full request / response schema: [docs/api.md](docs/api.md).

---

## Peer-to-peer public tier

Peer gossip ships in the box — this is not a roadmap item. A node
announces itself to the network, syncs `public`-tier facts with peers,
and holds unknown peers in quarantine pending operator review.

| Control | How |
|---|---|
| Add a peer | `POST /v1/peer/add` |
| Open the public tier to signed queries | `POST /v1/peer/open-search` |
| Who is open | `GET /v1/peer/open-search` |

Peer search needs a relationship on both sides. Open search is the
other mode: publish your `public` tier to any peer whose signature
verifies, without adding anyone. Off by default.

```bash
# open to every signature-verified peer
curl -X POST localhost:7803/v1/peer/open-search \
     -H 'Content-Type: application/json' \
     -d '{"allow": true, "peer_id": "*"}'

# or invite exactly one node
curl -X POST localhost:7803/v1/peer/open-search \
     -H 'Content-Type: application/json' \
     -d '{"allow": true, "peer_id": "astor:<32-hex>"}'
```

What it deliberately is not: `public` tier only, read-only, rate
limited per caller, and it creates no friendships. `source` and
`private` are unreachable — the public store is the only file opened,
so the boundary is structural rather than a filter.
| List peers | `GET /v1/peer/list` |
| Tune trust | `POST /v1/peer/<peer_id>/trust` (0–100, default 60) |
| Block a peer | `POST /v1/peer/<peer_id>/blacklist` |
| Review quarantine | `GET /v1/peer/quarantine/list` → `POST /v1/peer/unquarantine` |
| Adopt a pending peer | `POST /v1/peer/adopt` |
| Watch the network | Dashboard → peer panel, or `GET /v1/peer/health` |
| Rate limits | `GET /v1/peer/rate-limit` |

The hard boundary: only `public` gossips. `private` and `source` never
leave the node, regardless of trust level. Incoming peer traffic is
rate-limited per peer, and peers you have not approved sit in
quarantine instead of being able to write.

**Try it in 60 seconds:** start two nodes locally, add each as the
other's peer, `POST /v1/write` a fact with `"tier": "public"` on node A,
then `/v1/read` it from node B.

---

## The shared tier keeps the method and loses the machine

A `public`-tier fact is readable by every peer in the federation. That
is the point of the tier: one node works out how to fetch a page, and
the next one inherits the method instead of spending the day again.

It is also the reason the tier needs a boundary. A method is worth
sharing; the working directory, the user profile, the host name and the
LAN address of the machine that learned it are not. A peer reading the
public tier should learn the method, not the map of the host that found
it.

So a public write is scrubbed on the way in. The rules are structural,
not a list of one operator's paths:

```
C:\Users\someone\AppData   ->  <home>\AppData
D:\work\project\file.py     ->  <root>\project\file.py
/home/someone/x               ->  <home>/x
\\HOST\share                ->  \\<host>\share
<private-ip>                  ->  <private-host>
丙子/丙申/壬辰/乙巳          ->  <birth-pillars>
birthday 1996-08-23          ->  <birth-date>
```

A path names its owner, so the name goes too — the names are learned
from the text before the paths are rewritten, because after that there
is nothing left to learn them from.

The scrub is in `promote_candidate`, the one funnel every write path
goes through: the HTTP API, `am write`, the extractor, the learner.
The `private` and `source` tiers are untouched — a local path is
useful there, and neither tier leaves the machine.

| Command | What it does |
|---|---|
| `am scrub-public` | Rewrite the facts that are already in the public tier (dry run; `--apply` writes) |
| `am mine-public` | Move what the private tier learned into the public tier (dry run; `--apply` writes) |

Mining keeps the facts that are methods rather than history — a lesson,
a success pattern, a failure pattern, a method — and drops the ones
that are about a person: first person, emotion, geography, anything the
visibility classifier reads as private. What survives is scrubbed,
distilled and tagged `distilled_by=am.mine-public`, so the origin of a
shared fact stays visible.

`scripts/mine_public_weekly.py` runs the mining once a week. It is a
script and not a prompt on purpose: the mining is deterministic, and a
weekly job that wakes a model to run a query costs money and can still
be wrong.

---

## Why peer-to-peer, not a central memory service

1. **No central server to trust — or pay.** Shared agent knowledge
   shouldn't live on someone else's SaaS. Gossip between self-hosted
   nodes keeps the commons owned by its participants.
2. **Privacy by architecture, not by policy.** Sharing is an explicit
   publish to `public`, per peer, per topic — never an all-or-nothing
   upload of your memory.
3. **The network is the moat.** Every new node makes every other node's
   public recall richer. A centralized memory product can't offer that
   without becoming the custodian of everyone's data.

Within one node the operational surface stays small on purpose: one
server process, one `ASTOR_DIR`, one backup. Running a fleet of nodes is
opt-in, and each node is independently complete.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).

## Maintainer

Astor-Memory Maintainers — see [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md).

## Installation troubleshooting

Windows installs can fail in a way that produces no error message at all —
onnxruntime needs the Visual C++ 2015-2022 redistributable (x64), and without
it the import is a native fault rather than an exception, so the process dies
before Python can report anything.

`am preflight` diagnoses this in one second and `am preflight --fix` repairs it.
See **[INSTALL.md](INSTALL.md)** for the full guide, including the degraded
mode astor falls back to when the runtime cannot be updated.
