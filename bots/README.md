# Bots / Platform Integration Directory

This directory is the **unified bot / platform integration home** for
astor-memory. It groups all data and code that pertain to multi-platform
messaging (telegram, discord, wechat, feishu, webchat, ...) under one
directory.

This README also lives at `<source_dir>bots\README.md` (the repo
copy). Both point to the same architecture.

## Files that LIVE here (active, single source of truth)

| File | Purpose | Lives at |
|---|---|---|
| `../bot-binding.db` | Bot config + tokens + chat_id→user_id bindings + user_meta | **$ASTOR_DIR/bot-binding.db** (sibling, one level up) |
| `../audit/astor_audit.db` | Append-only audit log (every private + source read/write) | **$ASTOR_DIR/audit/astor_audit.db** |
| `../audit/astor_grants.db` | Cross-user private access grants (created 2026-08-16 strict-privacy ship) | **$ASTOR_DIR/audit/astor_grants.db** |

The active files live **outside `bots/`** (one level up next to it) because
the `astor_memory._internal.bot_binding` module hard-codes
`$ASTOR_DIR/bot-binding.db` and the audit DBs hard-code `$ASTOR_DIR/audit/`.
Moving them would require code changes in 4+ files + audit/test updates.
**A directory restructure of the active files is out of scope for this commit.**

## Files inside `bots/` (this directory)

| Path | Status | Purpose |
|---|---|---|
| `archive/` | retired | Old single-platform DBs that have been consolidated into bot-binding.db. Kept on disk for forensic rollback only. Safe to delete after 60 days (2026-10-15). |
| `sessions/` | future | Reserved for cross-platform session history (wechat chat logs, telegram message archives). **Empty for now** — current session data lives in event/canonical rows of the per-user memory DBs. |
| `check/` | scripts | Health-check utility scripts (e.g. `_check_weixin_session.py`, `_check_dc.py`). These live here so they can be shared across runtime paths and don't pollute the runtime root. |
| `README.md` | docs | This file. |
| `DESIGN.md` | docs | Why astor models bots this way — 1×N×M many-to-many. |

## Why this directory exists — the design philosophy

astor-memory is built around the observation that **bot accounts and end
users are two different things, and the relationship between them is
many-to-many**. Concretely:

```
                        ┌───────────────┐
   Bot 1 (TG) ───┐       │  1 person      │
   Bot 2 (DC) ───┼───►   │  = 1 user_id   │
   Bot 3 (WX) ───┘       │  in astor      │
                        └───────────────┘
                               │
                               │  binds 0..N chat_ids
                               ▼
                        ┌───────────────┐
   chat_id A  (TG) ─┐   │  1 user       │
   chat_id B  (DC) ├─► │  may use 0..N │
   chat_id C  (WX) ┘   │  bots         │
   chat_id D  (WX)     │  concurrently │
                        └───────────────┘
```

### The four canonical cases

| Case | Person count | Bots used | Chat count | Example |
|---|---|---|---|---|
| **Solo developer** | 1 | 1 (often just Telegram for personal cron notifications) | 1 | "I run a bot just for myself" |
| **Multi-channel solo** | 1 | N (e.g. TG + DC + WX for cross-device reach) | N | "I use Discord on desktop, Telegram on the go, and WeChat for friends — same me, three bots" |
| **Bot operator + friends** | N | 1 (often WeChat) | N | "I run a single WeChat bot that 12 friends use; each chat_id binds to a different user_id" |
| **Bot service** | N | N (TG + DC for support; WX for premium tier) | M | "Customer service bot: 50 users on Telegram, 30 on Discord, 5 VIPs on WeChat, all bound to their per-user private DBs" |

### Why WeChat is different (1:1 chat ↔ user)

WeChat's **`im.bot` protocol is per-direct-message**: the bot can only be
in **one** conversation with one user at a time (in DMs). This means **1
WeChat bot instance maps 1:1 to a single user** in practice — even if 100
people DM the bot, each chat is independent and gets its own `chat_id`
that binds to that user's `user_id`.

Telegram and Discord are the opposite: a single bot **can serve all DMs
in parallel** and address them by `chat_id`. So one Telegram bot maps
**1:N (one bot to many users)**.

This is why astor's `bot-binding.db` schema has TWO distinct levels:

```sql
-- Per-bot configuration (token + URL + enabled flag)
platforms (platform_id, platform_kind, account_id, account_token, base_url, enabled)

-- Per-chat binding (which chat_id talks to which user)
bindings (binding_id, platform_id, chat_id, user_id, scope, active, ...)
```

- A **Telegram bot** has 1 row in `platforms` + many rows in `bindings` (one per DM/group).
- A **WeChat bot** has 1 row in `platforms` + typically 1 row in `bindings` per active user.
- A **Discord bot** is similar to Telegram (1:N).

### How ACL and tier-isolation follow from this

Once a `chat_id` is bound to a `user_id`, astor's **3-tier isolation**
takes over:

