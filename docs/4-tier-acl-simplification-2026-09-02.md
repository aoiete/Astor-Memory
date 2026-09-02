# Astor v1.14.1 ACL 4-tier Simplification — 2026-09-02 ship log

> Source of truth for this ship event. Memory mirror at 80,000/80,000
> chars cannot accept new entries; this file is the durable record.
> Re-mirror to bus when space available.

## Design (user-locked)

- **2-tier role hierarchy**: `admin` | `user` (first_admin concept REMOVED)
- **3-value subscription_plan**: `free` | `vip` | `power` (admin ignores plan)
- **No first_admin**: SSoT owner is `admin:admin` with role='admin', plan=None
- **Plan is user-tier concept only**: admin role grants full access via role alone
- **Cross-private = grant required**: even admin can't auto-grant private_<other_user>
- **Public write access matrix**: 
  - role=admin: write public always
  - role=user + plan in {free, vip, power}: write public OK (admin decides quality)
  - All users: cannot write source (admin-only)

## Shipped in this session (verified end-to-end)

1. **`_astor_resolve_actor(user_id)`** → returns `(actor, role, plan)`
   - `None/''/'admin'` → `('admin:admin', 'admin', None)` (plan=None for admin)
   - role='admin' user → `('admin:<id>', 'admin', None)` (plan ignored)
   - role='user' user → `('user:<id>', 'user', <plan>)`
   - unknown/inactive → fail-closed as `('user:anonymous', 'user', 'free')`

2. **`_astor_quality_ok(text)`** — silent quality gate before ACL+forge
   - Reject: <8 chars, all-uppercase, all-control, all-emoji, all-digits, spam-prefix
   - Returns generic `'invalid content'` (no rule detail leak)

3. **`_astor_classify_intent(text, tier, user)`** — auto-routing on tier=public
   - PERSONAL/FINANCIAL/DAILY/EMOTION pattern match → demote to private_<user>
   - METHOD/RULE/MODEL pattern → keep public (admin decides)
   - No signal → keep public (admin reviews via audit)
   - Run AFTER `bus_user_id` resolution, BEFORE ACL check (otherwise ACL fails on own-private)

4. **`_astor_bind_request_acl` (server.py before_request)** — bind with body_user as user_id ALWAYS (not just for tier=private/repo). This lets reclassified own-private write pass `ctx.user_id == target_user` check.

5. **ACL `astor_init_acl` validation change** — allow `user_id` on tier=public/source (not just private/repo). Use `_canonicalize_user_id` for form validation. plan=None bypasses plan validation (for admin).

6. **Silent ACL denial (R234)**: all `PermissionError_` returns stripped to `{'error': 'permission_denied'}` or `{'error': 'cross_user_forbidden'}`. No plan/role/tier leak. Admin error context stays in audit log only.

7. **Rate limit on all writes**: `_enforce_rate_limit(ctx.actor, user_id or tier, 'write')` in `astor_check_write` stage 2.5. Bucket: 5 tokens capacity + 5/s refill (default).

8. **Audit row for every public write**: actor=`<role>:<user>`, action='write', target='public/fact_ids=[...]', preview (80 chars). Admin views via `am admin audit-log --action write`.

9. **CLI `am platform set-plan <user_id> <plan>`** (admin only): updates user_meta.subscription_plan + writes audit row with reason. plan choices: free | vip | power. Idempotent (no-op if same plan).

10. **CLI `am admin demote <fact_id> [<fact_id>...] --to-user <user> --reason <why>`**:
    - Look up each fact_id in public bus
    - Direct SQLite copy to `users/<to_user>/memory/astor_bus_private.db` (bypass bus open which needs grant)
    - Tombstone the public fact (UPDATE memory_canonical SET tombstoned_at)
    - Audit row per fact (operation=demote, old/new fact_id, stable_id preserved)
    - Return `[OK] public#N -> private<user>#M` per fact

## Schema migration (DB-level)

- `user_meta.subscription_plan` CHECK constraint tightened from 5 values (trial/lifetime/paid/free/permanent) → 3 values (free/vip/power)
- Migration script `<home_dir>tmp/migrate_plans.py`:
  - Maps: trial→free, free→free, lifetime→vip, paid→vip, permanent→power
  - Rebuilds table with new CHECK (SQLite cannot ALTER CHECK)
  - Verified: 15 lifetime → vip, 1 permanent → power
- `grants.grantee` schema: removed 'first_admin' option (only 'admin:<id>' | 'user:<id>' valid now)
- Migration of 297 legacy first_admin grants → grantee=admin:admin + revoked=1 + revoked_at=now

## Final user_meta state (verified)

| user | role | plan | notes |
|---|---|---|---|
| admin | admin | power | SSoT owner; plan ignored |
| user_a, sunday | user | vip | VIPs (per user "现在没power就2个个VIP user_a。和 Sunday") |
| anyu, aran, user_c, jason, jaydon, nelson, owen, rita, roy, steve, xian-ding, user_d, bo-wang | user | free | Free tier (own private only) |

