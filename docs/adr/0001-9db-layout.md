# 0001. 9-DB SQLite layout (3-tier × 3-store)

- Status: accepted
- Date: 2026-08-15
- Deciders: admin (first_admin), astor-memory maintainers
- Source: astor-memory reverse-design session, 2026-08-13

## Context and Problem Statement

astor-memory needs to store facts across three logically distinct scopes
(public / source / private) for multiple users in a single self-hosted
deployment. The naive approach — one big SQLite with `tier` and `user_id`
columns — was rejected because:

1. **Public tier** is shared across all users and read by every agent —
   it must survive per-user DB corruption.
2. **Source tier** is admin-only design notes / architecture decisions —
   leaking it would expose system internals.
3. **Private tier** is per-user — cross-user access is a privacy bug
   class, not just an ACL bypass.

Naively putting all three in one DB means a single SQL bug (e.g. missing
WHERE clause) can leak source notes to a free-tier user. The tier
boundary needs to be **physical**, not just a column.

## Considered Options

- **A — Single SQLite with tier column** — simple, but no physical
  isolation; ACL bypass = data leak.
- **B — One DB per user (single tier each)** — physical isolation but
  loses the "shared public" notion; every user has their own copy of
  common facts, no cross-user recall.
- **C — 9-DB layout** — 3 tiers × 3 stores = public/source/private per
  bus/forge/nest. Plus per-user private DBs (`users/<u>/memory/`) for
  the 28 user-specific facts we have today. Public + source stay
  shared.

## Decision Outcome

Chosen option: **C (9-DB layout + per-user private DBs)** because it
makes the tier boundary physical (a missing WHERE clause can't leak
across `astor_bus_public.db` and `astor_bus_admin.db` at the OS level),
while preserving the shared-public + admin-private + per-user-private
shape that fits the multi-tenant model.

### Concrete shape

```
D:/AI/Astor-Memory-Runtime/
├── public/memory/
│   ├── astor_bus_public.db
│   ├── astor_forge_public.db
│   └── astor_nest_public.db     # 3901 embeddings, 4675 events, 3914 facts
├── source/memory/               # admin-only design notes
│   ├── astor_bus_source.db
│   ├── astor_forge_source.db
│   └── astor_nest_source.db
└── users/<long_alias>/memory/   # per-user private
    └── astor_<store>_<user>.db
```

### Server-side enforcement

`server.py:_astor_bind_request_acl` resolves the actor from `X-Actor`
header (v1.14.37) and binds them to a specific tier DB. The tier DB is
opened **per-tier**, not per-request with row-level filtering — so the
SQL path simply doesn't have access to other tiers.

## Consequences

### Good

- **Physical isolation** — tier leak requires OS-level access, not just
  a missing WHERE clause.
- **Per-user private DBs** — admin can audit any user's private tier
  without leaking it to other users (verified end-to-end in v1.14.37).
- **Backup granularity** — backup public tier independently of private
  tiers (different retention, different sensitivity).
- **Migration** — per-tier migration scripts don't need to touch other
  tiers; lower blast radius.

### Bad

- **Schema coordination** — schema changes (e.g. v9→v10 `created_at`
  column) must be applied to all 9 DBs. Mitigated by `bus/schema.py`'s
  idempotent CREATE TABLE IF NOT EXISTS, run on every server start.
- **Recall complexity** — `/v1/read` for admin reads public + source +
  admin-private; for free users, public only. The path is straightforward
  but documented in `server.py` for the next maintainer.
- **No FK across tiers** — facts in public can't have FK to facts in
  private. We use `parent_fact_ids` (JSON list) instead.

### Risks

- **Schema drift** — if a DB is missed on migration, that tier silently
  fails reads. Mitigated by `astor_audit.py` daily cron that asserts
  schema version equality across all DBs.
- **Per-user DB explosion** — 28 users today, manageable. If we onboard
  1000s, consider sharding by `users/<u>/memory/<shard>/`. Not a
  problem today.

## Related

- ADR-0002 — hybrid retrieval uses 9 DBs via tier-specific nest.
- docs/architecture.md § "Storage layout".
- astor write path: `server.py:_astor_bind_request_acl`.
