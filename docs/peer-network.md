# Astor Peer Network — Architecture & Status

**Status (v1.15.23, 2026-09-28)**: Phases 1-5 + PPS (peer public search) +
dashboard panel + per-peer rate limit + trust auto-update all shipped.
This document is the canonical reference for the peer subsystem.

---

## Opening thesis

> **One person's memory is finite. The collective memory of all astor peers grows without bound.**

Each peer ships (a) identity — `peer_id` + `ed25519` keypair; (b) personal
trust decisions — who to trust, who to blacklist; (c) trust-weighted
signals — rekey notifications, fact provenance, search results, sync
frequency. Combined, peers form a distributed long-term memory that no
single node could hold alone. Astor is not a backup system.
**Astor is a memory system whose ceiling grows with every mind that joins.**

This document covers everything that's shipped today (v1.15.23) plus
the threat model and operational recipes. Future phases will extend
this file.

---

## What's shipped (v1.15.23)

| Phase | Ships | Status |
|---|---|---|
| **Phase 1** | peer_id + ed25519 keypair + status CLI | Shipped v1.14.67 |
| **Phase 2** | peer_relationships table (friend/blacklist/whitelist/pending), `am peer friend add/list/remove`, social_graph export/import | Shipped v1.14.68 |
| **Phase 3** | Signed rekey notification (auto on peer_id change), gossip protocol | Shipped v1.14.72 |
| **Phase 4** | topic_index per peer, topic-aware routing in `/v1/read` | Shipped v1.14.70 |
| **Phase 5** | topic-aware hybrid recall + topic_boost | Shipped v1.14.71 |
| **PPS wire format** | `GET /v1/peer/public_search` — signed ed25519 requests | Shipped v1.15.18 |
| **PPS REST CRUD** | `/v1/peer/{list, add, <pid>/{trust, allow-search, blacklist, unblacklist}}, DELETE <pid>` | Shipped v1.15.19 |
| **PPS dashboard** | "Peer Friends" panel with inline trust + allow toggle + block + delete | Shipped v1.15.19 |
| **PPS bugfix** | `to_dict()` on `PeerSearchResult` (v1.15.19 → v1.15.20) | Shipped v1.15.20 |
| **PPS auto-recall** | `/v1/peer/recall` (fans out to friends when local empty) | Shipped v1.15.21 |
| **PPS rate limit** | 1000 req/24h per peer (env `ASTOR_PEER_RATE_LIMIT_PER_24H`) | Shipped v1.15.22 |
| **Trust auto-update** | +1 per successful adopt; -5 decay sweep at 30d | Shipped v1.15.23 |

Five distinct surfaces (Phase 1-5) plus six PPS layers. New code is
**gated**, **opt-in**, and **rate-limited** — see the threat model
section below.

---

## Identity model

```
peer_id  = astor:<sha256(DB_path + first_run_ts)[:32]>
keypair  = ed25519 (signing only, not identity anchor)
```

### Why DB-bound peer_id (not key-bound)

| Approach | Pros | Cons |
|---|---|---|
| sha256(public_key) | Unforgeable | Reinstall = new ID = lose all friend relationships |
| sha256(DB_path + ts) | Reinstall with same DB keeps ID | Anyone with DB can fake peer_id |
| **Our choice: sha256(DB_path + ts)** | DB is the canonical asset. Key loss doesn't lose identity. | Need separate signing for authenticity |

The DB is the real memory. Keys are signing tools. If your DB
survives, your peer_id survives. If you lose your key, you can
re-generate one and broadcast a **rekey notification** (Phase 3) to
friends.

### Files on disk

```
$ASTOR_DIR/identity/
├── peer_id           # "astor:<32-hex>" (38 bytes)
├── first_run_ts      # ISO 8601 (20 bytes)
├── keypair.json      # {private_key, public_key, generated_at} (177 bytes)
└── relationships.db  # SQLite: peer_relationships, topic_index, rekey_log
```

