# ACL Hardening — v1.2 (2026-09-01)

## Why we hardened the ACL

Astor-Memory's ACL is the **single gatekeeper** for every read and write
in the system. Any weakness there is a system-wide data leak. We harden
it in v1.2 because the original 1.0/1.1 design had three structural holes:

1. **`astor_init_acl` accepted any string** for `actor`. A typo
   (`'first_admin'` vs `'firt_admin'`) or an attacker-controlled caller
   could silently bind a context with the wrong identity.

2. **`tier='public'` was writable by every role**, including a compromised
   `user` or `system` actor. That meant a single leaked user token could
   spam public facts to mislead cross-user recall.

3. **`user_id=None` for tier=private/repo was a silent pass**. Caller
   bugs (forgot to pass who they were reading) looked like "success"
   while actually reading the wrong user's namespace.

All three are now closed. **Nothing else changes** in v1.2 — the existing
matrix logic, the grant system, the audit logger, and the runtime
query paths are untouched.

---

#### Changes in v1.2 (four, by category)

### 1. `astor_init_acl` now validates every argument

| What | Before | After (v1.2) |
|---|---|---|
| `role` | checked against `_VALID_ROLES` | unchanged |
| `tier` | checked against inline tuple | centralized into `_VALID_TIERS` |
| `actor` | **no validation** | must match `_ACTOR_RE = ^(first_admin\|system\|admin:<id>\|user:<id>)$` |
| `actor`/`role` consistency | **not checked** | rejected (`actor='first_admin'` cannot run as `role='user'`) |
| `user_id` | checked for None vs tier | also checked against `_USER_ID_RE` for format |
| re-init (different actor) | silent overwrite | audit-logged so privilege escalation is visible |

The canonical forms are documented in `AccessContext.actor`:

- `first_admin` — system root
- `admin:<id>` — power user identified by canonical id
- `user:<id>` — regular user
- `system` — background tasks (`am compact`, `am audit`, etc.)

Anything else is a typo or a smuggling attempt and is rejected at the
boundary, not silently accepted.

### 2. `tier='public'` write is restricted to first_admin + admin

Before, `user` and `system` could write to `public`. That meant a single
leaked user token could poison cross-user recall by spamming
public facts. After:

```python
("write", "public"): {"first_admin", "admin"},  # NOT user, NOT system
```

`user` and `system` can still **read** public; they just cannot write to it.
This is the same isolation philosophy that private uses: write access is
the dangerous one, not read.

### 3. `user_id=None` is no longer a silent pass for tier=private/repo

Both `astor_check_read` and `astor_check_write` now raise
`PermissionError_` if called with `tier='private'` and `user_id=None`:

```python
raise PermissionError_(
    f"actor={ctx.actor!r} attempted to read tier=private with user_id=None; "
    f"caller must supply the target user_id"
)
```

Previously, the function silently returned, and caller bugs went
undetected. Now they fail loud at the first request, which is exactly
when you want the failure.

### 4. Re-init is audit-logged

`astor_init_acl` is allowed to be called multiple times within a process
(operator may switch contexts), but a re-init where the actor **changes**
writes an audit row tagged `action="rebind"`. That means silent
privilege escalation — where an unprivileged caller rebinds the
context to `first_admin` between two requests — is now visible in the
audit log.

---

#### What we deliberately did **not** change

Some improvements are tempting but would break existing callers or
overlap with existing mechanisms. We did **not** add:

- **Per-user ACL grants for tier='repo'**: repo is a v1.1 per-git-repo
  namespace; cross-user read already requires `role='first_admin'`
  for write. Adding grants would conflict with the agent self-pattern
  documented in `AccessContext.actor`.
- **Time-based ACL expiry**: this is a separate workstream (P12
  per-action audit decay); tracked in `roadmap.md`.
- **Role downgrade path for `first_admin`**: explicitly forbidden by
  the plan (`first_admin` cannot be demoted). v1.2 keeps that.

---

#### How to verify v1.2 is in effect

```python
from astor_memory._internal.acl import (
    astor_init_acl, astor_check_read, astor_check_write,
    _MATRIX, _VALID_ROLES, _VALID_TIERS,
)

# 1. Matrix check
assert _MATRIX[("write", "public")] == {"first_admin", "admin"}

# 2. Validation rejects bad inputs
try:
    astor_init_acl(actor="hacker", role="first_admin", tier="source")
    assert False, "must reject non-canonical actor"
except ValueError:
    pass

# 3. user_id=None raises PermissionError_
astor_init_acl(actor="user:alice", role="user", tier="private", user_id="alice")
try:
    astor_check_read("private", user_id=None)
    assert False
except PermissionError_:
    pass
```

All three of these tests are in `tests/test_acl.py::test_v12_hardening`
(v1.2 ship adds them).

---

#### Threat-model coverage

After v1.2, the following attacks are blocked at the ACL layer
(no need for caller cooperation):

| Attack | v1.2 mitigation |
|---|---|
| Caller passes `actor='hacker'` to bind a fake context | rejected by `_ACTOR_RE` |
| Caller passes `actor='first_admin' role='user'` (impersonation) | rejected by role consistency check |
| Caller calls `astor_check_read('private', user_id=None)` and reads whatever comes back first | raises PermissionError_ |
| Compromised user token writes to `tier='public'` and poisons cross-user recall | role not in `("write","public")` set |
| Code re-binds ACL context between requests to escalate | audit row with `action="rebind"` |
| Random user_id like `"alice@example.com"` slips past format check | rejected by `_USER_ID_RE` |

---

#### Files touched

- `astor_memory/_internal/acl.py` — v1.2 hardening
- `tests/test_acl.py` — added `test_v12_hardening` covering all six
  rejection paths above

No other modules needed changes; the new validation is additive at
the boundary and existing callers that pass canonical arguments
continue to work unchanged.

---

#### Lock

v1.2 hardening: 2026-09-01.
Backstop: any future caller that depends on the silent-pass behavior
will fail loudly on first contact — which is the goal.