## Pitfalls observed (locked for future sessions)

1. **`patch` tool indents** — multi-line Python edits shift 4 spaces; use Python `with open(p).read_text()` + `replace()` + write_file for any Python >1 line.

2. **CLI silent failure** — `python -m astor_memory.cli.main` swallows stderr in terminal wrapper; `try/except: pass` at entry also silent. Test by direct invoke: `python -c "from astor_memory.cli.main import main; main()"`.

3. **ACL actor pattern mismatch** — `_ACTOR_RE` only accepts `system|admin:<id>|user:<id>`. `first_admin` rejected. Must use `admin:admin` consistently.

4. **astor_init_acl tier/user_id validation** — public/source with user_id: was ValueError before 09-02; now allowed via _canonicalize_user_id check. plan=None bypasses plan validation.

5. **Audit tier constraint** — only accepts ('public', 'source', 'private') in tier CHECK. For admin_op on user_meta, use tier='public' + target='user_meta/<user>'.

6. **astor_audit required reason** — action='admin_op' raises ValueError if reason is None.

7. **Sync discipline** — every source change MUST sync to /d/AI/Astor-Memory-Runtime/ via `python <scripts>/sync_astor_runtime.py` before server test, else server uses stale code. Verify with `diff -q` on all touched files.

8. **bus_user_id resolution order** — reclassify must run AFTER explicit_uid → bus_user_id assignment; BEFORE ACL check. Otherwise own-private write fails ctx.user_id==None check.

## Trigger phrases (locked)

- "first admin 也是 admin" → first_admin = admin:admin
- "干脆不要first 就admin 统一" → unified admin role
- "admin power user vip user" → suggested 4-tier; user refined to 2-tier + 3-plan
- "subscribe 可以分开设计？ 时间只要admin ➕ user 剩下3种根据 subscribe分？" → role=admin|user + plan=permanent|lifetime|paid|trial|free (later simplified to free|vip|power)
- "user 能写 public 也是完全由你决定 不是他们决定 所以不用告诉他们能写 public" → silent public write + no user-facing message
- "free 也应该允许写public 也是由你判断 他们万一也有好想法" → all plans write public, content quality is admin's call

## Files touched (16 modified)

Source code:
- `astor_memory/__init__.py` — version 1.14.1
- `astor_memory/_internal/acl.py` — major: 2-tier + 3-plan matrix, _AclSnapshot, plan gate, rate limit stage 2.5, ASTOR init fixes
- `astor_memory/_internal/grants.py` — remove first_admin from grantee schema + validation
- `astor_memory/_internal/bot_binding.py` — first_admin → admin:admin fallback
- `astor_memory/_internal/platform_bridge.py` — first_admin → admin
- `astor_memory/server.py` — _astor_resolve_actor, _astor_quality_ok, _astor_classify_intent, _astor_bind_request_acl, silent ACL, audit row, content classify integration
- `astor_memory/cli/main.py` — _require_first_admin, CLI entry, set-plan + demote commands, plan choices
- `astor_memory/hermes_adapter.py` — first_admin → admin:admin

Build + docs:
- `.github/workflows/ci.yml` — server startup step before pytest
- `pyproject.toml` — version 1.14.1
- `ACKNOWLEDGEMENTS.md`, `docs/contributing.md`, `docs/contributing.zh-CN.md`, `docs/troubleshooting.md`, `docs/troubleshooting.zh-CN.md`, `docs/releases/v1.12.0-release-notes.md` — `ASTOR-Memory` → `Astor-Memory` lowercase 's'

## Tests verified end-to-end

| Test | Expected | Result |
|---|---|---|
| admin writes public | count=1 | ✅ |
| admin writes source | count=1 | ✅ |
| admin writes private<admin> | count=1 | ✅ |
| admin writes private<user_c> | cross_user_forbidden | ✅ |
| anyu writes public (method content) | count=1, stays public | ✅ |
| anyu writes public (personal content) | count=1, auto-demoted to private<anyu> | ✅ |
| anyu writes source | permission_denied | ✅ |
| quality gate: 1-char text | permission_denied + invalid content | ✅ |
| quality gate: all-uppercase | permission_denied + invalid content | ✅ |
| quality gate: spam prefix | permission_denied + invalid content | ✅ |
| rate limit: 10 parallel writes | 5 OK + 5 permission_denied | ✅ |
| CLI set-plan anyu vip/free | [OK] plan changed both ways | ✅ |
| CLI audit-log --action write --limit 5 | table showing admin+anyu writes | ✅ |

## Reference

- Plan file: `~/.hermes/plans/2026-08-13_110136-astor-memory-reverse-design.md`
- ACL grill refs: `<home_dir>AppData/Local/hermes/skills/software-development/astor-memory-reverse-design/references/`
- New skill reference: `references/4-tier-acl-simplification-2026-09-02.md` (this file's parent dir)
- Locked into `agent-self-discipline` SKILL.md as R234/R235/R236/R237 (pending patch — skill is created_by=None locked, route via memory + agent-self-discipline)