Permissions:
- Unix: `chmod 600` on `keypair.json` (signing key is sensitive)
- Windows: `icacls` to remove inheritance and grant only current user

### Rekey flow (shipped)

When `peer_id` changes (DB moved, fresh install, ts drift):

```
[admin's astor detects peer_id change]
    ↓
[signed REKEY message: old_peer_id, new_peer_id, signature]
    ↓
[friend peer receives REKEY via POST /v1/peer/recv]
    ↓
[verify signature with admin's old public key]
    ↓
[decision by trust level]
  trust >= 70 → auto-accept: replace peer_id, KEEP trust score
  trust 30-70 → pending: notify admin for manual confirm
  trust < 30  → reject: ignore, no change
```

This avoids the "new install = transparent" problem. Rekey lets
friends verify "this is the same admin, just a new install" and
preserve their trust relationship.

---

## PPS — Peer Public Search (Phases 4+)

PPS is the demand-driven search protocol. Peers do not push facts;
they sign requests to query other peers' **PUBLIC** tier. All
operations are **opt-in** (default OFF) and **rate-limited**.

### Wire format (PPS)

```http
GET /v1/peer/public_search?req=<urlsafe-b64 json> HTTP/1.1
```

Where `req` decodes to:
```json
{
  "requestor_peer_id": "astor:<32-hex>",
  "requestor_pubkey":  "<base64 ed25519 verify key, 32 bytes>",
  "query":            "<1..1024 bytes>",
  "topic":            "<optional, 1..128 bytes>",
  "limit":            1..20,
  "timestamp":        "2026-09-28T19:00:00Z",
  "signature":        "<base64 ed25519 sig over canonical_payload, 64 bytes>"
}
```

The signature is over `requestor_peer_id \n query \n topic \n limit \n
timestamp` (joined with `\n`). Construct-time validation rejects
malformed requests before any network round-trip.

Response:
```json
{
  "requestor_peer_id": "astor:<32-hex>",
  "results": [
    {
      "source_peer_id": "astor:<32-hex>",   // who said it
      "source_trust":   0..100,             // the requestor's recorded trust
      "fact_id":         <int>,              // friend's local fact_id
      "content":         "<=2048 bytes>",
      "kind":            "rule|fact|...",
      "tags":            ["..."],
      "created_at":      "ISO 8601",
      "relevance":       0.0..1.0
    },
    ...
  ],
  "truncated":  false,  // true if `limit` capped the response
  "error":     null
}
```

### PPS server-side gates (in order)

1. **Signature verification** — ed25519 over canonical payload with
   `requestor_pubkey`. Failure: 403 `bad_signature`, audit-logged.
2. **Self-search guard** — a peer cannot search itself; 400
   `self_search_not_allowed`.
3. **Per-peer rate limit** (Ship E) — 1000 req/24h sliding window,
   enforced **before** signature verify in some implementations
   (saves CPU) or right after (more accurate to the requestor).
   Failure: 429 with `Retry-After` header + JSON
   `count_in_window` + `retry_after_seconds`.
4. **Receiving chain**:
   - `unknown_peer` (403) — friend has not added us
   - `peer_blacklisted` (403) — we are in their blacklist
   - `trust_below_threshold` (403) — their trust in us < 50
   - `search_not_allowed` (403) — they haven't opted in

### PPS attack model (all attacks preventive, not reactive)

| Attack | Mitigation |
|---|---|
| Spam (push) | No push path exists. Peers cannot write to us. |
| Poison (eclipse) | We only query our own `trust>=50` friends; results are source-marked so we know who said what. |
| Replay (old request) | Timestamp window: `now-7d < ts < now+60s`. Past or future requests are rejected at construction. |
| DoS (slow peer) | Per-peer `urllib` timeout (3s). Total budget 10s. Slow peer skipped automatically. |
| Hostile unknown peer | Target list filtered at construction. The HTTP layer never sees unknown peer_ids. |
| Self-search confusion | Endpoint refuses peer_id == our own. |
| Abuse via opt-in | Opt-in is default OFF. Reverse direction: friend can `am peer allow-search` us at any time. |

