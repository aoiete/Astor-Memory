# astor-memory — Design Philosophy

## The principle

> **Agent can change. Model can change. But accumulated context must not.**

This principle, articulated by the Memmy project in Sept 2026, is the
**core design invariant** for astor-memory. Every API design decision,
schema field, and migration path is evaluated against this question:

> "If a user switches to a different agent tomorrow, will their context
> still be there?"

If the answer is no, the design is wrong.

## What we explicitly optimize for

### 1. **Format-agnostic**

Any HTTP client can talk to astor-memory. We expose plain JSON over REST
— not a custom protocol, not a binary format, not hermes-specific IPC.

- Python SDK: `astor_memory.client.AstorClient`
- curl: `curl -X POST http://...:7803/v1/read`
- JavaScript: standard `fetch()`
- Rust, Go, Java: implement against the OpenAPI spec (TODO)

### 2. **Backward compatible**

When we add new fields to the fact schema, we add them as **optional**.
Old facts still load. Old clients still work.

When we deprecate endpoints, we keep them for 2 minor versions with a
warning header.

### 3. **Local-first**

The service runs on `127.0.0.1:7803` by default. No telemetry, no cloud,
no authentication against external services. **Your memories are on
your machine, in SQLite, where you can read them with `sqlite3`**.

### 4. **Self-audit**

The service audits itself daily:
- `astor_grounding_audit.py` — finds ungrounded facts
- `astor_coverage_monitor.py` — tracks coverage trends
- `astor_daily_sweep.py` — consolidates duplicates

Audits run via cron, results land in `logs/audit/`. The agent that
wrote a fact is the same agent that reviews whether the fact is
grounded. This is the accountability loop.

### 5. **Tier isolation**

Three tiers enforce access boundaries:

| Tier | Read | Write | Use case |
|---|---|---|---|
| `public` | anyone | anyone | general knowledge |
| `source` | agent | agent | architecture notes |
| `private` | owner + grants | owner | personal context |

You cannot write to someone else's `private` tier. You cannot read it
without an explicit grant. This is enforced at the ACL layer.

## What we explicitly don't optimize for

### Not a chatbot

astor-memory is **not** an AI agent. It doesn't chat with users. It
doesn't decide anything. It's a memory service — a database with smart
recall. Any agent (or non-agent script) can use it.

If you want a chatbot that uses astor-memory, build one on top.

### Not a vector store

Yes, we use embeddings for recall. Yes, we have a vector index. But
the **canonical representation of a fact is its row in
`memory_canonical`** — not a vector. Vectors are an index, not the data.

This matters because:
- You can `sqlite3 astor_bus.db` and read everything
- You can `grep` for keywords
- You can write SQL queries for audit

A pure vector store loses this.

### Not a knowledge graph

We have lineage (`parent_fact_ids`) and provenance tracking. But the
**primary structure is flat facts**, not a graph. We don't force
entities into a fixed schema; we let facts emerge from user input.

If you need a knowledge graph, build one on top of astor-memory using
`/v1/fact/<id>/lineage` and `/v1/fact/<id>/provenance`.

## The WPS/Office analogy

In the early 2000s, Microsoft Office and WPS Office fought over
document format dominance. The winner wasn't the one with the best
features — it was the one that could read the other's files.

When you could open a `.doc` in WPS, switching costs dropped. Users
chose based on features, not on lock-in.

**The same applies to AI agents.** If agent A can read agent B's
memory, switching costs drop. Users choose based on capability, not on
lock-in.

astor-memory's job is to be **the format layer** — a shared file
format for memories that any agent can read and write.

## Open-source commitments

- **MIT license** — use it anywhere, modify it, ship it
- **Public GitHub** — issues, PRs, releases all visible
- **Versioned releases** — every commit tagged with `vX.Y.Z`
- **Migration scripts** — when schema changes, migration is automated
- **Tests in CI** — `pytest` runs on every PR; `eval/recall.json` is
  the regression suite

## What this means for users

You can:
1. Run astor-memory locally today
2. Switch agents tomorrow without losing context
3. Switch models without rewriting memory
4. Switch computers by copying the SQLite file
5. Switch hosting by exporting facts to JSON

You **cannot**:
1. Get locked into a proprietary format
2. Lose context by upgrading
3. Have your memory sold or leaked

## See also

- `docs/integration.md` — how to integrate with your agent
- `README.md` — quickstart and architecture overview
- `docs/releases/` — version-by-version change log