- `public` tier — shared knowledge (skills, agent rules, project notes)
- `source` tier — admin-private (first_admin only; agent sees it, users don't)
- `private_<user_id>` tier — **per-user private DB** that ONLY that user's `chat_id`s can read/write

The bot, **as a process**, has no special privilege over private data.
When a Telegram DM comes in from chat_id `C` bound to user_id `alice`:

1. astor resolves `C → alice` via `bot-binding.db`
2. astor binds ACL: actor=`user:alice`, role=`user`, tier=`private_alice`
3. The bot's process executes `/v1/read` on tier=`private`, user_id=`alice`
4. acl_check_read passes because `user_id == ctx.user_id`
5. Recall returns alice's private facts

If a DIFFERENT chat_id `D` bound to `bob` then sends a read request for
alice's private:

1. astor resolves `D → bob`
2. ACL binds as `actor=user:bob, tier=private_alice`
3. acl_check_read for tier=`private`, user_id=`alice` **fails** because `user_id != ctx.user_id`
4. Returns 401 — "user grant required (strict privacy model 2026-08-16)"

**The bot doesn't see alice's private when bob asks.** That's the whole
point of the binding layer + the strict-privacy ship.

### Why one bot can serve many users without mixing data

A naive single-bot service would have one big memory pool. A user `alice`
writes a fact "I have a meeting tomorrow"; user `bob`'s read might
accidentally surface that fact. Multi-user mode + per-user private tier
prevents this:

- `public/memory/astor_bus_public.db` — shared facts (skills, rules)
- `users/alice/memory/astor_bus_alice.db` — only alice's chat_ids can read
- `users/bob/memory/astor_bus_bob.db` — only bob's chat_ids can read

The bot is a thin transport layer; the **memory DB owns the isolation**.
You can move the bot, replace the bot, fork the bot — the memory
isolation persists in the SQLite files.

## Files inside `bots/` (this directory)

| Path | Status | Purpose |
|---|---|---|
| `archive/` | retired | Old single-platform DBs that have been consolidated into bot-binding.db. Kept on disk for forensic rollback only. Safe to delete after 60 days (2026-10-15). |
| `sessions/` | future | Reserved for cross-platform session history (wechat chat logs, telegram message archives). **Empty for now** — current session data lives in event/canonical rows of the per-user memory DBs. |
| `check/` | scripts | Health-check utility scripts (e.g. `_check_weixin_session.py`, `_check_dc.py`). These live here so they can be shared across runtime paths and don't pollute the runtime root. |
| `README.md` | docs | This file. |
| `DESIGN.md` | docs | Why astor models bots this way — 1×N×M many-to-many. |

## Migration history

### 2026-08-16 — unified bot-binding.db created

Previously, each platform had its own DB file:
- `D:/AI/users/_system/wechat_bots.db` (5 wechat bots)
- (telegram / discord / feishu were tracked in `install-state.json`)

All of these were consolidated into **`$ASTOR_DIR/bot-binding.db`** with 4 tables:
- `platforms` (per-bot config + token + base_url + enabled flag)
- `bindings` (chat_id → user_id, with scope + role_inherit + allowed-from)
- `user_meta` (human-readable info per user)
- `_schema_version` (migration tracking)

The README's docstring at the top of `astor_memory/_internal/bot_binding.py`:
> *Bot binding DB API — replaces install-state.json platform_bindings + wechat_bots.db.*

### 2026-08-16 — bots/ directory created at runtime root

After consolidating into bot-binding.db, user asked: "其他对应程序文件
是不是可以统一到一个目录 包括其他平台不单单微信". This `bots/` directory
is the answer. Stale data gets `archive/`, future growth (sessions,
more checks) gets its own subdir, active code/data stays at `$ASTOR_DIR/`
for backward compat.

### 2026-08-16 — strict-privacy ship

`audit/astor_grants.db` added (separate file, mode 0600). Grants table
tracks `grantor` (data owner) → `grantee` (first_admin / admin:<id> /
user:<id>) with `scope` (read / write / admin) and `expires_at`.
Revoked grants fail immediately. Cross-user private access requires an
explicit grant (no more implicit admin override).

## Layout diagram

```
$ASTOR_DIR/                                  = <runtime_dir>
├── bot-binding.db                            ← unified, single source
├── audit/                                    ← single source
│   ├── astor_audit.db
│   └── astor_grants.db
├── public/, source/, repos/, users/          ← 4-tier memory
├── lex/                                      ← BM25 keyword index
├── logs/, scripts/
└── bots/                                     ← THIS directory
    ├── README.md                             ← you are here
    ├── DESIGN.md                             ← design philosophy
    ├── archive/
    │   ├── README.md
    │   └── wechat_bots.db.archived_2026-08-16
    ├── sessions/                             ← empty, future
    └── check/                                ← future
```

## Future work

- Move `_check_weixin_session.py`, `_check_dc.py`, `_db_scan.py` from
  `$ASTOR_DIR/_check_*.py` to `$ASTOR_DIR/bots/check/` (next cleanup).
- Populate `sessions/` if/when wechat session history becomes a thing.