### PPS operations surface

| Operation | REST | CLI |
|---|---|---|
| List friends (with trust + allow_search flag) | `GET /v1/peer/list?kind=&min_trust=` | `am peer friend list` |
| Add friend (trust, endpoint, pubkey, alias) | `POST /v1/peer/add` | `am peer friend add` |
| Update trust | `POST /v1/peer/<pid>/trust` | `am peer friend update-trust` |
| Opt in: allow my friends to search my public tier | `POST /v1/peer/<pid>/allow-search` | `am peer allow-search` |
| Revoke opt-in | `POST /v1/peer/<pid>/allow-search {allow:false}` | `am peer unallow-search` |
| Blacklist | `POST /v1/peer/<pid>/blacklist` | `am peer blacklist` |
| Restore from blacklist | `POST /v1/peer/<pid>/unblacklist` | `am peer friend add` (re-add) |
| Remove friend entirely | `DELETE /v1/peer/<pid>` | `am peer friend remove` |
| Search my friends' public tiers | `GET /v1/peer/search?q=&topic=&limit=` | `am peer search` |
| **Auto-recall**: local empty → fan out | `GET /v1/peer/recall?q=&tier=&user=` | (read path) |
| Adopt: copy a peer's fact into my local DB | `POST /v1/peer/adopt {source_peer_id, facts:[{content,...}]}` | (write path) |
| Per-peer rate limit status | `GET /v1/peer/rate-limit?peer_id=...&all=true` | `am peer rl status` |
| Reset a peer's rate-limit bucket | `POST /v1/peer/rate-limit/reset {peer_id}` | `am peer rl reset` |
| Rebuild buckets from audit log | `POST /v1/peer/rate-limit/rebuild` | `am peer rl rebuild` |

### Per-peer rate limit (Ship E) — design

```text
Default cap: 1000 requests per 24 hours per peer
Override:    ASTOR_PEER_RATE_LIMIT_PER_24H env var (int)
Storage:     in-memory dict keyed by peer_id, sliding window
             of [timestamp, ...] (lazy-evicted on insert).
Persistence: server-start hook calls `rebuild_from_audit()` which
             reads last-24h of audit rows (action='peer_search',
             actor starting 'peer:') and rebuilds the in-memory
             buckets. After restart, the cap resumes smoothly.
```

Why per-peer and not per-actor? R12593: PPS is server-to-server sync.
A single-agent client (Claude Code, Codex, custom script) and a
peer-fanout are very different traffic patterns. Per-actor limits
on a fanout would throttle the very use case the system is designed
for. The PPS path is its own budget, separate from `/v1/read`
(which has its own per-actor rate limit for the single-agent case).

### Manual adopt (PPS Path)

`POST /v1/peer/adopt` lets the operator **explicitly** copy facts
from a peer's response into the local DB. Each adopted fact carries
metadata:

```json
{
  "adopted_via": "peer:search",
  "adopted_at": "2026-09-28T19:30:00Z",
  "source_peer_id": "astor:<32-hex>",
  "original_fact_id": 42,
  "source_kind": "fact"
}
```

Trust bumps +1 for the source peer on each successful adopt
(operator signal: "this peer gave me something useful"). The bump
is **explicit-operator-action only** — no auto-paths trigger it. This
defends against a hostile friend returning many irrelevant facts to
trick the system into raising their own trust.

### Auto-recall (`/v1/peer/recall`)

Endpoint that runs local recall first; if empty, fans out to all
eligible friends (trust>=50, endpoint set, allow_search=True) and
returns the combined result with provenance. Per R12593-b: the
agent's default `/v1/read` does NOT auto-fire peer. The
auto-recall path is **explicit**: callers opt in by hitting
`/v1/peer/recall` instead of `/v1/read`.

