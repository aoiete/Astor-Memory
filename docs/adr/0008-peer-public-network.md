# 0008. Peer-Public Network — federating the public tier across astor nodes

- Status: accepted
- Date: 2026-09-19
- Deciders: admin (first_admin), astor-memory maintainers
- Source: Phase C-D ship log (v1.14.54–62), Phase B identity model
  (cross-channel role routing), operational observation that
  production (PID 67292, hostname lts-nutspc) is one of N intended
  peers and currently has no way to share public-tier facts with
  other peers

## Context and Problem Statement

astor-memory today runs as a single-node deployment on one machine.
Phase C-D locked the identity model (`agent_id` / `user_id` /
`transport`) and the tier routing rules. Phase B shipped
cross-channel role resolution for bot transports. The **public** tier
exists as a SQLite DB on disk (`public/memory/astor_bus_public.db`)
and is the one tier that *could* be safely shared across nodes — every
fact there is either admin-curated (`rule_ship` LOCK rule action) or
explicitly publishable, with no per-user privacy concerns.

The gap: today there is no way for facts in peer A's `public` tier to
reach peer B. The user's stated direction ("we don't do remote direct;
only future is peer-to-peer public network design") commits astor to a
**federation model** — peers sync public content with each other, no
central server, no remote-direct RPC. This ADR specifies that
federation model end-to-end so it can be implemented and shipped
incrementally even if work is interrupted (a partial implementation
must still produce a working, auditable state).

## Goals

- **G1.** Multiple astor peers (≥ 2) on different machines can sync
  public-tier facts without a central coordinator.
- **G2.** Each peer retains full local control over what it accepts
  from whom (admin allowlist + signed manifest).
- **G3.** Sync is **public tier only**. `private_*user*` and `source`
  never leave the originating peer, even at the wire level.
- **G4.** Sync is **eventually consistent**. No synchronous global
  ordering; CRDTs or last-write-wins are acceptable for v1.
- **G5.** A peer can run **read-only** (never publishes, only
  subscribes). Symmetric publish+subscribe is the default.
- **G6.** The public tier remains useful for a **single peer with no
  network** — no regression to local-only setups.

## Non-Goals

- N1. Private / source / repo tier sync (deliberately out of scope;
  tier semantics never change).
- N2. Per-fact pub/sub for live updates (the model is bulk-sync
  via manifest diff, not streaming).
- N3. Central server, central index, central discovery (the design
  is point-to-point + gossip only).
- N4. Crypto beyond signed manifest + TLS — no end-to-end encryption
  layer above what HTTPS / mTLS gives us.
- N5. Mobile / embedded targets. Peers are full astor installs on
  machines an admin runs.
- N6. Sync during the initial install (a peer with no `peer_id` /
  no peer public key cannot sync until `am peer init` runs).

## Architecture at a Glance

```
┌─────────────────┐         ┌─────────────────┐
│  peer A          │         │  peer B          │
│  peer_id: alice   │ ◄────► │  peer_id: bob     │
│  public_key: A   │  sync   │  public_key: B   │
│  ┌─────────────┐ │  over  │  ┌─────────────┐ │
│  │ public tier  │ │ HTTPS /│  │ public tier  │ │
│  │ astor_bus_   │ │ mTLS / │  │ astor_bus_   │ │
│  │ public.db    │ │gossip  │  │ public.db    │ │
│  └─────────────┘ │        │  └─────────────┘ │
│  + am peer ...   │         │  + am peer ...   │
└─────────────────┘         └─────────────────┘
       ▲                              ▲
       │                              │
       └────── /v1/peer/sync ────────┘
              (admin allowlist)
```

## Considered Options

### A — Central relay server
A single always-on relay that all peers push to and pull from.
- Pro: simplest sync semantics.
- Con: violates G2/G3 in spirit (relay can inspect or drop facts);
  contradicts user's "no central" constraint; operationally fragile.

### B — Gossip / epidemic
Each peer gossips its manifest to N random peers on a timer;
receivers pull diffs lazily.
- Pro: scales to hundreds of peers; no fixed topology.
- Con: eventually consistent with long convergence tails; harder to
  reason about for ≤ 10 peers (the realistic fleet size).

### C — Static peer allowlist + pull-based diff (chosen)
Each peer has `~/.astor/peers.toml` listing trusted peer endpoints
(`https://host:port`). Each peer periodically pulls `/v1/peer/manifest`
from each trusted peer, computes the diff, and fetches missing facts
via `/v1/peer/facts?ids=...`.
- Pro: deterministic; auditable; works offline; trivially testable
  with two peers on one machine on different ports.
