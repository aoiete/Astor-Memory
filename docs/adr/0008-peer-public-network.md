# 0008. Peer-Public Network — background sync for the public tier

- Status: accepted
- Date: 2026-09-19
- Deciders: admin (first_admin), astor-memory maintainers
- Source: Phase C-D ship log (v1.14.54–62), Phase B identity model,
  **existing untracked implementation** at
  `astor_memory/_internal/peer_relationships.py` (v1.14.68, the friend +
  trust + blacklist schema) and
  `astor_memory/_internal/peer_search.py` (v1.14.73, demand-driven PPS),
  Hermes-Telegram bridge in `platform_bridge.py`,
  operational observation that production (PID 67292) is one of N
  intended peers.

## Context and Problem Statement

astor-memory today runs as a single-node deployment. Phase C-D locked
the identity model (`agent_id` / `user_id` / `transport`) and the
tier routing rules. The **public** tier exists as a SQLite DB on
disk and is the one tier that *could* be safely shared across nodes.
The user explicitly committed to a **federation model** — peers sync
public content with each other, no central server, no remote-direct
RPC.

Two pieces of the federation already exist as untracked files:

1. **`peer_relationships.py`** (v1.14.68) — the social graph: who this
   astor trusts, who it ignores, per-topic trust weights, rekey
   chain. Schema uses `peer_id = "astor:" + <32 hex>`, trust 0-100 with
   tiered semantics, topic_index for per-topic subscriptions, separate
   SQLite DB at `$ASTOR_DIR/identity/relationships.db`.

2. **`peer_search.py`** (v1.14.73) — Demand-driven Peer Public Search
   (PPS): on `recall`, query friends' public tier; signed requests
   with trust ≥ 50 gate, opt-in per friend, 7-day timestamp freshness,
   3s per-peer timeout, 1MB response cap. **NO push, NO continuous
   pull — pure demand-driven.**

What is **missing** as a documented design + a shippable surface:

- **Background sync.** PPS is on-demand only; nothing pulls facts
  continuously so a peer B can have stale or missing public content
  until somebody searches for it. Without background sync, the public
  tier of B diverges from A until the next search hits.
- **REST endpoints** (`/v1/peer/*`) and **CLI** (`am peer ...`) that
  let the existing `peer_relationships` and `peer_search` modules be
  exercised remotely. Right now the modules are import-only.