Response shape:
```json
{
  "mode": "local_only" | "peer_fanout",
  "local_count": 0..20,
  "peer_count": 0..20,
  "results": [<result>],   // primary list, local first if any
  "peer_results": [...],   // alias for clarity in peer_fanout mode
  "per_peer": [{peer_id, alias, error, count, truncated}, ...],
  "local_error": null      // honest fallback if local recall hit an exception
}
```

---

## Trust model

| Trust range | Effect on PPS path | Effect on incoming facts |
|---|---|---|
| 0 | Blacklisted (no PPS, no fact acceptance) | — |
| 1-29 | Incoming facts quarantined (not auto-accepted) | — |
| 30-49 | Friend can be queried for PPS (with permission) but their results carry `source_trust < 50` | Incoming facts quarantined |
| 50-69 | Friend is "trusted enough" for PPS auto-recall | Incoming facts accepted |
| 70-100 | Friend is "fully trusted" — rekey auto-accepted | Incoming facts accepted |
| New peer | trust=30 default | Quarantined until proven |

`/v1/peer/public_search` server-side gate requires **our trust in
them >= 50**. This is the explicit minimum for "trustworthy enough
to search". The lower `allow_search=True` flag is receiver-side opt-in,
separate from this.

### Trust auto-update (Ship F)

| Trigger | Effect |
|---|---|
| `/v1/peer/adopt` with `count >= 1` | `+1` trust for the source peer (clamp 0..100) |
| 5+ consecutive 4xx/5xx from same peer | `-3` trust (caller-driven, manual) |
| 30+ days with no contact | `-5` trust on next decay sweep, clamped at default=30 |

Decay is **admin-initiated only** (no auto-cron) to keep the trust
surface auditable:
```bash
am peer trust decay  # sweep stale peers
am peer trust show <peer_id>  # display state + age + error count
am peer trust bump <peer_id> <delta>  # manual override
```

---

## Dashboard panel (Ship C)

The operator-facing dashboard at `/` (served from
`/astor_memory/dashboard/index.html`) includes a **Peer Friends**
card with:
- Friend list grouped by kind (friends / blacklisted / other)
- Inline trust editing (input field, change saves on blur, success flash)
- One-click `allow_search` toggle (ON / off with color)
- Block / Unblock / Delete action buttons
- Add-row at top (peer_id, alias, trust input)

Auto-polls every 60s alongside the rest of the dashboard. The panel
is fully driven by the REST endpoints — no separate dashboard API.

---

## Operational recipes

### Recipe 1: bootstrap a 2-peer friendship

```bash
# Peer A (admin) - host1:7803
am peer status  # get peer_id_A + public_key_A

# Peer B (alice) - host2:7803
am peer status  # get peer_id_B + public_key_B

# On A:
am peer friend add <peer_id_B> \
  --trust 80 \
  --endpoint https://host2:7803 \
  --pubkey <public_key_B>

# On B:
am peer friend add <peer_id_A> \
  --trust 80 \
  --endpoint https://host1:7803 \
  --pubkey <public_key_A>

# B opts in to A's searches (one-way):
am peer allow-search <peer_id_A>

# A searches B's public tier (CLI):
am peer search "R132 forbids fabrication"

# A searches via dashboard: open /, scroll to Peer Friends card.
```

### Recipe 2: integrate PPS into a custom agent

```python
import urllib.request, json, base64
from astor_memory import astor_identity, sign

# 1. Build a signed request
req = build_search_request(
    query="R132 fabrication",
    requestor_peer_id=astor_identity.peer_id,
    requestor_pubkey=astor_identity.public_key,
    requestor_private_key=astor_identity.private_key,
    limit=5,
)
req_b64 = base64.urlsafe_b64encode(json.dumps({
    "requestor_peer_id": req.requestor_peer_id,
    "requestor_pubkey":  req.requestor_pubkey,
    "query":             req.query,
    "topic":             "",
    "limit":             req.limit,
    "timestamp":         req.timestamp,
    "signature":         req.signature,
}, separators=(",", ":")).encode())

# 2. GET /v1/peer/public_search
url = f"https://host2:7803/v1/peer/public_search?req={urllib.parse.quote(req_b64)}"
with urllib.request.urlopen(url, timeout=10) as r:
    response = json.loads(r.read())

# 3. Adopt what you want
for r in response.get("results", []):
    if "fabrication" in r["content"]:
        urllib.request.urlopen(urllib.request.Request(
            "https://host1:7803/v1/peer/adopt",
            data=json.dumps({
                "source_peer_id": req.requestor_peer_id,  # NOTE: source = the responding peer
                "tier": "source",
                "facts": [{"content": r["content"], "kind": r["kind"]}]
            }).encode(),
            headers={"Content-Type": "application/json"},
        ), timeout=10)
```