- Con: doesn't auto-discover peers (admin adds them by hand).

**Decision: C.** Convergence within the realistic fleet (≤ 10
peers) is fast (one pull every N minutes per peer). Auto-discovery is
explicitly out of scope (N3). When the fleet grows, the same
allowlist can be reused by a thin gossip layer in Phase F3.

### D — Push-based (peer A pushes whenever it writes)
- Pro: lower latency.
- Con: requires every peer to know every other peer; pushes don't
  survive downtime (need queues); denial-of-service surface.

**Decision: pull-based.** Push is a Phase F3 optimization for
high-churn public tiers.

## Decision

Adopt **option C (static peer allowlist + pull-based diff)** as the
federation model. Phase F1 ships a minimum-viable version of all nine
mechanism groups listed in §Implementation Plan; later phases harden
each one.

## Detailed Design

### 1. Peer Identity

- Each peer has a stable `peer_id` (string, kebab-case, 1-64 chars)
  and an ed25519 keypair. The private key never leaves the peer.
- Keypair lives at `~/.astor/peer_identity/` (private key chmod 0600).
- Public key is published in the peer manifest; receivers verify
  signatures against the allowlist-known pubkey.
- `am peer init <peer_id>` generates a keypair if missing.
- `am peer show` prints peer_id + public key fingerprint.
- Migration: if no peer_id is set, the peer is **single-node only**
  — sync endpoints return 403 until `am peer init` runs.

### 2. Discovery

- No mDNS, no bootstrap nodes, no DHT (per N3).
- Admin maintains `~/.astor/peers.toml`:

  ```toml
  [[peer]]
  id            = "alice-laptop"
  base_url      = "https://alice.lan:7803"
  public_key    = "ed25519:abcd1234..."
  allow_publish = true   # false = read-only pull from this peer
  last_seen_ts  = 0       # filled in by sync loop

  [[peer]]
  id            = "bob-server"
  base_url      = "https://bob.example.com:7803"
  public_key    = "ed25519:efgh5678..."
  allow_publish = false
  last_seen_ts  = 0
  ```

- Sync loop reads this file every `sync_interval_seconds` (default
  300 = 5 min).

### 3. Sync Protocol

For each peer in `peers.toml`:

1. **Fetch manifest:** `GET /v1/peer/manifest?since=<last_manifest_ts>`
   - Returns: `{peer_id, ts, fact_ids: [int], manifest_sig: ...}`
   - The manifest is signed by the source peer's private key.
   - `since` is optional; if omitted, full manifest.
2. **Verify signature** against the configured `public_key` for that
   peer_id. Mismatch → log warning, skip peer this round.
3. **Compute diff:** local `public` facts minus remote `fact_ids`.
4. **Fetch missing facts:** `POST /v1/peer/facts {fact_ids: [...]}`
   - Returns a list of fact rows (same shape as `/v1/read/multi`).
5. **Apply locally:** insert into public bus DB. Tags get a synthetic
   `peer:<peer_id>` prefix so the receiver can later prove the origin
   (audit trail).
6. **Update `last_seen_ts` + `last_manifest_ts`** in `peers.toml`.

Sync is single-direction per round: the receiver never auto-replies
back unless the receiver also pulls in its own loop. This keeps
topology explicit.

### 4. Conflict Resolution

- **LWW (last-write-wins) on `last_confirmed_at`** — if both peers
  have a fact with the same `stable_id`, the one with the newer
  `last_confirmed_at` wins. Ties: keep both, dedup at read-time by
  `stable_id`.
