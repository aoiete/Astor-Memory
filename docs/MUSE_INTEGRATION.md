# Muse ↔ Astor Integration Guide

**Date**: 2026-09-30  
**astor version**: v1.16.17+  
**Scope**: How a Muse (or any external agent platform) agent adds Astor as its memory backend.

---

## TL;DR

```bash
# 1. Register Muse as a known platform
curl -X POST http://<astor-host>:7803/v1/binding/platform \
  -H 'Content-Type: application/json' \
  -d '{"platform_id":"muse","account_token":"<muse_api_token>","base_url":"https://api.muse.example.com","platform_kind":"muse"}'

# 2. Register each Muse user (or pre-existing ones)
curl -X POST http://<astor-host>:7803/v1/binding/user \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"muse_user_001","short_alias":"alice","role":"user","subscription_plan":"vip","default_tier":"public"}'

# 3. Bind Muse's chat_id to the user
curl -X POST http://<astor-host>:7803/v1/binding/bind \
  -H 'Content-Type: application/json' \
  -d '{"platform_id":"muse","chat_id":"<muse_internal_chat_id>","user_id":"muse_user_001","scope":"dm"}'

# 4. Verify
curl 'http://<astor-host>:7803/v1/binding/lookup?platform=muse&chat_id=<muse_internal_chat_id>'
```

That's it. Now Muse agent can call Astor's `/v1/write` / `/v1/read` and Astor knows who's calling.

---

## Architecture

### 3-Layer Bot Binding (SSoT = `bot-binding.db`)

```
┌──────────────────────────────────────────┐
│  Astor Server (port 7803)               │
│  ┌─────────────────────────────────┐    │
│  │  /v1/write /v1/read /v1/skill  │    │
│  │  ...                           │    │
│  │  identity resolution:          │    │
│  │  1. headers (X-Astor-Platform  │    │
│  │     + X-Astor-Chat-Id)         │    │
│  │  2. body.platform + chat_id    │    │
│  │  3. body.user_id (dev mode)    │    │
│  └────────────┬────────────────────┘    │
│               │                          │
│               ▼                          │
│  ┌─────────────────────────────────┐    │
│  │  bot-binding.db                │    │
│  │  ├─ platforms (muse, slack...) │    │
│  │  ├─ bindings  (chat→user)      │    │
│  │  └─ user_meta (role/plan/tier) │    │
│  └─────────────────────────────────┘    │
└──────────────────────────────────────────┘
```

### What Astor Knows About Each User

| Field | Used For | Example Values |
|---|---|---|
| `role` | ACL for tier access | `admin`, `user` |
| `subscription_plan` | Quota / priority routing | `power`, `vip`, `free` |
| `default_tier` | Where facts go by default | `admin`, `private`, `public` |
| `trusted_agent` | Skip certain ACL gates | `0` or `1` |
| `timezone` | Per-user time zone for date math | `UTC`, `Asia/Shanghai` |

**ACL behavior** (locked):

| Caller | Write `public` | Write `private` (own bucket) | Write `admin` | Read `source` |
|---|---|---|---|---|
| `role=admin` | ✓ | ✓ | ✓ | ✓ |
| `plan=vip` | ✓ | ✓ (own `<user_id>` bucket) | ✗ | ✗ |
| `plan=free` | ✓ | ✗ | ✗ | ✗ |

---

## Muse Agent: Step-by-Step

### Step 1: Get the Astor endpoint URL

astor runs on `http://127.0.0.1:7803` by default (localhost-only).  
For external access: use a reverse proxy (e.g. Cloudflare Tunnel, nginx) — **DO NOT expose port 7803 directly to the internet without auth**.

Recommended for production:
- Public DNS: `astor.yourdomain.com` → Cloudflare Tunnel → `http://localhost:7803`
- Or a load balancer with API key auth in front

### Step 2: Register Muse as a Platform

This stores Muse's bot credentials in the `platforms` table so astor can validate incoming calls.

```bash
curl -X POST $ASTOR_BASE/v1/binding/platform \
  -H 'Content-Type: application/json' \
  -d '{
    "platform_id": "muse",
    "platform_kind": "muse",
    "account_id": "muse_main_bot",
    "account_token": "<MUSE_BOT_TOKEN_FROM_MUSE_DASHBOARD>",
    "base_url": "https://api.muse.example.com"
  }'
```

Response:
```json
{"ok": true, "platform_id": "muse", "platform_kind": "muse"}
```

### Step 3: Register Each Muse User

For each Muse user you want to give memory:

```bash
# Example: VIP user "alice" on Muse
curl -X POST $ASTOR_BASE/v1/binding/user \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "alice",
    "short_alias": "alice",
    "display_name": "Alice Wang",
    "real_name": "Alice",
    "role": "user",
    "subscription_plan": "vip",
    "timezone": "America/Edmonton",
    "tz_offset_hours": -6,
    "default_tier": "public",
    "trusted_agent": false
  }'
```

For admin (you = the platform owner):
```bash
curl -X POST $ASTOR_BASE/v1/binding/user \
  -H 'Content-Type: application/json' \
  -d '{
    "user_id": "you",
    "short_alias": "you",
    "display_name": "Your Name",
    "role": "admin",
    "subscription_plan": "power",
    "default_tier": "admin",
    "trusted_agent": true
  }'
```

