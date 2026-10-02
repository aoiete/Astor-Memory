# Astor-Memory

> **Self-hosted memory system for AI agents.** Three stores, three tiers, federated public tier, zero vendor lock-in.

> **中文文档:** [README.zh-CN.md](README.zh-CN.md) · **Dashboard:** [docs/dashboard.md](docs/dashboard.md) · **Architecture:** [docs/architecture.md](docs/architecture.md) · **API:** [docs/api.md](docs/api.md)

---

## What this is

A single-server SQLite-backed memory layer for a small group of trusted
people (your family, your friends, your co-admins) who share one bot.
Each user gets a private tier they can read and write; the operator
maintains a `source` tier of operator-only patterns; everyone shares a
`public` tier of cross-user knowledge.

The same server also acts as the spoke endpoint for external agent
platforms (Muse, custom HTTP clients, Slack adapters) that need a
persistent memory backend with ACL-enforced user isolation and
per-call audit logging.

```
+---------------------------------------+
|  One astor-memory server (port 7803)   |
|                                        |
|   first_admin    first_admin tier      |
|   mom            private_mom tier      |
|   friend_a       private_friend_a      |
|   friend_b       private_friend_b      |
|   cousin            private cousin       |
|                                        |
|   shared memory = public + source      |
|   per-user memory = private_<user_id>  |
+---------------------------------------+
```

Each user has their own SQLite database, their own ACL grants, and their
own bot binding. The admin sees `source` (operator-only patterns) plus
any `private` tier a user has explicitly granted.

This is not a multi-tenant SaaS. It is a single-server memory for a
small group of people who trust each other enough to share the bot.
Privacy is enforced by ACL at the matrix level
(see [ACL hardening](docs/acl-v1.2-hardening.md)), not by trusting
each user to behave.

---

## Why we built this

Modern AI agents need memory. Existing solutions force you to choose
between capability and autonomy:

| Option | You get | You lose |
|---|---|---|
| **Pure RAG** (vector store) | Simple retrieval | No event log, no fact extraction, no user isolation |
| **Letta** (Memory Blocks) | Read-only protection + archival | Heavy runtime, strict architecture |
| **mem0** (4-tier ACL) | Multi-tenant + scope labels | Tight cloud coupling, async by default |
| **Astor-Memory** | Three SQLite stores + three tiers + ACL + event log + audit + PII defense + decay + dashboard + REST + open data on disk | Single-server scope (not horizontally sharded) |

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

Single-binary install via `pip install astor-memory` after which
`astor-server` becomes available as a console script. Optional
`[muse]` extra installs the Muse adapter package.

---

## Quick start

```bash
# Install
pip install astor-memory
# or, with the Muse adapter
pip install astor-memory[muse]

# Initialize a fresh runtime
export ASTOR_DIR=/var/lib/astor
mkdir -p "$ASTOR_DIR"
python -m astor_memory.cli.main init

# Add an admin user
python -m astor_memory.cli.main user add admin --role first_admin

# Start the server
astor-server --host 127.0.0.1 --port 7803

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

Full Muse integration recipe: [docs/integration-muse.md](docs/integration-muse.md).

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
| `/v1/peer/trust` | POST | Adjust trust score |
| `/v1/peer/blacklist` | POST | Block a peer |
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

## Peer-to-peer public tier federation (Phase 5)

Multiple astor instances owned by trusted peers will sync their
`public` tier directly — no central server, no remote-direct RPC.
Per-peer trust (0-100) + per-topic weights let curating admins decide
what to share with whom. `private` and `source` tiers never cross
the boundary.

This ships as `/v1/peer/*` REST endpoints plus an `am peer` CLI on
top of the existing `peer_relationships.py` schema. Phase 5 lands
when the operator-facing pilot reaches convergence.

---

## Why a single server, not federated from day one

Three reasons:

1. **Operational simplicity.** One `astor-server` process, one
   `ASTOR_DIR` directory, one backup cron. Multi-server sharding
   triples the operational surface area and adds eventual-consistency
   bug classes that show up only in production.
2. **ACL is the trust boundary.** The same ACL matrix that enforces
   per-user privacy works in a multi-server deployment — federation
   only requires per-server trust, not a rewrite of access control.
3. **Per-peer sharing is enough for the actual deployment shape.** A
   family / friends / co-admin group rarely needs cross-organization
   sharing. When they do, the per-peer trust + per-topic weight
   mechanism in `peer_relationships.py` is the minimal extension that
   covers the use case without re-architecting.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).

## Maintainer

Astor-Memory Maintainers — see [AUTHORS.md](AUTHORS.md).