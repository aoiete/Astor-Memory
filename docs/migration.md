# Migration Guide

> Move into Astor-Memory from any of the common AI memory stacks.

This guide covers five source-system families people ask about most
often: **mem0**, **memu.ai SDK**, **Letta / Zep / MemGPT**, **ChromaDB /
Pinecone / Weaviate**, and **plain file/JSON archives**. Plan for
~2 hours of focused work and a 1–2 week parallel-run window before
cutover.

---

## Table of contents

1. [Why migrate](#why-migrate)
2. [Source-system compatibility matrix](#source-system-compatibility-matrix)
3. [Common 5-step migration](#common-5-step-migration)
4. [From mem0](#from-mem0)
5. [From memu.ai SDK](#from-memu-ai-sdk)
6. [From Letta / Zep / MemGPT](#from-letta--zep--memgpt)
7. [From ChromaDB / Pinecone / Weaviate](#from-chromadb--pinecone--weaviate)
8. [From plain file or JSON archives](#from-plain-file-or-json-archives)
9. [Side-by-side API reference](#side-by-side-api-reference)
10. [Rollback procedure](#rollback-procedure)
11. [Common pitfalls](#common-pitfalls)

---

## Why migrate

Astor-Memory is built for one specific deployment shape:

> **One bot server, many isolated users.** Mom's birthday notes never
> leak to the friend group, and the poker brief you wrote for a buddy
> does not contaminate your cousin's career advice.

If you are evaluating whether to migrate, the question to ask is:

| If your current stack is... | Migration value | Why |
|---|---|---|
| mem0 | **High** | Same per-user isolation goal, but multi-tenant SaaS only; Astor gives you a self-owned DB per user on a single server |
| memu.ai SDK | **None — do not migrate data** | memu.ai is shutting down; you can wrap astor under the same call sites in one PR |
| Letta / Zep / MemGPT | **High** | These give agents long-term memory, but every user shares a single DB; Astor adds per-user ACL by design |
| ChromaDB / Pinecone / Weaviate | **Medium** | You have a vector store, not an agent memory system; Astor adds facts/scenarios/profile + ACL on top of the same vectors |
| Plain files / JSON | **Low but useful** | You get version tracking, dedup, decay, scenario clustering — all the things plain JSON lacks |

If you are not sure which bucket you fit, see
[`docs/architecture.md`](./architecture.md) for what Astor-Memory actually
is.

---

## Source-system compatibility matrix

Quick check of what each source system maps to in Astor-Memory:

| Source system | Where data lives | Astor equivalent | One-shot CLI? |
|---|---|---|---|
| mem0 | `mem0/vectors/` + per-user JSON | `private_<user>.db` (bus + nest) | `am migrate from-mem0` (v1.3+) |
| memu.ai SDK | cloud (no local file) | wrap with `astor_write`/`astor_read` in code | No CLI needed |
| Letta | PostgreSQL or SQLite | `private_<user>.db` (bus + forge + nest) | `am migrate from-letta` (v1.4+) |
| Zep | Zep cloud or local Docker | `private_<user>.db` | `am migrate from-zep` (v1.4+) |
| MemGPT | SQLite | `private_<user>.db` | `am migrate from-memgpt` (v1.4+) |
| ChromaDB | local directory or server | `private_<user>.db` (nest only — you keep your vector DB) | `am migrate from-chroma` (v1.3+) |
| Pinecone | cloud index | `private_<user>.db` (bus + forge; nest points at Pinecone) | manual |
| Weaviate | local server | same as Pinecone | manual |
| Plain JSON / YAML / Markdown | local directory | `private_<user>.db` (bus) | `am migrate from-files` (v1.3+) |

CLI flags shown are planned; if a CLI does not exist yet for your
source system, use the per-system walkthrough below.

---

## Common 5-step migration

Most migrations, regardless of source system, follow this skeleton:

### Step 1 — Install Astor-Memory alongside your current stack

```bash
pip install astor-memory
```

Verify install:

```bash
am --version
am doctor
# → bus: NOT INITIALIZED (expected; not yet running)
# → forge: NOT INITIALIZED
# → nest: NOT INITIALIZED
```

Your existing stack is untouched. Both can run side-by-side.

### Step 2 — Initialize Astor-Memory in parallel mode

```bash
am init --parallel --port=7804
```

The `--parallel` flag tells Astor-Memory to use ports 7804–7806 so it
does not collide with anything you have on 7801–7803.

You now have:

```
7801–7803   your existing stack (mem0 / Letta / Chroma / etc.)
7804        astor_forge
7805        astor_nest
7806        astor_bus
```

### Step 3 — Migrate data (one-time)

Run the per-system migration (see the dedicated section below for
your source). Always `--dry-run` first.

```bash
# example shape
am migrate from-<source-system> --source=<old-path> --dry-run
am migrate from-<source-system> --source=<old-path>
```

After migration:

```
~/.astor/
├── astor_bus.db      # facts + events + audit
├── astor_nest.db     # vector embeddings + lex index
└── astor_forge.db    # extracted structured facts
```

If your source system already kept per-user separation (Letta,
mem0 with their `user_id` field), Astor preserves the user boundary.
If your source system had a single shared store (ChromaDB,
plain files), Astor infers per-user separation from a `user_id`
metadata field if present, otherwise falls back to one admin-private
DB until you tag your data.

### Step 4 — Cut over (1–2 weeks later)

```bash
# Stop legacy services
<your stack stop command>

# Switch Astor-Memory to canonical ports
am config bus.port=7803
am config forge.port=7801
am config nest.port=7802

# Restart Astor-Memory on canonical ports
am serve --detach
```

Verify health:

```bash
am doctor
# → bus: OK (N events migrated)
# → forge: OK (provider=openai, latency=320ms)
# → nest: OK (N docs indexed, X KB vector cache)
```

Run your existing test suite:

```bash
pytest tests/
```

If any tests fail, see [Rollback procedure](#rollback-procedure).

### Step 5 — Archive the old stack (do not delete)

```bash
mv <legacy-dir> <legacy-dir>-archived-$(date +%Y-%m-%d)
```

Astor-Memory ships with the migration tool but does NOT auto-delete
legacy data. The decision is yours.

---

## From mem0

[mem0](https://mem0.ai) is a multi-tenant SaaS for AI agent memory.
Per-user isolation is enforced at the API layer; data lives in
mem0's cloud. Migration to Astor-Memory is **high value** because you
get a self-owned per-user DB on a single server, no SaaS dependency.

### What maps

| mem0 concept | Astor equivalent |
|---|---|
| `Memory.add(messages, user_id="alice")` | `write(text, user_id="alice")` |
| `Memory.search(query, user_id="alice")` | `read(query, user_id="alice")` |
| `user_id` field | `user_id` field (same) |
| `agent_id` field | `metadata["agent_id"]` |
| `run_id` field | `metadata["run_id"]` |
| `created_at` timestamp | `created_at` (same) |

### Migration script

```python
# scripts/migrate_from_mem0.py
from mem0 import MemoryClient
from astor_memory import write, init

init(actor="first_admin", role="first_admin")

client = MemoryClient(api_key="<your-mem0-key>")

for user_id in client.list_users():
    memories = client.get_all(user_id=user_id, limit=10000)
    for mem in memories:
        write(
            mem["memory"],
            user_id=user_id,
            scope="long_term",
            metadata={
                "agent_id": mem.get("agent_id"),
                "run_id": mem.get("run_id"),
                "migrated_from": "mem0",
                "mem0_id": mem["id"],
            },
        )
    print(f"{user_id}: {len(memories)} memories migrated")
```

> Pricing: mem0 charges per-write; pulling all memories is one read
> per user, so this is cheap regardless of memory count.

---

## From memu.ai SDK

[memu.ai](https://memu.ai) is **discontinued**. There is no production
data to migrate.

If you have call sites that imported `memu` directly:

```python
# Before
from memu import MemoryClient
client = MemoryClient(api_key="...")
client.add("user prefers concise replies", user_id="alice")
hits = client.search("user preferences", user_id="alice")
```

Replace with:

```python
# After
from astor_memory import write, read
write("user prefers concise replies", user_id="alice")
hits = read("user preferences", user_id="alice")
```

That is the entire migration. There is no data path because there is
no data — the SDK never persisted anything you can recover.

---

## From Letta / Zep / MemGPT

These three are the closest competitors to Astor-Memory: they all
provide **agent-managed long-term memory** with blocks, archival
memory, and recall. The main difference is they all share one DB
across users; Astor splits per-user by default.

### Letta → Astor

Letta stores agents and their blocks in PostgreSQL or SQLite. Each
agent has its own block set.

```python
# scripts/migrate_from_letta.py
import sqlite3  # or psycopg2 for Postgres
from astor_memory import write, init

init(actor="first_admin", role="first_admin")

conn = sqlite3.connect("<your-letta-db>.sqlite")
agents = conn.execute("SELECT id, user_id, name FROM agents").fetchall()

for agent_id, user_id, name in agents:
    blocks = conn.execute(
        "SELECT label, value, created_at FROM blocks "
        "WHERE agent_id = ?", (agent_id,)
    ).fetchall()
    for label, value, created_at in blocks:
        write(
            value,
            user_id=user_id,
            scope="long_term",
            metadata={
                "agent_id": agent_id,
                "block_label": label,
                "created_at": created_at,
                "migrated_from": "letta",
            },
        )
    print(f"agent={name} user={user_id}: {len(blocks)} blocks migrated")
```

Letta's `user_id` field, if set, becomes Astor's `user_id`. If your
Letta deployment shared one user across all agents, treat the whole
import as belonging to `first_admin` and re-tag later.

### Zep → Astor

Zep stores sessions and messages per `user_id` in their Docker
container.

```bash
docker exec <zep-container> sqlite3 /data/zep.db \
  ".dump sessions" > zep_sessions.sql
```

Then walk the SQL dump and emit `astor_write` calls — pattern is
identical to the Letta script.

### MemGPT → Astor

MemGPT persists everything in a single SQLite file. Migration is
a direct table walk: read `messages`, read `archival_memory`, emit
`astor_write` per row with `user_id` extracted from your MemGPT
agent config.

---

## From ChromaDB / Pinecone / Weaviate

These are **vector databases**, not agent memory systems. You have
embeddings; you do not have facts, scenarios, profiles, decay, or
ACL. Migration brings those.

### ChromaDB

ChromaDB persists to a local directory. Each collection has its own
SQLite file.

```python
# scripts/migrate_from_chroma.py
import chromadb
from astor_memory import write, init

init(actor="first_admin", role="first_admin")

client = chromadb.PersistentClient(path="<chroma-dir>")
for collection in client.list_collections():
    coll = client.get_collection(collection.name)
    results = coll.get(include=["documents", "metadatas", "embeddings"])

    for doc, metadata, embedding in zip(
        results["documents"], results["metadatas"], results["embeddings"]
    ):
        user_id = metadata.get("user_id") or "first_admin"
        write(
            doc,
            user_id=user_id,
            scope="long_term",
            metadata={
                "chroma_collection": collection.name,
                "chroma_id": metadata.get("id"),
                **metadata,
            },
            embedding=embedding,  # pass through if model matches
        )
```

> **Important**: Astor-Memory embeds using a specific model (default
> `BAAI/bge-base-en-v1.5`). If your Chroma collection used a different
> model, omit the `embedding` arg and let Astor re-embed on first recall.

### Pinecone / Weaviate

These are cloud / network vector stores. Same pattern: walk the
index, emit `astor_write` per vector with metadata, optionally
re-embed if model differs.

Astor can also **delegate nest (vector storage) to Pinecone or
Weaviate** via `am config nest.backend=pinecone`. In that mode, the
bus + forge still live in SQLite, but embeddings live in your
existing Pinecone index. Useful when you already pay for Pinecone
and want to keep the vectors in place.

---

## From plain file or JSON archives

You have a folder of `.json`, `.yaml`, or `.md` files. There is no
structure, no dedup, no decay. Astor will give you all of those.

### Markdown / Obsidian vault

```bash
am migrate from-files \
  --source ~/notes/alice.md \
  --user-id alice \
  --format markdown
```

Astor splits on `## ` (level-2 heading), treats each section as a
fact, and preserves frontmatter as metadata.

### JSON Lines / array

```bash
am migrate from-files \
  --source ~/notes/alice.jsonl \
  --user-id alice \
  --format jsonl
```

Each line is a fact. If the JSON has `user_id` / `created_at`
fields, Astor uses them; otherwise it tags with the `--user-id`
you provided and the migration timestamp.

### YAML frontmatter (Obsidian-style)

Same as Markdown, but frontmatter fields become metadata directly
rather than text.

---

## Side-by-side API reference

### Write

| Source | New (astor-memory) |
|---|---|
| `mem0.add(text, user_id="alice")` | `write(text, user_id="alice")` |
| `letta.agent.block_add(value)` | `write(value, user_id=<agent.user_id>)` |
| `chroma.add(documents=[text])` | `write(text)` (chroma has no user concept) |
| `json.dump({...})` to file | `write(text, metadata={...})` |

### Read

| Source | New (astor-memory) |
|---|---|
| `mem0.search(query, user_id="alice")` | `read(query, user_id="alice")` |
| `letta.agent.block_list()` | `read(query, user_id=<agent.user_id>)` |
| `chroma.query(query_texts=[q])` | `read(q)` (chroma returns vectors; astor returns facts with refs) |
| `json.load(open(file))` | `read(query, user_id=<from-filename-or-arg>)` |

### Health check

| Source | New (astor-memory) |
|---|---|
| `mem0.health()` (SaaS ping) | `am doctor` |
| `letta server status` | `am doctor` |
| `chroma.heartbeat()` | `am doctor` (covers bus + forge + nest) |

---

## Rollback procedure

If something goes wrong after cutover, rollback is a 3-step process:

### 1. Stop Astor-Memory

```bash
am serve --stop
```

Or if running in systemd:

```bash
systemctl stop astor-memory.service
```

### 2. Restart your previous stack

```bash
# mem0 / Letta / Zep: their normal start command
# ChromaDB: docker start <container> or chroma run --path <dir>
# Plain files: nothing to restart; data is still in the folder
```

### 3. Verify legacy is operational

```bash
curl localhost:7801/health
# → {"status": "ok", ...}
```

Your application continues to work because your old call sites were
never modified during parallel-run.

### Data rollback

Astor-Memory data is in `~/.astor/`. Source data is in whatever path
your previous stack used (untouched during parallel-run). To roll
back **data** (not just services):

```bash
# Re-export from astor back to source format
am migrate to-<source-system> --source ~/.astor/astor.db --target <old-path>
# (this CLI is planned; for now, you can re-run your migration in reverse manually)
```

---

## Common pitfalls

### Pitfall 1: Skipping the dry-run

Always `--dry-run` first. Each source system has subtle schema
differences (mem0's `agent_id` field, Chroma's embedding model,
Letta's `block_label` taxonomy) that you want to verify before
writing.

**Fix**: Read the dry-run report. Check user counts, fact counts,
embedding-model compatibility.

### Pitfall 2: Mixing per-user data into admin-private

If your source system had a single shared DB (plain files, single
Chroma collection), the migration defaults `user_id` to
`first_admin`. If you have multiple users in the same DB, tag them
manually before running.

**Fix**: Add `--user-id-field` to point at the right metadata
field, or pre-process your data with `user_id` injection.

### Pitfall 3: Embedding model mismatch

Astor-Memory embeds with `BAAI/bge-base-en-v1.5` (768 dim) by
default. If your source used a different model (OpenAI `text-embedding-3-small`,
`all-MiniLM-L6-v2`, etc.), the embedding pass-through is unsafe —
similarity scores will be wrong.

**Fix**: Either (a) re-embed by omitting the `embedding` arg, or
(b) configure Astor to use the same embedding model
(`am config forge.embedding_model=<your-model>`).

### Pitfall 4: Cutting over too fast

Do not cut over after 1 day of parallel-run. Plan for 1–2 weeks
minimum. Watch for:

- Latency differences (Astor-Memory should be 10–100× faster
  in-process; slower if REST-only)
- Citation format changes (any code that string-matches old format
  breaks)
- Cron jobs that depend on specific ports

**Fix**: Use the 1–2 week parallel-run to observe and adjust.

### Pitfall 5: Forgetting that you had multiple users

If your source had `user_id` and you did not realize, your
migration will silently import everyone as `first_admin`. Then
`read(query)` returns their data to the admin.

**Fix**: Always print a per-user fact count in the dry-run before
running for real. If counts are off, fix the `--user-id-field` and
re-run.

### Pitfall 6: Importing bot-bound data as facts

If your source system stored bot-binding tables, audit logs, or
runtime state alongside facts, those tables do not belong in
Astor-Memory. They are infrastructure, not memory.

**Fix**: Skip non-fact tables in your migration. Filter at the SQL
level: only emit `astor_write` for rows where the table is fact-like
(`memories`, `blocks`, `archival_memory`, `messages`), not
infra-like (`audit_log`, `bot_binding`, `sessions`).

---

## Next

- [`docs/agent-adapters.md`](./agent-adapters.md) — MCP / LangChain /
  REST / Python integration
- [`docs/faq.md`](./faq.md) — frequently asked questions
- [`docs/troubleshooting.md`](./troubleshooting.md) — common errors
  and fixes
- [`docs/contributing.md`](./contributing.md) — for contributors