- **No CRDTs in v1.** Rationale: the public tier is admin-curated
  (LOCK-rule-ship'd) so churn is low; CRDTs would be over-engineering.
- **Tombstones propagate.** If peer A tombstones a fact it previously
  published, peer B sees the tombstone in the next manifest pull and
  tombstones locally.

### 5. Tier Semantics at Receive Side

- Synced facts land in the **public** tier only. Private/source/repo
  tiers are unreachable from the sync protocol.
- Public facts arriving from a peer are NOT auto-promoted to higher
  tiers locally. They stay public. If the local LOCK rule evaluator
  (`/v1/classify` Path 2) needs them, it'll find them.
- Tag enrichment: each synced fact gets `peer:<source_peer_id>` and
  `synced:<iso_ts>` added to its tags so audit can distinguish
  locally-authored from peer-authored.

### 6. Rate Limiting & Backpressure

- **Default caps** (admin-tunable in `peers.toml` per-peer):
  - `max_facts_per_pull = 500` (per round)
  - `max_concurrent_pulls = 2`
  - `pull_timeout_seconds = 30`
- On rate-limit hit, receiver applies **backpressure**: returns
  `429 Too Many Requests` with `Retry-After`. Sender halves
  `sync_interval_seconds` for that peer (with floor).
- Hard cap: a peer sending > `burst_limit` in any 60-second window
  gets **auto-quarantined** for 10 minutes (logged at WARNING level,
  alert row written to audit log).

### 7. Manifest & Signature

- **Manifest** = sorted list of public-tier fact_ids + the peer's
  current manifest timestamp.
- Format: `{"peer_id": "alice-laptop", "ts": 1700000000,
  "fact_ids": [1, 2, 3, ...], "manifest_sig": "ed25519:<base64>"}`
- Signature covers everything except `manifest_sig` itself
  (canonical JSON, sorted keys, no whitespace).
- Verification: receiver decodes signature with the allowlist
  public_key; reject on mismatch.
- Manifests are NOT chained (each is independent). A peer that
  missed 3 syncs can re-sync by omitting `since`.

### 8. Security Boundaries

- **Transport:** HTTPS or mTLS. The sync HTTP client MUST verify
  TLS cert (no `verify=False`).
- **Auth:** The peer endpoints (`/v1/peer/*`) require a `X-Astor-Peer-Token`
  header. Token = a per-peer random 256-bit secret, stored in
  `peers.toml` as `auth_token = "..."`. Server side validates the token
  matches the peer_id; mismatch → 401.
- **Body content:** Manifest + facts payload are NOT additionally
  encrypted — the tier semantics + LOCK-rule path already keep PII out
  of public. We rely on TLS for in-transit confidentiality.
- **Replay protection:** Each manifest carries `ts`; receivers reject
  manifests older than 5 minutes (clock skew tolerance).

### 9. Phased Rollout

| Phase | Scope | Ship target |
|-------|-------|--------------|
| **F1 (MVP)** | Identity + manifest endpoint + pull-based diff + admin CLI for peer add/list. One-direction. No rate limiting. Tombstones propagate. | 1-2 weeks |
| **F2 (Hardening)** | Signatures enforced, rate limits, backpressure, audit log entries, replay window, peer quarantine. | 1-2 weeks |
| **F3 (Scale)** | Gossip overlay (peers pull from peers they trust, who in turn pull from *their* peers), conflict reduction via vector clocks. | Later |
| **F4 (GA)** | Multi-region replication, conflict-free merge for tags / metadata. | Later |

F1 alone gets us from "single peer" to "two peers sync the public
tier". That's enough to test the design end-to-end before committing
to the heavier F2 work.

## API surface (new endpoints)

All peer endpoints live under `/v1/peer/*` and require admin
(allowlist membership) + `X-Astor-Peer-Token`.

### `GET /v1/peer/manifest?since=<unix_ts>`

Response 200:
```json
{
  "peer_id": "alice-laptop",
  "ts": 1700000000,
  "fact_ids": [12, 13, 14],
  "manifest_sig": "ed25519:<base64>"
}
```

Response 403: peer not initialized (`am peer init` first).

### `POST /v1/peer/facts`

Request:
```json
{ "fact_ids": [12, 13] }
```

Response 200: list of fact rows, same shape as `/v1/read/multi` (only
public tier, only non-tombstoned).

Response 400: too many ids (over `max_facts_per_pull`).

### `POST /v1/peer/tombstone`

Request:
```json
{ "fact_id": 14, "reason": "duplicate of 12 after merge" }
```

Response 200: `{ "tombstoned": 14 }`.

Response 403: not admin.

## CLI surface

```
am peer init [<peer_id>]              # generate keypair if missing
am peer show                          # show peer_id + public key fingerprint
am peer add <id> <base_url> [--pubkey=...] [--readonly]
am peer list                          # show allowlist + last_seen + status
am peer remove <id>
am peer sync                          # one-shot pull from all peers
am peer sync --peer=<id>             # one-shot pull from one peer
am peer audit                          # show recent sync events from audit log
```

## Implementation Plan (F1 detail)

Files to add (in approximate order):

1. **`astor_memory/_internal/peer_identity.py`**
   - Generate / load ed25519 keypair at `~/.astor/peer_identity/`.
   - `peer_id()`, `public_key_b64()`, `sign(data)`, `verify(data, sig, pubkey)`.

2. **`astor_memory/peer/manifest.py`**
   - `compute_manifest(astor_dir) -> dict` — sorted fact_ids + ts.
   - `sign_manifest(manifest, private_key) -> str` — return base64 sig.
   - `verify_manifest(manifest, signature, public_key) -> bool`.

3. **`astor_memory/peer/sync.py`**
   - `pull_from_peer(peer_entry, astor_dir) -> SyncReport`.
   - `apply_remote_facts(facts, source_peer_id, astor_dir) -> int` —
     inserts into public bus DB with synthetic peer tags.

4. **Server endpoints** (`astor_memory/server.py`):
   - `GET /v1/peer/manifest` (admin + peer token)
   - `POST /v1/peer/facts` (admin + peer token)
   - `POST /v1/peer/tombstone` (admin)

5. **`astor_memory/cli/peer.py` + wire into `cli/main.py`**:
   - `am peer init|show|add|list|remove|sync|audit`

6. **Tests** (`tests/test_peer_sync.py`):
   - Two test peers on ports 7803 + 7805 with a shared peer allowlist.
   - Seed fact on 7803, run `am peer sync` on 7805, assert fact
     appears in 7805's public bus DB with `peer:alice-laptop` tag.

7. **CHANGELOG entry** for F1.

## Open Questions

- **Q1.** Should `am peer sync` be a one-shot CLI command or a
  background daemon (systemd / `nohup` loop)? Defer to F2.
- **Q2.** When peer B receives peer A's facts, do we also re-run the
  local LOCK rule evaluator on them, or trust the sender's tags?
  Default in F1: trust sender's tags; re-evaluate in F3.
- **Q3.** Tombstone propagation: do we propagate *deletions* of
  rule_ship'd rules? Yes — once a peer tombstones, all peers
  tombstone. No "undo over federation" in v1.
- **Q4.** What about source-tier admin facts that should be readable
  cross-peer (e.g. shared admin guidelines)? Out of scope — source
  tier never syncs. Admin can manually re-publish if needed.

## Security Considerations

- A compromised peer can sign manifests as itself; receivers verify
  the signature against the **allowlist-stored public_key**, so a
  compromised peer cannot impersonate another. To add/rotate a
  peer's key, the admin edits `peers.toml` and restarts the sync
  loop.
- A peer that consistently sends bad signatures or stale manifests
  gets **auto-quarantined** (see §6) and the admin gets a
  `WARNING` audit row.
- Private-tier data never crosses the wire. Source-tier admin facts
  also never cross (per G3). A misbehaving peer can only inject or
  hide PUBLIC content — which is the explicit trust boundary.
- Token storage: per-peer `auth_token` is stored in `peers.toml`
  with chmod 0600 on the receiving peer. Rotation is by file edit.

## Rollback

- F1 ships behind a feature flag `peer_sync_enabled = false` in
  `peers.toml` (default off). Admin opts in per-peer.
- If a sync misbehaves, admin sets `peer_sync_enabled = false` for
  that peer or runs `am peer remove <id>` to drop it.
- Sync never deletes local data (only adds + tombstones tombstoned
  facts). To undo a bad sync, admin can re-seed affected facts and
  manually tombstone the bad ones.

## Consequences

### Positive

- Multiple peers can share the public tier without a central server.
- Each peer remains the sole authority over its own private tier.
- Sync is deterministic, auditable, and offline-resilient.
- Phased rollout (F1 → F4) lets us ship value early and harden later.

### Negative

- Manual peer configuration (`peers.toml` editing). Acceptable for
  ≤ 10 peers; will need a discovery layer past that.
- LWW conflict resolution can lose data in pathological concurrent
  edit scenarios. Public tier is admin-curated so this is unlikely;
  F4 will revisit.
- Public tier becomes a shared resource across machines; admins
  must agree on curating LOCK rules carefully (a bad `rule_ship`
  rule in one peer's LOCK rules now propagates to all peers).

### Neutral

- Adds 3 new REST endpoints (`/v1/peer/*`), 1 new internal module
  family (`astor_memory/peer/`), 1 new identity module
  (`_internal/peer_identity.py`), 1 new CLI subcommand family
  (`am peer`). Total new surface: ~600 lines Python + tests.
