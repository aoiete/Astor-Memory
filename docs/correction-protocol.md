# Pushback / Correction Capture Protocol (v1.15.57)

> **Why this exists.** When a user tells an AI agent "you got that wrong" or
> "stop doing X", that lesson is gold — but only if it survives the session.
> OpenClaw's `self-improving` pattern (see `mp.weixin.qq.com/s/_qnX11asfGdS9V_tXw2zMQ`)
> calls this a "错题本" (mistake ledger). astor-memory implements the same idea
> by exposing the `memory_experience` table (shipped v1.6.0) over HTTP and
> auto-forking `kind=correction` writes from `/v1/write`.

## Three endpoints

| Endpoint | Purpose | When to call |
|---|---|---|
| `POST /v1/experience` | Capture a pushback (mistake or correction) | Agent detects user pushback (text + trigger words), OR `client.correct()` SDK helper |
| `POST /v1/experience/match` | Recall past pushbacks matching a query | Before taking an action that might repeat a known correction |
| `POST /v1/consult` (extended) | Reactive consult now includes `experience` field with auto-matched pushbacks | Whenever you would call `/v1/consult` |

### Plus auto-fork in `POST /v1/write`

When you write a fact with `kind ∈ {correction, pushback, user_correction, failure_pattern, lesson}`,
astor **automatically** inserts an `experience` row alongside the canonical fact.
Same dedup + 3-occurrence HOT-promote logic applies. **No client opt-in needed.**

### Plus `after_request` middleware

`/v1/read`, `/v1/write`, `/v1/consult` automatically receive an
`X-Astor-Experience-Warning` response header when matching experiences exist.
External agents don't need to remember to check — the warning just shows up.

## Field reference (`POST /v1/experience`)

| Field | Type | Required | Notes |
|---|---|---|---|
| `text` / `action_summary` | str | yes | What the agent did wrong (or what was tried). 4+ chars. |
| `outcome` | str | no | `success` / `partial` / `failure` / `neutral`. `failure` + `partial` auto-flag pushback. |
| `reflection` | str | no | Why it went wrong. |
| `next_step_hint` | str | no | What to do next time instead. |
| `trigger_keywords` | list[str] | no | Used for dedup + recall matching. First 5 participate in dedup hash. |
| `trigger_fact_ids` | list[int] | no | Related fact_ids (e.g. from `/v1/write`). |
| `user` / `actor` | str | no | Default `admin`. |
| `tier` | str | no | Default `source` for admin, `private` for others. |
| `source_session_id` | str | no | For traceability. |
| `importance` | float | no | Default 0.85 for pushbacks, 0.7 for neutral experiences. |

### Response

```json
{
  "experience_id": 1234,
  "occurrence_count": 3,
  "deduped": true,
  "dedup_hash": "eb6604daf622622b",
  "promoted_to_hot": true,
  "kind": "pushback_correction",
  "importance": 0.95
}
```

`promoted_to_hot=true` means the 3-occurrence threshold was reached; `importance` has been bumped to 0.95.

## Pushback auto-detection

Text starting with these trigger phrases auto-flags as pushback
(no need to set `outcome=failure` explicitly):

```
不对 / 错了 / 应该是 / 搞错了 / 你错了 / 你搞错 /
no, / wrong / that's wrong / should be / actually /
别再 / stop / don't / 不应该
```

## Dedup behavior

Two experiences are duplicates iff
`(actor, text.strip(), sorted(trigger_keywords[:5]))` produces the same SHA-256 prefix.
Repeated pushbacks on the same topic **increment `invocation_count`** and at 3 occurrences
auto-promote `importance` to 0.95.

The dedup hash is stored as a `[dedup:XXXXXXXXXXXXXXXX]` prefix in the `reflection` column.
Lookup uses `instr(reflection, '[dedup:HASH]') > 0` (exact substring — avoids LIKE wildcard
traps from the `[ ]` characters in the prefix).

## SDK usage (Python)

```python
from astor_memory.client import AstorClient
client = AstorClient(base_url="http://127.0.0.1:7803", user_id="my-agent")

# Capture a pushback (one call, server handles dedup + count + HOT promote)
result = client.correct(
    text="agent shouldn't kill gateway without user approval",
    reflection="auto-killed hermes PID 27364 on 2026-07-23",
    next_step_hint="ask user before restarting any service",
    trigger_keywords=["restart", "gateway", "kill"],
    outcome="failure",
)
print(result)
# {'experience_id': 14, 'occurrence_count': 1, 'deduped': False, ...}

# Before a risky action, recall past pushbacks
hits = client.match_experiences("restart hermes", top_k=3)
for h in hits:
    if h['occurrence_count'] >= 3:
        print(f"HOT rule ({h['occurrence_count']}x): {h['next_step_hint']}")
```

## Headers shipped on responses

| Response header | Endpoint | Meaning |
|---|---|---|
| `X-Astor-Experience-Warning` | `/v1/read`, `/v1/write`, `/v1/consult` | Top matching experience: `id | occ:N | HOT/warm | summary` |
| `X-Astor-Experience-Id` | `/v1/write` | The auto-forked experience id (only on `kind ∈ correction kinds`) |
| `X-Astor-Experience-Occurrence` | `/v1/write` | New occurrence count after the auto-fork |
| `X-Astor-Experience-Matches` | `/v1/read` | Comma-separated IDs of top-3 matching experiences |

HTTP headers must be latin-1 (RFC 7230), so non-ASCII summary text is best-effort
replaced (`?` for chars outside latin-1 range). The body fields are full UTF-8.

## Lifecycle

```
POST /v1/experience  ──┐
                       ├─→  memory_experience row + dedup check + count
POST /v1/write         ┘                  ↓
   kind=correction ────────────────────→   invocation_count += 1
                                            ↓ (≥ 3)
                                       importance = 0.95 (HOT)
                                            ↓
match_experiences()  /v1/experience/match  /v1/consult  /v1/read middleware
                                            ↓
                                       X-Astor-Experience-Warning header
                                       + recall body field
```

## Version

Shipped in **astor-memory v1.15.57** (2026-09-30).
Backed by `memory_experience` table (originally shipped v1.6.0 — this is a
protocol exposure, not a schema change). 24 unit tests pass.

## See also

- `memory_experience` schema in `astor_memory/bus/schema.py` (CREATE TABLE block)
- `AstorBus.insert_experience` / `match_experiences` in `astor_memory/bus/store.py`
- Client SDK: `astor_memory.client.AstorClient.correct` / `match_experiences`
- Background reading: "Self-learning三层进化机制" (红尘炼AI WeChat MP, 2026-09-30)