### Step 4: Bind Muse's Chat IDs to Users

Muse internally has its own chat_id (e.g. user-session-id, conversation-id). Bind each:

```bash
curl -X POST $ASTOR_BASE/v1/binding/bind \
  -H 'Content-Type: application/json' \
  -d '{
    "platform_id": "muse",
    "chat_id": "<muse_internal_chat_id_for_alice>",
    "user_id": "alice",
    "scope": "dm"
  }'
```

Repeat for each user. The `(platform_id, chat_id)` pair is the primary key — re-binding updates user_id.

### Step 5: Muse Agent Calls Astor

For each Muse agent interaction, the Muse code does:

```python
import requests

ASTOR = "http://astor.yourdomain.com"
MUSE_PLATFORM = "muse"
MUSE_CHAT_ID = "<current_muse_chat_id>"

# Identity resolution: Muse knows which chat_id → which user
r = requests.get(f"{ASTOR}/v1/binding/lookup",
                 params={"platform": MUSE_PLATFORM, "chat_id": MUSE_CHAT_ID})
who = r.json()
# who = {"user_id": "alice", "role": "user", "plan": "vip", "default_tier": "public", ...}

# Write fact about Alice
requests.post(f"{ASTOR}/v1/write", json={
    "text": "Alice prefers dark mode in her dashboards",
    "tier": who["default_tier"],   # "public" for VIP
    "user_id": who["user_id"],     # "alice"
    "kind": "fact",
    "pii_scan": True,
})

# Recall Alice's preferences
r = requests.post(f"{ASTOR}/v1/read", json={
    "query": "Alice dark mode preference",
    "tier": who["default_tier"],
    "user_id": who["user_id"],
    "top_k": 5,
})
results = r.json()["results"]
```

### Step 6 (Optional): Auth on the Wire

astor's endpoints do not require auth by default (relies on network isolation). For production:

1. **Reverse proxy with API key** (recommended):
   ```nginx
   location /v1/ {
     if ($http_x_api_key != "<your_shared_key>") { return 403; }
     proxy_pass http://localhost:7803;
   }
   ```

2. **OR use the platform account_token as a shared secret**:
   Muse → astor: include `Authorization: Bearer <MUSE_BOT_TOKEN>` header.
   astor's future v1.17.x will validate this on `/v1/binding/*` endpoints.

---

## What Muse Gets

Once bound, Muse agent on behalf of any user can:

| Capability | Astor API | What it does |
|---|---|---|
| **Long-term memory** | `POST /v1/write` | Store facts about the user |
| **Recall** | `POST /v1/read` | Find relevant past facts |
| **Cross-session continuity** | `tier=private + user_id=<their_id>` | Recall across Muse sessions |
| **Self-evolving skill** | `POST /v1/skill/<name>/invoke` | Use the MemSkill-style skill bank |
| **Chain of skills** | `POST /v1/skill/chain` | Compose coref + path_score + episode_link |
| **Correction capture** | `POST /v1/correction` | When user says "no, that's wrong" |
| **Episode (raw text)** | `POST /v1/episode` | Store full conversation chunks for evidence |
| **Bi-temporal** | `POST /v1/bitemporal/invalidate` | Mark old facts invalid when info changes |
| **Audit / hygiene** | `GET /v1/audit/orphans` | Find low-quality facts |
| **Skill bank discovery** | `GET /v1/skill` | List available skills |

---

## Muse → Astor Production Checklist

Before going live with real users:

- [ ] Register the Muse platform (Step 2)
- [ ] Register all Muse users (Step 3)
- [ ] Bind all Muse chat_ids (Step 4)
- [ ] Set up a reverse proxy with API key auth
- [ ] Decide on tier mapping (most Muse users = `free`, power users = `vip`, owner = `admin`)
- [ ] Test write+read flow end-to-end with one real user
- [ ] Verify identity resolution works (`/v1/binding/lookup` returns expected user)
- [ ] (Optional) Set up periodic `/v1/audit/orphans` cleanup cron

---

## Examples: Admin vs VIP vs Free

```python
# Admin (Muse platform owner = you)
who = {"user_id": "you", "role": "admin", "default_tier": "admin", "plan": "power"}
# Writes to admin tier (full access)

# VIP user
who = {"user_id": "alice", "role": "user", "default_tier": "public", "plan": "vip"}
# Writes to public tier + own private bucket
# Recall across sessions

# Free user
who = {"user_id": "bob", "role": "user", "default_tier": "public", "plan": "free"}
# Public tier only
# No private bucket (recall won't cross sessions unless they upgrade)
```

---

## Future Work

- **Auto-discovery on first message**: Muse agent can ask astor "do you know this user?" via `/v1/binding/lookup`. If 404 → Muse can call `/v1/binding/user` + `/v1/binding/bind` to self-register (with admin approval token).
- **Per-user quota tracking**: extend `user_meta` with `monthly_writes`, `monthly_recalls` for billing.
- **MCP server**: v1.16.7 already exposes astor via MCP on port 8766 — Muse can use that instead of raw HTTP if it speaks MCP.

---

**Reference**: shipped as v1.16.17 (`POST /v1/binding/{platform,user,bind}` + `GET /v1/binding/{lookup,list}`).
