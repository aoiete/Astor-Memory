# Why astor-memory models bots this way

This document explains the **design philosophy** behind the bots/
platform integration layer. It's the answer to a question users keep
asking: "I have 1 bot on Telegram and my friend wants to use the same
bot — does astor handle that?" or "I want my Discord bot and my WeChat
bot to share memory for the same person — does astor support that?"

## TL;DR

**Yes to all of the above.** astor treats **people** (user_id) and **bots**
(platform_id) as two independent dimensions. The relationship is many-to-many:

```
    persons × bots × chat_ids = full reality

    1 person  ──► can have 1..N bots (different platforms)
    1 bot     ──► can serve 1..N persons (via separate chat_ids)
    1 person  ──► can have 1..N chat_ids per bot (DMs, groups, threads)
```

This is why astor has TWO separate tables in `bot-binding.db`:

- `platforms` — per-bot config (token + URL + enabled)
- `bindings` — per-chat-id → user_id mapping

Not one combined table, because the relationships are genuinely
independent.

## The four canonical scenarios

### Scenario 1: Solo developer, one bot

```
┌─────────┐         ┌─────────────────┐
│   me    │◄───────►│ TG bot          │
│ (admin) │  1:1    │ (1 chat_id = me) │
└─────────┘         └─────────────────┘
```

This is the **default astor install**. One user, one bot, one chat.
Memory is `public + private_admin`. Multi-user mode is off (`am bot off`
is implicit on a fresh install).

### Scenario 2: Solo developer, multiple bots across platforms

```
            ┌─────────┐
            │   me    │
            │ (admin) │
            └────┬────┘
                 │  1:N
        ┌────────┼────────┐
        ▼        ▼        ▼
   ┌────────┐ ┌────────┐ ┌────────┐
   │ TG bot │ │ DC bot │ │ WX bot │
   └────────┘ └────────┘ └────────┘
   chat_id_1  chat_id_2  chat_id_3
       ▲          ▲          ▲
       │          │          │
       └──── all bind to user_id=admin ────┘
```

Same person, three bots on three platforms. **Each bot's chat_id binds to
the same `user_id`**. Memory is shared across bots — write via Telegram,
recall from Discord, both see the same private tier.

This is how the astor author uses it personally: TG on phone, DC on
desktop, WeChat for friends — same `user_id=admin`, three `bindings` rows,
one `private_admin.db`.

### Scenario 3: Bot operator, multiple users, one bot

```
        ┌─────────────────────┐
        │  WX bot             │
        │  (im.bot protocol)  │
        │  account: abc123    │
        └─────────┬───────────┘
                  │  1:N (each chat_id = one DM = one user)
        ┌─────────┼─────────┬─────────┐
        ▼         ▼         ▼         ▼
    chat_id_A  chat_id_B  chat_id_C  chat_id_D
        │         │         │         │
        ▼         ▼         ▼         ▼
    alice      bob        charlie   dan
```

This is the **service bot** pattern. One bot, many users. The WeChat
platform is special: its `im.bot` protocol only allows 1:1 DMs, so
**one bot instance essentially serves N users via separate DM chats**.

All users share `public/` (skills, agent instructions) and have their
own `private_<user>.db`. The bot's runtime reads `chat_id → user_id` from
`bot-binding.db` to know whose private DB to query.

### Scenario 4: Multi-platform service bot (N users, M platforms)

```
   alice uses TG  + DC  (not WX)            ─┐
   bob uses TG  + WX  (not DC)              ─┤  All same bot
   charlie uses DC + WX (not TG)            ─┤  operator
                                              ┘
```

```
┌──────────────┐
│ TG bot       │◄─── alice, bob
├──────────────┤
│ DC bot       │◄─── alice, charlie
├──────────────┤
│ WX bot       │◄─── bob, charlie
└──────────────┘
```

Each user has access to whichever platform they prefer. Memory is still
per-user (`private_alice.db`, `private_bob.db`, `private_charlie.db`).
The agent can switch platforms mid-conversation and remember everything
because the **user_id is the stable anchor**.

## Why two tables?

