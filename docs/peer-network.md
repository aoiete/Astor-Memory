# Astor Peer Network — Architecture & Status

**Status (v1.14.68, 2026-09-17)**: Phase 1 + Phase 2 shipped (identity + friend
list + rekey notification). Phases 3-5 are planned but not yet implemented.

---

## Opening thesis

> **One person's memory is finite. The collective memory of all astor peers grows without bound.**

Each peer ships (a) identity — `peer_id` + `ed25519` keypair; (b) personal trust
decisions — who to trust, who to blacklist; (c) trust-weighted signals —
rekey notifications, fact provenance, sync frequency. Combined, peers form a
distributed long-term memory that no single node could hold alone. Astor is
not a backup system. **Astor is a memory system whose ceiling grows with
every mind that joins.**

Phases 1-2 lay the foundation. Phases 3-5 wire peers together over the wire.

---

## Why a peer network

Astor is a memory system. Its first value is **local recall** — your own DB is
authoritative for your own memories. But memory is more useful when peers can
share patterns, methods, and reference facts:

- "I wrote a note about X" → my friend sees the note (with provenance)
- "How do I configure Y?" → public broadcast finds peers who know
- "What's a good pattern for Z?" → recall across trusted peers

Phase 1 (this release) lays the foundation: every astor install gets a
**peer identity** so future phases can attach relationships and route queries.

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

The DB is the real memory. Keys are signing tools. If your DB survives,
your peer_id survives (same path + ts). If you lose your key, you can
re-generate one and broadcast a **rekey notification** (Phase 3) to friends.

### Rekey flow (planned Phase 3)

When peer_id changes (DB moved, fresh install, ts drift):

```
[admin's astor detects peer_id change]
    ↓
[signed REKEY message: old_peer_id, new_peer_id, signature]
    ↓
[friend peer receives REKEY]
    ↓
[verify signature with admin's old public key]
    ↓
[decision by trust level]
  trust >= 70 → auto-accept: replace peer_id, KEEP trust score
  trust 30-70 → pending: notify admin for manual confirm
  trust < 30 → reject: ignore, no change
```

This avoids the "new install = transparent" problem (fact 12611 default
trust=30 means new peer has to re-earn trust from scratch). Rekey lets
friends verify "this is the same admin, just a new install" and preserve
their trust relationship.

## Files on disk

```
$ASTOR_DIR/identity/
├── peer_id           # "astor:ea1c7c31..." (38 bytes)
├── first_run_ts      # ISO 8601 (20 bytes)
└── keypair.json      # {private_key, public_key, generated_at} (177 bytes)
```

Permissions:
- Unix: chmod 600 on keypair.json (signing key is sensitive)
- Windows: `icacls` to remove inheritance and grant only current user

## Permissions on recall_log.jsonl (v1.14.67 fix)

User query history contains private text — PII risk if world-readable. Fixed
by:

```python
# server.py recall-log write path:
os.chmod(_log_path, stat.S_IRUSR | stat.S_IWUSR)  # 600
```

On Windows, `chmod` is a no-op. Run once:

```powershell
icacls "D:\AI\Astor-Memory-Runtime\astor\metrics\recall_log.jsonl" ^
  /inheritance:r /grant:r "SYSTEM:(R,W)" "%USERNAME%:(R,W)" "Administrators:(R,W)"
```

## CLI

```bash
am peer status                  # show peer_id + pub key + DB path
am peer status --reveal-private  # also show private key (DANGEROUS)
am peer init                    # initialize if not present (idempotent)
am peer sign <payload>          # sign payload with private key
am peer verify <payload> <sig> <pubkey>   # verify signature
```

## Architecture phases (planned)

| Phase | Ships | Status |
|---|---|---|
| **Phase 1** | peer_id + keypair + status CLI | **Shipped v1.14.67** |
| Phase 2 | peer_relationships table (friend/blacklist/whitelist), `am peer friend add/list/remove`, social_graph export/import | Planned |
| Phase 3 | Signed rekey notification (auto on peer_id change), gossip protocol | Planned |
| Phase 4 | topic_index per peer, topic-aware routing in /v1/read | Planned |
| Phase 5 | consensus verification (3+ peer agree = verified), anti-spam | Planned |

## Trust model (from fact 12610/12611)

- New peer defaults to trust=30 (low)
- trust < 50: incoming facts quarantined (not auto-accepted)
- trust >= 50: incoming facts accepted
- Rekey notification: verified admin → trust preserved (not reset to 30)

## What Phase 1 does NOT ship (intentional)

- **No friend list** — `am peer friend add` not implemented yet
- **No gossip** — peer doesn't broadcast to network yet
- **No rekey notification** — handled by manual `am peer init` + friends manually re-add
- **No public sync** — sync infrastructure comes in Phase 3+
- **No trust score** — hardcoded trust=30 default for everyone (Phase 2+)
- **No anti-spoof** — keypair exists but not yet used to sign/verify facts

This is **foundation only**. The full network ships over multiple future
versions. Phase 1 just makes peer identity real.

## Related facts

- fact 3248: peer_id = sha256(DB_path + first_run_ts) design rationale
- fact 3249: social_graph (friend/trust) is portable, separate from peer_id
- fact 3250: rekey notification design (Phase 3)
- fact 12609: BT-like gossip design (default on)
- fact 12610: trust layering (new peer trust=30, quarantine below 50)
- fact 12611: peer network default on, dashboard visibility

## Permissions reference

| File | Mode (Unix) | ACL (Windows) |
|---|---|---|
| `identity/peer_id` | 644 (public) | read-all OK, no secret |
| `identity/first_run_ts` | 644 (public) | read-all OK |
| `identity/keypair.json` | 600 (private) | current user only |
| `astor/metrics/recall_log.jsonl` | 600 (private) | current user only |