### Recipe 3: monitor PPS rate limit

```bash
am peer rl status  # all peers with non-empty buckets
am peer rl status astor:<32-hex>  # one peer
am peer rl reset astor:<32-hex>  # admin escape: clear their bucket
am peer rl rebuild  # re-derive from last-24h audit (post-restart recovery)
```

REST equivalent:
```bash
curl 'http://127.0.0.1:7803/v1/peer/rate-limit?all=true'
curl -X POST 'http://127.0.0.1:7803/v1/peer/rate-limit/reset' \
  -H 'Content-Type: application/json' \
  -d '{"peer_id": "astor:..."}'
```

### Recipe 4: trust decay sweep (cron / scheduled)

```bash
# Add to weekly cron: 0 4 * * 0 /d/AI/scripts/peer_decay.sh
# peer_decay.sh:
#   #!/bin/bash
#   export ASTOR_DIR=/d/AI/Astor-Memory-Runtime
#   D:/AI/PY-311/Scripts/python.exe -m astor_memory.cli.main peer trust decay
# Result: prints 'decayed N peer(s)'. Stale friends (>30d, trust>30) get -5.
```

---

## Anti-spam and abuse (proven in practice)

The PPS path was designed specifically to resist the following attack
classes (each is a **preventive** control, not a detective one):

| Attack | Control |
|---|---|
| Spam push | No push route exists |
| Poison (eclipse) | Trust-gated; results source-marked |
| Replay | 7-day timestamp window |
| DoS (slow peer) | Per-peer urllib timeout (3s) |
| Hostile unknown peer | Receiver-side trust+allow gate |
| Self-search confusion | 400 self_search_not_allowed |
| Trust inflation | Auto-bump is operator-action-only (adopt); not on read/search |
| Trust deflation (hostile search returns) | Auto-bump is +1 per adopt, NOT per query; hostile queries don't lower trust |
| Adoption flood | trust 0-100 clamp; 1000 req/24h per-peer cap |

---

## Compatibility

- All PPS endpoints require `astor-memory` v1.15.18 or later on both
  requestor and responder sides.
- Backward compatibility: `/v1/peer/public_search` is the only PPS
  endpoint that requires signature; all the others (list, add, etc.)
  run with the server's admin identity.
- Forward compatibility: when a new PPS layer ships, the wire format
  version is tracked in `astor_memory/__version__` and reflected in
  `CHANGELOG.md` head.

---

## Files added or changed by PPS

```
astor_memory/_internal/
├── peer_identity.py       # Phase 1
├── peer_relationships.py  # Phase 2 + Phase 4 (topic_index) + Ship F (trust helpers)
├── peer_search.py         # PPS wire format
└── peer_rate_limit.py     # Ship E: per-peer rate limit
astor_memory/server.py     # PPS endpoints + audit logger
astor_memory/cli/main.py   # `am peer` subcommand
astor_memory/dashboard/    # Phase 4 dashboard panel (Ship C)
tests/                     # 87+ tests across test_peer_*.py
```

---

## Related

- `docs/api.md` — full REST surface reference
- `docs/architecture.md` — system-wide architecture
- `docs/fact-lifecycle.md` — how fact decay + tombstoning works
- `CHANGELOG.md` — version history; v1.15.18 through v1.15.23 are PPS ships
- GitHub: github.com/<repo-owner>/Astor-Memory