- **Manifest sync protocol** for the background sync (analogous to
  PPS's signed request, but pull-based and idempotent).
- **Tombstone propagation** so a fact deleted on peer A disappears on
  peer B.
- **Decision and ADR** so the user has a single document to read
  before changing any of this — the design lives in 8 files now and
  is hard to keep in your head.

This ADR documents the existing implementation, identifies what's
missing, and decides the missing pieces. It also commits to git-
tracking the two untracked files so they don't get lost between
sessions.

## Goals

- **G1.** Document the existing friend + trust + PPS design so future
  agents and the user can read it from one place.
- **G2.** Add background sync on top of the existing PPS so a peer's
  public tier converges without requiring active search.
- **G3.** Every existing peer operation (add/list/remove/sync/audit/
  search) is reachable via CLI + REST, not just import.
- **G4.** Backwards compatible: a peer running only PPS keeps working.
- **G5.** Git-track the two untracked modules so they survive
  sessions.
- **G6.** Each phase ships independent value. A user who stops after
  F1 still has a working CLI; F2 adds background sync; F3 adds gossip.

## Non-Goals

- N1. Sync of `private_*user*` or `source` tiers (deliberately out of
  scope; tier semantics never change).
- N2. Replacing the existing on-demand PPS with background sync. Both
  coexist — background sync for convergence, PPS for explicit cross-
  peer search.
- N3. Central server / central index / central discovery.
- N4. Per-fact streaming push (the model is bulk manifest + bulk fact
  fetch; no live updates).
- N5. End-to-end encryption above what TLS gives.

## Existing design (Phase F0 — already shipped, untracked)

Capture the design that's already in the repo as untracked files. This
section is the source of truth for what's there; the implementation
plan below is what to ADD.

### Peer identity

- `peer_id` format: `"astor:" + <32-hex>`. Total length ~38 chars.
  Validated in `peer_relationships.add_peer`.
- Ed25519 keypair per peer. Public key stored in `peer_relationships.
  public_key`. Private key lives at `~/.astor/identity/` (chmod 0600).
- `kind`: `friend` | `blacklist` | `whitelist` | `pending`.

### Trust semantics (locked 2026-09-16, fact 12610/12611)

| Range | Meaning | Sync behavior |
|-------|---------|----------------|
| 0 | blacklisted | auto-reject all incoming |
| 1–29 | very low | quarantine all incoming |
| 30 (default) | new peer | quarantine |
| 31–49 | low | manual accept only |
| 50–69 | medium | auto-accept with caution |
| 70–89 | high | auto-accept, KEEP trust on rekey |
| 90–100 | very high | auto-accept, KEEP trust on rekey, broadcast back |

### Per-topic trust

`topic_weights`: JSON dict, e.g. `{"poker": 0.9, "cooking": 0.4}`. Per-
topic multiplier on the trust threshold for PPS requests. Stored in
`peer_relationships.topic_weights`, separate table `topic_index`
tracks (topic, peer_id, weight, fact_count, last_seen_at, source).

### Rekey

When a peer's identity rotates (private key compromise, migration),
the new peer_id is published alongside `rekey_chain: [<old_ids>...]`.
Trust ≥ 70 is preserved across rekey; trust < 70 drops back to 30.
Stored in `rekey_log` table.

### Storage location

```
~/.astor/
├── identity/
│   └── relationships.db   # peer_relationships + rekey_log + topic_index
└── peer_identity/         # private key (chmod 0600)
```

### On-demand PPS (peer_search.py)

- Caller builds `PeerSearchRequest` dataclass (construct-time
  validation: invalid fields raise before the object exists).
- Signed ed25519 over `(requestor_peer_id + query + topic + ts)`.
- Timestamp freshness: `now-7d < ts < now+60s` (skew tolerance).
- Trust gate: target list filtered to friends with trust ≥ 50.
- Opt-in: friend must run `am peer allow-search <my_peer_id>` (default
  off) before their public tier will respond.
- Per-call caps: 20 facts, 1 MB / peer, 3 s / peer, 10 s total.
- Read-only: results come back, caller manually adopts via
  `am peer adopt <fact_id>` if they want to persist.
- Tier restriction: friends only return public tier.

## What's missing — Phase F1 (this ADR's decision)

Add the **background manifest sync** layer on top of the existing
social graph + PPS. Specifically:

### F1.A — REST endpoints for peer operations

Mount the existing module functions as REST. Admin-only.

| Endpoint | Verb | Body / params | Calls |
|----------|------|----------------|-------|
| `/v1/peer/peers` | GET | — | `list_peers()` |
| `/v1/peer/peers` | POST | `{peer_id, alias?, trust?, public_key?, endpoint?, topic_weights?, metadata?}` | `add_peer()` |
| `/v1/peer/peers/<id>` | GET | — | `get_peer()` |
| `/v1/peer/peers/<id>` | PATCH | `{trust?, alias?, endpoint?, ...}` | partial update |
| `/v1/peer/peers/<id>` | DELETE | — | `remove_peer()` |
| `/v1/peer/peers/<id>/trust` | POST | `{trust: 0-100}` | `update_trust()` |
| `/v1/peer/peers/<id>/allow-search` | POST | `{allow: bool}` | (toggle opt-in) |
| `/v1/peer/manifest` | GET | `?since=<unix_ts>` | (new) |
| `/v1/peer/facts` | POST | `{fact_ids: [...]}` | (new) |
| `/v1/peer/tombstone` | POST | `{fact_id, reason}` | (new) |
| `/v1/peer/topics` | GET | — | `list_all_topics()` |
| `/v1/peer/topics` | POST | `{topic, peer_id, weight?, source?}` | `set_topic()` |
| `/v1/peer/topics` | DELETE | `{topic, peer_id}` | `remove_topic()` |
| `/v1/peer/search` | POST | `{query, topic?, limit?, friends_only?}` | `dispatch_search_to_peers()` |
| `/v1/peer/sync` | POST | `{peer_id?}` | (new — triggers pull) |
| `/v1/peer/audit` | GET | `?since=<unix_ts>` | (new — audit log of sync events) |

All `/v1/peer/*` endpoints require admin role. Sync endpoints also
require `X-Astor-Peer-Token` header (per-peer random 256-bit secret
stored in `peer_relationships.metadata.auth_token`).

### F1.B — `am peer` CLI

Wire existing module functions + new sync into a CLI subcommand:

```
am peer init [<peer_id>]              # generate keypair if missing
am peer show                          # show my peer_id + pubkey fingerprint
am peer add <id> [--pubkey=...] [--endpoint=...] [--trust=30]
am peer list                          # show all relationships + last_sync + last_topic_seen
am peer show <id>                     # one peer detail
am peer trust <id> <0-100>            # update trust
am peer remove <id>
am peer sync                          # one-shot pull from all sync-enabled peers
am peer sync --peer=<id>             # one-shot pull from one peer
am peer allow-search <id>             # toggle my opt-in for peer <id>'s PPS queries
am peer topic add <topic> [--peer=<id>] [--weight=1.0]
am peer topic list
am peer topic remove <topic> [--peer=<id>]
am peer audit                          # recent sync events from audit log
am peer search <query> [--topic=X] [--limit=20]   # PPS dispatch
```

### F1.C — Manifest sync protocol (new)

On top of the existing social graph, add a pull-based manifest sync:

1. **My manifest:** local astor computes `{"peer_id", "ts",
   "fact_ids": [int], "manifest_sig"}` from the `public` tier bus DB
   (only `tombstoned=0` facts, sorted by id). Signed with my
   ed25519 private key.
2. **Pull loop:** every `sync_interval_seconds` (default 300), for each
   peer where `kind='friend' and trust >= 50` and `peer_sync_enabled=true`:
   - `GET https://peer/v1/peer/manifest` (over mTLS, with
     `X-Astor-Peer-Token`).
   - Verify manifest_sig against peer's stored `public_key`. Reject
     on mismatch (audit WARNING).
   - Compute diff: facts in remote manifest NOT in my public tier.
   - `POST /v1/peer/facts` with the diff (max 500 facts / call per
     rate cap).
   - For each received fact, insert into my public bus DB with
     synthetic tags `peer:<remote_peer_id>`, `synced:<iso_ts>`. Caller
     field set to `peer:<remote_peer_id>` for audit.
3. **Tombstone propagation:** `POST /v1/peer/tombstone` propagates
   deletions; receivers tombstone locally.
4. **Manifest signature format:** canonical JSON, sorted keys, signed
   over `{"peer_id", "ts", "fact_ids"}` (signature excluded from the
   signed bytes).
5. **Replay window:** reject manifests with `ts` older than 5 minutes
   (clock-skew tolerance).
6. **LWW conflict resolution:** if both peers have a fact with same
   `stable_id`, the one with newer `last_confirmed_at` wins; tie =
   keep both, dedup at read time.

### F1.D — Rate limiting & backpressure

Per-peer caps in `peer_relationships.metadata`:
- `max_facts_per_pull = 500` (default)
- `max_concurrent_pulls = 2`
- `pull_timeout_seconds = 30`
- `burst_limit = 1000` per 60 s

A peer exceeding burst gets auto-quarantined for 10 minutes (WARNING
audit row, sync paused).

### F1.E — Git-track the untracked files

Add `peer_relationships.py` and `peer_search.py` to the repo. They
should be in v1.14.63+ once committed. Update CHANGELOG.

## Considered Options

### A — Replace existing PPS with background sync
- Pro: simpler model.
- Con: loses on-demand search; breaks backwards compat.
- **Decision: rejected.** PPS + background sync coexist; they serve
  different access patterns (push vs pull, on-demand vs continuous).

### B — Central relay
- Pro: simplest sync.
- Con: violates user's "no central" constraint.
- **Decision: rejected.**

### C — Pull-based manifest sync on top of existing social graph (chosen)
- Pro: deterministic, auditable, works offline, builds on what's there.
- Con: still no auto-discovery; admin maintains `peer_relationships`.
- **Decision: chosen.** Auto-discovery is non-goal N3.

## Implementation Plan

### F1.1 — Endpoints + CLI (smallest shippable unit)

1. **REST endpoints** (`server.py`): add `/v1/peer/*` routes.
   - Map each route to existing `peer_relationships` module function.
   - Sync routes (`/v1/peer/manifest`, `/v1/peer/facts`,
     `/v1/peer/tombstone`, `/v1/peer/sync`) call into a new
     `astor_memory/peer/sync.py` module.
   - PPS route (`/v1/peer/search`) calls existing
     `peer_search.dispatch_search_to_peers`.
   - Auth: admin role + `X-Astor-Peer-Token` header check.

2. **CLI subcommand** (`cli/main.py`):
   - `am peer init|show|add|list|show|trust|remove|sync|allow-search|
     topic|audit|search`.
   - Wire to existing functions.

3. **Tests** (`tests/test_peer_endpoints.py` + `tests/test_peer_cli.py`):
   - Two test peers on ports 7803 + 7805 sharing the same peer DB.
   - Add peer via CLI; verify it shows in `/v1/peer/peers`.
   - Seed fact on 7803 public tier; trigger sync on 7805; assert
     fact appears in 7805's public DB with `peer:<id>` tag.

4. **CHANGELOG** entry for v1.14.63 (or whatever the ship target is).

### F1.2 — Background sync daemon

1. `astor_memory/peer/sync.py` — `pull_from_peer(peer_entry,
   astor_dir) -> SyncReport`. Idempotent (re-runnable).
2. `astor_memory/peer/daemon.py` — optional daemon loop. Default off
   (admin opts in via `peer_sync_enabled = true` in
   `peer_relationships.metadata`). Falls back to one-shot
   `am peer sync` cron if daemon is undesirable.
3. `am peer sync` (one-shot) usable as a systemd timer / cron job for
   the daemon-free path.
4. Audit log rows for every sync (success / failure / rate-limit /
   quarantine).

### F1.3 — Rate limiting + backpressure + quarantine

1. Per-peer caps stored in `peer_relationships.metadata`.
2. `astor_memory/peer/rate_limit.py` — sliding-window counter.
3. Auto-quarantine on burst violation (trust → 1 for 10 minutes).

### F1.4 — CHANGELOG + ADR revisions

Update ADR 0008 to reflect what F1 actually shipped (vs what was
planned here).

## Architecture at a Glance

```
┌──────────────────────┐         ┌──────────────────────┐
│  peer A                │         │  peer B                │
│  peer_id: astor:a1b2   │ ◄─────► │  peer_id: astor:c3d4   │
│  pubkey: ed25519:AAAA   │  HTTPS   │  pubkey: ed25519:CCCC  │
│  trust(B) = 70         │  mTLS    │  trust(A) = 70        │
│  ┌──────────────────┐ │  /v1/    │  ┌──────────────────┐ │
│  │ public tier       │ │  peer/   │  │ public tier       │ │
│  │ astor_bus_public  │ │  manifest│  │ astor_bus_public  │ │
│  │ .db               │ │  facts   │  │ .db               │ │
│  └──────────────────┘ │  tombstone│  └──────────────────┘ │
│  + peer_relationships  │         │  + peer_relationships  │
│    db (trust, etc.)     │         │    db (trust, etc.)     │
│  + PPS (on-demand)      │         │  + PPS (on-demand)      │
│  + background sync (F1.2)│         │  + background sync (F1.2)│
└──────────────────────┘         └──────────────────────┘
                ▲                              ▲
                └──── am peer CLI / REST ──────┘
```

## Security Considerations

- **Transport:** HTTPS or mTLS. Client MUST verify TLS cert.
- **Auth (REST endpoints):** admin role + `X-Astor-Peer-Token` header.
  Token = per-peer random 256-bit secret, stored in
  `peer_relationships.metadata.auth_token` (chmod 0600 on the
  receiving peer).
- **Auth (PPS):** ed25519 signature on every request, 7-day freshness,
  replay window.
- **Manifest signature:** ed25519 over canonical-JSON payload
  (`peer_id`, `ts`, sorted `fact_ids`). Receiver verifies against
  allowlist-stored public_key.
- **Replay protection:** manifests older than 5 minutes rejected.
- **Tombstones propagate:** if peer A tombstones a fact, all
  tombstone-listening peers see it in the next sync.
- **A compromised peer can only inject/withhold PUBLIC content.**
  Private tier never crosses the wire; tier semantics enforced at the
  receiver's local LOCK-rule path.
- **Rekey chain:** trust ≥ 70 survives rekey; < 70 drops to 30
  (quarantine). Audited in `rekey_log`.

## Rollback

- Each F1.X phase ships behind `peer_sync_enabled = false` per peer
  (or daemon opt-out). Admin enables incrementally.
- Misbehaving peer: `am peer trust <id> 0` (blacklist) or
  `am peer remove <id>`.
- Sync never deletes local data (only adds + tombstones facts already
  tombstoned upstream). Admin can re-seed and manually tombstone bad
  rows.

## Consequences

### Positive

- The 8-file design collapses to one ADR. Future agents + the user
  can read this single document.
- The two existing untracked modules get git-tracked, surviving
  sessions.
- Background sync complements PPS: peer B's public tier converges
  with peer A's without requiring active queries.
- Reuses existing trust model + rekey chain — no parallel infra.

### Negative

- A new admin surface (CLI + 13 REST routes) to maintain.
- Sync adds write-side DB activity that doesn't exist today
  (every 5 minutes per peer) — small but real.
- The peer_id format change (`astor:<32hex>`) is locked in; any
  existing peer with a different format needs to migrate.

### Neutral

- On-demand PPS stays as-is. Background sync is additive.
- Admin's `peers.toml` becomes `peer_relationships` (DB-backed).
- Total new surface: ~400 lines (routes + sync + CLI) + tests.

## Open Questions

- Q1. Should `am peer sync` be one-shot CLI (cron-friendly) or a
  background daemon by default? Default to one-shot, daemon is opt-in.
- Q2. When peer B receives peer A's facts, do we also re-run the
  local LOCK rule evaluator on them, or trust the sender's tags?
  Default: trust sender's tags, re-evaluate on demand.
- Q3. What about source-tier admin facts that should be readable
  cross-peer (e.g. shared admin guidelines)? Out of scope; admin can
  re-publish to public tier if needed.
- Q4. Does the trust ≥ 70 "broadcast back" semantic interact with the
  pull-only sync model? "Broadcast back" implies push; defer to F2.

## References

- `astor_memory/_internal/peer_relationships.py` (v1.14.68) —
  existing schema, friend + trust + rekey. Untracked; commit as
  part of F1.
- `astor_memory/_internal/peer_search.py` (v1.14.73) — existing PPS
  on-demand search. Untracked; commit as part of F1.
- `astor_memory/_internal/platform_bridge.py` — Hermes-Telegram
  bridge; reference for how peer ops interact with the bot layer.
- `docs/adr/0001-9db-layout.md` — tier isolation (G3 foundation).
- `docs/adr/0007-consolidate.md` — recent ship pattern for context.