```sql
CREATE TABLE platforms (
    platform_id    TEXT PRIMARY KEY,    -- 'telegram:bot1'
    platform_kind   TEXT,                -- 'telegram' | 'discord' | 'weixin' | ...
    account_id     TEXT,                -- bot-specific account id
    account_token  TEXT,                -- secret
    base_url       TEXT,                -- api base url
    enabled        INTEGER DEFAULT 1,
    notes          TEXT,
    created_at     TEXT,
    updated_at     TEXT,
    source         TEXT                  -- where the bot came from
);

CREATE TABLE bindings (
    binding_id     TEXT PRIMARY KEY,    -- uuid
    platform_id    TEXT NOT NULL,        -- FK to platforms
    chat_id        TEXT NOT NULL,        -- platform-specific chat_id
    user_id        TEXT NOT NULL,        -- astor user_id this chat binds to
    scope          TEXT DEFAULT 'dm',
    role_inherit   TEXT DEFAULT 'user',
    allow_from     TEXT,                  -- optional security allow_from
    active         INTEGER DEFAULT 1,
    bound_at       TEXT,
    revoked_at     TEXT,
    revoked_by     TEXT,
    bound_by       TEXT,
    notes          TEXT
);
```

Because:
- **platforms** describes the BOT (its token, its lifecycle, whether it's enabled)
- **bindings** describes the CHAT (who's talking to whom, which user they map to)

If we merged them, we'd have N copies of the same bot token for N chats
of the same bot. That's wasteful AND a security hazard (token leaks
breach multiply).

## How this maps to astor's 3-tier isolation

Once a `chat_id → user_id` binding is established, **the bot has no
special privilege over the user's private data**. The bot is just a
transport:

```
Telegram DM from chat_id C (bound to user_id=alice)
      │
      ▼
astor runtime:
    1. resolve(C) → alice (via bot-binding.db)
    2. astor_init_acl(actor='user:alice', role='user', tier='private_alice')
    3. handle /v1/read or /v1/write
    4. acl_check_read(tier='private', user_id='alice') ✓ pass
    5. read/write alice's private DB
```

If Telegram DM comes from chat_id D (bound to user_id=bob):

```
    1. resolve(D) → bob
    2. astor_init_acl(actor='user:bob', role='user', tier='private_alice')  ← MISMATCH
    3. acl_check_read(tier='private', user_id='alice')  ← user_id != ctx.user_id → DENY
    4. 401 "user grant required (strict privacy model 2026-08-16)"
```

The strict-privacy ship (v1.2.5) goes further: even `first_admin` and
`admin` need an explicit grant from `alice` before reading her private
tier.

## Why this matters for product design

If you're building a product on top of astor-memory:

1. **Your bots will never have god-mode access to user data.** They
   can only access what their bound user_id owns. This is enforced
   cryptographically (SQLite, ACL, audit row per cross-user attempt).

2. **You can migrate users between platforms without losing memory.**
   If alice moves from Telegram to Discord, just unbind her TG chat_id
   and bind her DC chat_id. Her private DB stays the same.

3. **You can run a single bot or many bots.** astor doesn't impose a
   topology — one bot for everything, one bot per user, one bot per
   platform, one bot per use case. All valid.

4. **The `users`/`admin` role distinction is platform-agnostic.** A user
   who happens to also be the bot operator gets `role=admin`, but the
   memory isolation is the same.

## Anti-patterns to avoid

### Don't merge platform + binding into one row

```sql
-- WRONG: would require token duplication per chat
CREATE TABLE bot_chats (
    bot_id TEXT, token TEXT, chat_id TEXT, user_id TEXT, ...
);
```

This pattern forces you to copy the bot token into every chat row. Token
rotation becomes N-row updates. Token leaks in logs hit every chat.

### Don't put user_id in platforms

```sql
-- WRONG: 1 platform row can serve many users
CREATE TABLE platforms (
    platform_id TEXT, user_id TEXT,  -- ← wrong axis
    ...
);
```

A Telegram bot serves many users. Putting user_id on `platforms` would
either require N rows per bot (back to duplication) or force only one
user per bot (kills the service-bot pattern).

### Don't treat Telegram and WeChat as the same shape

Telegram: 1 bot, many users, chat_id discriminates.
WeChat: 1 bot, many users (via DMs), but **the bot token's
permissions and the chat protocol are different**.

astor's `platform_kind` column keeps these explicit; the `bindings` table
absorbs the per-platform-chat_id-to-user_id mapping uniformly.

## See also

- `bot-binding.db` schema: see `astor_memory/_internal/bot_binding.py`
- `astor_memory/_internal/acl.py` — strict-privacy ship (v1.2.5)
- `docs/architecture.md` § 9 — Single-user vs multi-user mode
- `docs/architecture.md` § 2 — The 3-tier isolation model

## Version history

- 2026-07-30: First shipped with v1.0 (init had `install-state.json`
  tracking only)
- 2026-08-12: `wechat_bots.db` consolidated into `bot-binding.db`
- 2026-08-16: `bots/` directory created at runtime root + this DESIGN.md
  explaining the philosophy