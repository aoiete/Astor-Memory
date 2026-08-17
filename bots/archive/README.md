# Archive

Retired single-platform DBs that have been consolidated into
`bot-binding.db` (at `$ASTOR_DIR/bot-binding.db`).

## Files

| File | Retired | Reason |
|---|---|---|
| `wechat_bots.db.archived_2026-08-16` | 2026-08-16 | Consolidated into `bot-binding.db` tables `platforms` (per-bot token + base_url + enabled) + `bindings` (chat_id → user_id) + `user_meta` (PII). |

## Safe to delete?

**Wait 60 days.** Delete after 2026-10-15 if no rollback needed.

To verify migration is complete before deletion:

```bash
# Check no active code reads wechat_bots.db
grep -rn "wechat_bots" .

# Confirm bot-binding.db has all 5 wechat bots
sqlite3 $ASTOR_DIR/bot-binding.db "SELECT platform_id, account_id, enabled FROM platforms WHERE platform_kind='weixin'"
# Expected: 5 rows
```

## Migration

5 wechat bots recorded in 2026-08-16 migration:
- `11d658c3e7f7@im.bot` → bound to `user_d` (lifetime)
- `2f94d1fb499c@im.bot` → bound to `user_c` (trial)
- `6b76b87d5954@im.bot` → bound to `sunday` (trial)
- `71cc412a0283@im.bot` → bound to `user_a` (lifetime)
- `8263b17ef9c7@im.bot` → bound to `admin` (first_admin)

All `account_token` values (the `botid:secret` strings) verified to match
between legacy and bot-binding.db platforms row. No data loss.
