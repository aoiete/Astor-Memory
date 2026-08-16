# REST API reference

> **Status:** v1.1.1 (P0 ACL fix + SQLite thread safety).
> 18 endpoints total — 11 v1.1.0 base + 7 opt3-6 (merge dedup v2, provenance, versioning, restore).

Base URL: `http://127.0.0.1:7803` (default). The CLI subcommand
`am serve` and the legacy `restart.py` daemon both bind to this port.

For all POST endpoints, send `Content-Type: application/json`.

---

## Table of contents

1. [ACL: actor / role resolution](#acl-actor--role-resolution)
2. [Core write / read](#core-write--read)
   - [`POST /v1/write`](#post-v1write)
   - [`POST /v1/read`](#post-v1read)
   - [`POST /v1/forget`](#post-v1forget) — **opt3-6 (forget + dry-run + audit snapshot)**
   - [`POST /v1/read/multi`](#post-v1readmulti) — **opt3-6 (cross-tier parallel recall)**
3. [Lifecycle: dedup + merge](#lifecycle-dedup--merge)
   - [`POST /v1/merge/find`](#post-v1mergefind) — **opt3-6 (cosine + LLM judge)**
   - [`POST /v1/merge/apply`](#post-v1mergeapply) — **opt3-6 (apply reviewed merges)**
4. [Provenance graph (fact lineage)](#provenance-graph-fact-lineage)
   - [`GET /v1/fact/<id>/provenance`](#get-v1factidprovenance) — **opt3-6**
   - [`GET /v1/fact/<id>/lineage`](#get-v1factidlineage) — **opt3-6**
   - [`GET /v1/fact/<id>/graph.dot`](#get-v1factidgraphdot) — **opt3-6 (graphviz)**
   - [`POST /v1/fact/<id>/provenance`](#post-v1factidprovenance) — **opt3-6 (record parents)**
5. [Versioning + restore](#versioning--restore)
   - [`GET /v1/fact/<id>/versions`](#get-v1factidversions) — **opt3-6**
   - [`POST /v1/fact/<id>/restore`](#post-v1factidrestore) — **opt3-6**
   - [`GET /v1/snapshot/stats`](#get-v1snapshotstats) — **opt3-6 (daily snapshot stats)**
6. [Stats + health](#stats--health)
   - [`GET /v1/health`](#get-v1health)
   - [`GET /v1/viewer/stats`](#get-v1viewerstats) — v1.1.0 (MemoraX Viewer)
   - [`GET /v1/lex/stats`](#get-v1lexstats)
7. [Admin + installer](#admin--installer)
   - [`POST /v1/reload`](#post-v1reload) — first_admin only
   - [`POST /v1/install`](#post-v1install)

---

## ACL: actor / role resolution

Every POST endpoint routes through `before_request`, which resolves the
caller's `(actor, role)` from `bot-binding.db user_meta.role`. See
[`architecture.md`](./architecture.md) § "ACL enforcement flow" for the
full pipeline. The relevant body fields per request:

| Field | Required | Notes |
|---|---|---|
| `user` | always | The caller. Resolves to actor+role via `user_meta.role`. `admin` alias → `first_admin`. Unknown users → fail-closed as `first_admin` (with audit row). |
| `tier` | optional | `public` (default), `source`, `private`, `repo`. |
| `user_id` | when cross-user | For `tier=private`: filters which user's DB. Defaults to `user`. Cross-user reads require admin role; user role gets `403 cross_user_forbidden`. |
| `repo_id` | when `tier=repo` | Hash of the git remote URL. See CHANGELOG v1.1.0. |

**Error responses for ACL failures**:

| Status | `error` | Meaning |
|---|---|---|
| 403 | `permission_denied` | `astor_check_*` from downstream code raised (e.g. user_a → source) |
| 403 | `cross_user_forbidden` | Cross-user access attempted by a `user` role |
| 403 | `acl_init_failed` | `astor_init_acl` itself failed (bad tier / missing user_id for private) |

---

## Core write / read

### `POST /v1/write`

Write a fact through `bus` (canonical store) + `forge` (extraction) +
`nest` (embedding). The `mode` parameter controls extraction cost:

- `auto` (default): regex for short text (<200 chars), raw `none` for long
  bulk paste — zero LLM cost on average
- `regex`: pure regex, zero LLM
- `none`: skip extraction entirely, write raw text
- `llm`: opt-in to LLM extraction (cost = ~$0.001/call)

**Body**

| Field | Required | Default | Notes |
|---|---|---|---|
| `text` | yes | — | The fact to remember |
| `user` | no | `admin` | Caller id (resolves role from user_meta) |
| `mode` | no | `auto` | Extraction mode (see above) |
| `tier` | no | `public` | `public`/`source`/`private`/`repo` |
| `scope` | no | `long_term` | `long_term`/`short_term`/`profile`. `profile` auto-routes to `private`. |
| `user_id` | when private | `user` | Target user (cross-user requires admin) |
| `repo_id` | when `tier=repo` | — | Per-repo isolation (MemoraX pattern) |
| `mirror_to_source` | no | `false` | Admin-only. If `tier=public` + `true`, also writes to `source` tier |

**Response 200**

```json
{
  "fact_ids": [123],
  "count": 1,
  "event_id": 4567,
  "scope": "long_term",
  "tier": "public",
  "mirrored": []
}
```

**Errors**: `400 text required`, `400 invalid scope`, `400 tier=repo requires repo_id`,
`403 permission_denied` (user_a → source), `403 acl_init_failed`.

---

### `POST /v1/read`

Recall similar facts via hybrid vector + BM25 search.

**Body**

| Field | Required | Default | Notes |
|---|---|---|---|
| `query` | yes | — | Free-form query string |
| `top_k` | no | 5 | Number of results |
| `tier` | no | `public` | Recall target tier |
| `user_id` | when private | `user` | Filter to one user's private DB |
| `hybrid` | no | `true` | Use both vector (60%) + BM25 (40%); set `false` for pure vector |
| `bm25_weight` | no | 0.4 | Override hybrid weight |
| `vec_weight` | no | 0.6 | Override hybrid weight |

**Response 200**

```json
{
  "count": 3,
  "results": [
    {
      "fact_id": 359,
      "content": "...",
      "kind": "system_event",
      "namespace": "/tenant/admin/actor/discord:.../project/...",
      "user_id": "admin",
      "similarity": 0.85,
      "score_kind": "hybrid",
      "confidence": 0.9,
      "importance": 0.85,
      "tags": null
    }
  ]
}
```

---

### `POST /v1/forget`  *(opt3-6)*

Forget a fact by ID or by content match. Always writes a `forget` row
to `bus.audit_log` with the full `old_state` snapshot (for opt6
versioning / undo). Supports **dry-run** to preview what would happen.

**Body — one of two modes**

```jsonc
// Mode 1: explicit fact_id
{
  "user": "admin",
  "tier": "private",
  "fact_id": 123,
  "tombstone_only": true,   // false = hard-delete (also wipes embeddings)
  "dry_run": false           // true = no mutation, just preview
}

// Mode 2: content match (BM25 best-hit)
{
  "user": "admin",
  "tier": "private",
  "user_id": "alice",
  "query": "user mentioned she prefers tea",
  "forget_threshold": 5.0,   // BM25 min score to act on
  "tombstone_only": false,
  "dry_run": false
}
```

**Strategy**

1. If `fact_id` given: look up fact, tombstone via `bus`, remove from
   `lex` (soft or hard depending on `tombstone_only`).
2. If `query` given: BM25 search in `(tier, user_id)`; pick top-1; if
   score ≥ `forget_threshold` (default 5.0), forget it. Otherwise
   return empty hit + candidate list.
3. Always log to `bus.audit_log` with `old_state` JSON snapshot (opt6
   versioning).
4. `dry_run=true` returns the preview without mutating.

**Response 200**

```json
// Normal:
{
  "forgotten": [
    {"fact_id": 123, "score": 1.0, "content_preview": "...", "tombstone_only": true}
  ]
}

// Dry run:
{
  "dry_run": true,
  "would_forget": [
    {"fact_id": 123, "score": 0.96, "content_preview": "...",
     "tombstone_only": false, "tier": "private", "user_id": "alice"}
  ],
  "note": "No mutation was performed."
}

// BM25 below threshold:
{
  "forgotten": [],
  "reason": "best BM25 score 2.14 below threshold 5.0",
  "candidates": [{"fact_id": 88, "score": 2.14}, {"fact_id": 91, "score": 1.8}]
}
```

**Errors**: `400 fact_id or query required`, `404 fact_id N not found`,
`403 permission_denied`, `500 bus tombstone/delete failed`.

**Tombstone vs hard-delete**:
- `tombstone_only: true` → row stays in `bus.memory_canonical` with
  `tombstoned=1`. Recalls skip it but you can `/v1/fact/<id>/restore`
  it. Embeddings preserved.
- `tombstone_only: false` (default) → row hard-deleted from `bus`,
  `nest`, `lex`. Cannot be restored. Use for GDPR / right-to-be-forgotten.

---

### `POST /v1/read/multi`  *(opt3-6)*

Cross-tier parallel recall. Runs the same query against multiple
`(tier, user_id)` scopes in parallel, re-ranks by combined score, and
returns a single merged list. The primary recall path for callers whose
identity spans both `public` (agent's shared knowledge) and
`private_<self>`.

**Body**

```jsonc
{
  "query": "user preferences",
  "top_k": 10,
  "hybrid": true,
  "scopes": [
    {"tier": "public", "user_id": null, "weight": 0.5},
    {"tier": "private", "user_id": "alice", "weight": 1.0}
    // omit `scopes` to use default (public 0.5 + private/<caller> 1.0)
  ]
}
```

**Response 200**

```json
{
  "count": 7,
  "results": [
    {"fact_id": 359, "score": 0.92, "tier": "private", "user_id": "alice", ...},
    {"fact_id": 12,  "score": 0.71, "tier": "public",  "user_id": null,   ...}
  ]
}
```

Z-score normalization is applied per scope before merging, so heavier
weights can pull up lower-ranked hits in their scope.

---

## Lifecycle: dedup + merge

### `POST /v1/merge/find`  *(opt3-6)*

Find candidate duplicate fact groups using cosine similarity + optional
LLM judge. `first_admin` only (matrix decision — merge is a destructive
operation).

**Body**

| Field | Required | Default | Notes |
|---|---|---|---|
| `tier` | no | `public` | Scope of the search |
| `user_id` | when private | — | Filter to one user |
| `threshold` | no | 0.92 | Cosine similarity threshold |
| `top_k` | no | 50 | Max candidates to consider |
| `use_llm` | no | `true` | LLM judge to confirm duplicates (vs near-miss paraphrases) |
| `max_groups` | no | 100 | Max groups to return |

**Response 200**

```json
{
  "tier": "public",
  "user_id": null,
  "candidate_count": 50,
  "group_count": 3,
  "groups": [
    {
      "group_id": 0,
      "size": 3,
      "method": "cosine+llm",
      "suggested_winner": 359,
      "losers": [360, 361],
      "llm_verdicts": [{"pair": [359, 360], "verdict": "duplicate", "confidence": 0.94}]
    }
  ],
  "threshold": 0.92,
  "top_k": 50,
  "use_llm": true
}
```

The `groups` are slim — embedding vectors stripped. Use the returned
`group_id` + `losers` to construct a merge list for `/v1/merge/apply`.

**Errors**: `403 merge requires first_admin`.

---

### `POST /v1/merge/apply`  *(opt3-6)*

Apply a reviewed merge list. `first_admin` only. Each entry picks a
winner and lists losers to fold into it. Writes a `merge_apply` row to
`bus.audit_log` per group.

**Body**

```jsonc
{
  "merges": [
    {
      "winner": 359,
      "losers": [360, 361],
      "method": "cosine+llm"   // for audit
    }
  ],
  "actor": "merge_v2_operator"
}
```

**Response 200**

```json
{
  "applied": 1,
  "merged_groups": [{"winner": 359, "losers": [360, 361], "audit_id": 102}],
  "audit": "merge_apply rows written to bus.audit_log"
}
```

---

## Provenance graph (fact lineage)

Each fact can have parent facts that contributed to its existence
(extracted-from-conversation, derived-from-fact, etc.). The provenance
graph traces these links for audit and explainability.

### `GET /v1/fact/<id>/provenance`  *(opt3-6)*

Get the full ancestry tree (parents → grandparents → ...) for a fact.

**Query params**

| Param | Default | Notes |
|---|---|---|
| `tier` | `public` | Which tier DB to read |
| `user_id` | — | Filter when `tier=private` |
| `max_depth` | 8 | Stop at this ancestor depth |

**Response 200**

```json
{
  "fact_id": 71,
  "nodes": [
    {"id": 71, "kind": "fact", "content_preview": "...", "depth": 0, "agent": "user"},
    {"id": 42, "kind": "event", "depth": 1, "agent": "forge.regex_v2"},
    {"id": 38, "kind": "event", "depth": 2, "agent": "discord.DM"}
  ],
  "edges": [
    {"from": 71, "to": 42, "kind": "extracted"},
    {"from": 42, "to": 38, "kind": "observed"}
  ],
  "max_depth_reached": 2
}
```

---

### `GET /v1/fact/<id>/lineage`  *(opt3-6)*

Get the **descendant** tree (children, grandchildren, ...). Useful for
"what did this conversation produce?" queries.

Same params/response shape as `/provenance`, just the direction is
flipped.

---

### `GET /v1/fact/<id>/graph.dot`  *(opt3-6)*

Render the provenance graph as **Graphviz DOT** for visual inspection.
Pipe through `dot -Tpng` to get a PNG.

**Query params**

| Param | Default | Notes |
|---|---|---|
| `direction` | `both` | `up` (parents), `down` (children), `both` |
| `tier`, `user_id`, `max_depth` | — | Same as `/provenance` |

**Response 200** with `Content-Type: text/vnd.graphviz`:

```dot
digraph provenance {
  71 [label="fact 71: ..."];
  42 [label="event 42\nforge.regex_v2"];
  38 [label="event 38\ndiscord.DM"];
  71 -> 42 [label="extracted"];
  42 -> 38 [label="observed"];
}
```

**Errors**: `404` if `fact_id` doesn't exist in that tier.

---

### `POST /v1/fact/<id>/provenance`  *(opt3-6)*

Record a new provenance edge. Useful when a tool / skill wants to
declare "I derived fact X from facts A and B".

**Body**

```json
{
  "parents": [42, 38],
  "kind": "extracted",      // free-form: extracted, derived, merged, observed, ...
  "agent": "forge.regex_v2",
  "depth": 1,                // optional override
  "tier": "public",
  "user_id": null
}
```

**Response 200**

```json
{"recorded": 1, "edges_added": 2, "audit_id": 89}
```

---

## Versioning + restore

### `GET /v1/fact/<id>/versions`  *(opt3-6)*

List all stored versions of a fact (snapshots taken at each write /
forget / restore event). Useful for "what did this fact used to say?"

**Query params**: `tier` (default `public`), `user_id`.

**Response 200**

```json
{
  "fact_id": 71,
  "tier": "public",
  "user_id": null,
  "version_count": 3,
  "versions": [
    {
      "version": 3, "ts": "2026-08-16T...", "actor": "restore_v1",
      "target_state": "current", "reason": "restore from v1",
      "old_state": null
    },
    {
      "version": 2, "ts": "2026-08-15T...", "actor": "rest_api",
      "target_state": "current", "reason": "edit",
      "old_state": {"columns": {"content": "old text"}, "tier": "public"}
    }
  ]
}
```

`old_state` is the full JSON snapshot of the row before this version
became current.

---

### `POST /v1/fact/<id>/restore`  *(opt3-6)*

Restore a fact to a previous version, or preview what restore would do.
`first_admin` only.

**Body**

```json
{
  "tier": "public",
  "user_id": null,
  "target_state": "preview",   // or "current" to commit
  "actor": "restore_v1"
}
```

**Response 200** (preview)

```json
{
  "preview": true,
  "would_restore_from": {"version": 2, "content": "...", "ts": "..."},
  "current_state": {"content": "...", "ts": "..."},
  "diff": {"fields_changed": ["content", "confidence"]}
}
```

**Response 200** (commit)

```json
{
  "preview": false,
  "restored_to": {"version": 2, "content": "..."},
  "audit_id": 110,
  "new_version": 4
}
```

---

### `GET /v1/snapshot/stats`  *(opt3-6)*

Daily snapshot statistics — the events captured for that date with
severity + counts. Useful for "what happened yesterday?" dashboards.

**Query params**

| Param | Default | Notes |
|---|---|---|
| `date` | today (UTC) | `YYYY-MM-DD` |
| `tier` | `public` | Which tier DB |
| `user_id` | — | When `tier=private` |

**Response 200**

```json
{
  "date": "2026-08-16",
  "tier": "public",
  "user_id": null,
  "counts_by_event_severity": {
    "forget": {"warning": 22},
    "merge": {"warning": 9},
    "promote_idempotent_replay": {"info": 64},
    "restore": {"info": 1}
  },
  "sample": [
    {"audit_id": 96, "ts": "2026-08-16T...", "event": "forget",
     "actor": "rest_api", "severity": "warning", "target_id": "1"}
  ]
}
```

---

## Stats + health

### `GET /v1/health`

Health check + DB status. **Always 200** when server is up.

**Response 200**

```json
{
  "status": "ok",
  "version": "1.1.1",
  "astor_dir": "D:\\AI\\Astor-Memory-Runtime",
  "dbs": {
    "bus": "D:\\AI\\Astor-Memory-Runtime\\public\\memory\\astor_bus_public.db",
    "nest": "D:\\AI\\Astor-Memory-Runtime\\public\\memory\\astor_nest_public.db"
  },
  "facts": 866,
  "events": 86,
  "embeddings": 585
}
```

---

### `GET /v1/viewer/stats`  *(v1.1.0 — MemoraX Viewer)*

**Content-free** stats — counts only, NO fact content. Per MemoraX
architecture rule, the Viewer is a local projection, not memory
authority. Use for dashboards / health monitoring / writeback-status
without leaking PII.

**Response 200**

```json
{
  "version": "1.1.1",
  "astor_dir": "D:\\AI\\Astor-Memory-Runtime",
  "generated_at": "2026-08-16T...Z",
  "counts": {
    "facts_total": 6808,
    "facts_by_tier": {"public": 618, "source": 3080, "private": 3104, "repo": 6},
    "facts_by_scope": {"long_term": 6243, "short_term": 4, "profile": 5},
    "events_total": 3096,
    "embeddings_total": 6804,
    "candidates_total": 1260,
    "forge_audit_total": 6956,
    "dedup_hits_total": 64
  },
  "dbs": {
    "private/admin/bus": {"path": "...", "size_bytes": 11259904},
    "private/admin/nest": {"path": "...", "size_bytes": 12931072},
    ...
  },
  "last_activity_ts": "2026-08-16T...",
  "schema_versions": {...}
}
```

Scans all 9+ DBs (3 stores × 4 tiers) including N users and N repos.

---

### `GET /v1/lex/stats`

Per-tier BM25 (lex) index stats — document count, term count, tombstone
count.

**Response 200**

```json
{
  "private/admin": {"documents": 3104, "terms": 9197, "tombstoned": 0},
  "private/user_c": {"documents": 8, "terms": 240, "tombstoned": 0},
  "public/_": {"documents": 5, "terms": 21, "tombstoned": 0},
  "source/_": {"documents": 12, "terms": 145, "tombstoned": 0},
  "version": 1
}
```

---

## Admin + installer

### `POST /v1/reload`

Re-exec the server process via `os.execv` so module caches pick up
fresh source without manual restart. **`first_admin` only** — verified
via `astor_check_bot_admin()`.

**Body**: empty.

**Response 200**

```json
{"reloaded": true, "pid": 39024}
```

Server process exits with status 0 after responding; the caller (or a
supervisor like NSSM / restart.py) brings up a new process with the
same port + ASTOR_DIR.

---

### `POST /v1/install`

Plan an install into another agent (Claude Code / Cline / Cursor / ...).
Returns file changes without writing them — review first, then apply.

**Body**

| Field | Required | Notes |
|---|---|---|
| `ide` | yes | `claude-code`, `cline`, `cursor`, etc. |
| `mode` | no | `priority` / `coexist` / `replace` / `verify` / `auto` |
| `agent_dir` | no | Agent home, default `~` |

**Response 200**

```json
{
  "plan": {
    "agent": "claude-code",
    "mode": "coexist",
    "tier": "C",
    "changes": [
      {"path": "~/.claude/instructions.md", "action": "append",
       "preview": "## Astor-Memory hook ..."}
    ],
    "notes": ["priority marker prepended to instructions.md"]
  }
}
```

See [`agent-adapters.md`](./agent-adapters.md) for the per-IDE install
contract.

---

## Versioning this reference

This document is updated alongside the code. If you find a mismatch:

1. Check `am --version` — should be ≥ 1.1.1
2. Compare against `astor_memory/server.py` @app.route decorators
3. File an issue with the actual vs documented behavior

The endpoint list above maps 1:1 to `@app.route(...)` decorators in
`server.py:152-1108` as of 2026-08-16.
