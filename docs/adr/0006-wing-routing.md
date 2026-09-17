# 0006. Kind-based routing (wing alias for /v1/read)

- Status: accepted
- Date: 2026-09-16
- Deciders: admin (first_admin), astor-memory maintainers
- Source: MemPalace v3.3.6 `wing_api` (2026-06-06), internal observation
  that post_tool_call hooks' auto-captured facts were competing with
  human-typed facts in the same recall slot, lowering signal-to-noise.

## Context and Problem Statement

astor-memory's `/v1/read` is single-stream: it returns facts of every
provenance_kind mixed. Empirically:

- A user asks "what did I tell you about X?"
- Recall returns 3 facts the human said + 7 facts the agent extracted
  from hooks + 2 system rules. Top-K is filled with agent chatter.

MemPalace v3.3.6 ships a physical `wing_api` partition: API-tool
transcripts route into a separate wing so they don't pollute human/
project wings. We adopt the same idea without the physical separation
(we already have 9-DB layout per tier; adding physical wing separation
would multiply storage 3x with no clear win).

## Considered Options

- **A — Keep single-stream recall**
- **B — Add wing alias for /v1/read** (logical partition, filter at
  query time)
- **C — Physical wing DBs** (3× storage, complex migration)

## Decision Outcome

Chosen option: **B (wing alias filter on top of existing 9-DB layout)**.

### Why B

- **Solves the same problem as MemPalace** (recall precision for human
  queries) with much less infrastructure.
- **No migration** — wing is just a mapping over existing
  `provenance_kind` values.
- **Composable** — works alongside existing kinds filter, so users can
  do `kinds=user_preference + wing=human` to get only their own typed
  preferences.
- **Auto-derive provenance_kind at write time** — callers don't have to
  pass `provenance_kind` explicitly; we infer it from `origin_session_id`
  prefix. Manual callers can still override.

### Wing mapping

| wing | provenance_kind set | Examples |
|---|---|---|
| `human` | `{manual}` | discord:/telegram:/wechat:/cli: sessions |
| `agent` | `{extracted, inferred, merged}` | hook:post_tool_call, cron:, auto_link:, merge: |
| `rule` | `{rule}` | system-injected lessons, capture_intent hook |

Unknown wing → HTTP 400 (silent zero results would confuse callers).

### Implementation

1. **`_infer_provenance_kind(origin_session_id)`** — new helper at
   `server.py`. Called from `/v1/write` body handler. Manual callers
   can still override by passing `provenance_kind` in the body.

2. **`WING_TO_PROVENANCE` dict + `_expand_wing_to_provenance(wing)`**
   helper. Returns set[str] for SQL filtering, or raises ValueError
   on unknown wing.

3. **`/v1/read` body field `wing=human|agent|rule`** — filter runs
   AFTER the existing kinds filter so users can combine.

## Consequences

### Good

- **Recall precision up** for human queries — "what did I tell you
  about X" returns human-typed facts first.
- **Composable with kinds** — `kinds=failure_pattern + wing=human` is
  a common query and now works.
- **Auto-derive at write** — fewer manual mistakes (forgetting to set
  provenance_kind).
- **No storage cost** — pure filter at query time.

### Bad

- **Filter happens after recall** — we still load all matching facts
  into memory, then filter. ~60µs per filter for top-K=10. Acceptable.
- **Auto-derive is heuristic** — origin_session_id prefix conventions
  may drift. If they do, auto-derived provenance_kind drifts. Manual
  override always available.
- **One wing per fact** — facts can't be in two wings. If a fact is
  "human-typed then agent-extracted", we pick one (the source-side
  convention: manual wins over inferred).

### Risks

- **Convention drift** — hermes capture_intent hook might start using
  a new prefix not in our list. Mitigation: the helper returns `None`
  for unknown prefixes, which preserves the old "no provenance_kind"
  behavior. Add the new prefix when discovered.
- **Empty filter result** — wing=agent on a corpus with only manual
  facts returns []. Not an error, but caller must handle.

## Related

- `astor_memory/server.py`:
  - `_infer_provenance_kind()` — auto-derivation helper
  - `WING_TO_PROVENANCE`, `_expand_wing_to_provenance()` — wing map
  - `/v1/read` body parser — `wing=` filter (post-kinds)
- `tests/test_wing_routing.py` — 11 tests cover inference + mapping.
- ADR-0002 — wing filter runs after the kinds filter on the same
  hybrid recall result.
- `docs/competitive-sheet.md` § 3 "API-tool call routing" row —
  MemPalace wing_api is the source citation.
- v1.14.44 CHANGELOG entry.
