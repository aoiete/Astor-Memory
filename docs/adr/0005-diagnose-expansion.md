# 0005. Diagnose expansion: proxy + DB corruption + embedding version

- Status: accepted
- Date: 2026-09-16
- Deciders: admin (first_admin), astor-memory maintainers
- Source: MemU v2.0.0 `memU doctor` CLI (2026-07-23), internal R-class
  R12389 (silent DB corruption caught only after embedding drift), R12390
  (proxy hijack observed during network change in 2026-08)

## Context and Problem Statement

astor-memory's `/v1/health/diagnose` endpoint shipped with three checks:
embedding failure counts, warning summaries, audit log severity buckets.

It did NOT check:
1. **Proxy hijack** — if `HTTPS_PROXY` env var is set to a non-loopback
   address, all outbound embedding calls go through an attacker-controlled
   proxy. Silent data leak.
2. **DB corruption** — SQLite can have FK violations, page-level
   corruption, or schema drift after a power loss. Today these surface as
   "weird recall misses" days later, not at fault time.
3. **Embedding model version drift** — if the loaded model doesn't match
   the one that wrote the existing embeddings, cosine scores degrade
   silently. Recall looks broken but isn't.

MemU's `memU doctor` CLI (v2.0.0-beta.0, 2026-07-23) shipped a similar
diagnostic suite. We adopt the same three checks.

## Considered Options

- **A — Keep diagnose minimal** (current behavior)
- **B — Add the 3 new checks** (proxy + corruption + embedding version)
- **C — Add them as separate `/v1/diagnose/<check>` endpoints**

## Decision Outcome

Chosen option: **B (add the 3 checks to the existing endpoint)**.

### Why B

- **Single endpoint is easier for the dashboard** — one fetch, all
  checks visible. Option C would force 4 round trips and the dashboard
  would need to render 4 cards.
- **Aggregate warn flag** — `ship_c_warn` is true if any check fails,
  so a dashboard tile can light up red with one boolean.
- **No new endpoint surface** — the existing endpoint already has the
  user/astor_dir query params, so the new checks naturally inherit them.

### Why not C

- Operational complexity (4 endpoints to monitor).
- Dashboard complexity (4 tiles vs 1).
- No benefit unless each check has its own retention / alerting rules,
  which we don't need yet.

### Implementation

In `server.py:_astor_diagnose_route` (the `/v1/health/diagnose` handler):

1. **proxy_hijack_check** — reads `HTTPS_PROXY`, `HTTP_PROXY`, `https_proxy`,
   `http_proxy` env vars. Loopback (127.0.0.1, localhost, ::1) is OK
   (intentional dev proxies like mitmproxy). Non-loopback is `warn: true`.
   Windows env vars are case-insensitive, so dedupe by case-folded key.
2. **db_corruption_check** — `PRAGMA integrity_check` (must return "ok"),
   `PRAGMA foreign_key_check` (must return empty). Report first 5 FK
   violations as samples.
3. **embedding_version_check** — load the configured model, embed a
   1-char string, report dim + name. Catches "model failed to load"
   and "wrong model loaded" both.

The endpoint also exposes a top-level `ship_c_warn: bool` that's true
if any check warns.

## Consequences

### Good

- **Silent failures become visible** — proxy hijack, DB corruption,
  model drift all surface in the diagnose output before recall looks
  broken.
- **Single endpoint, single round trip** — dashboard can render one
  warning tile.
- **Reversible** — if any check is too noisy, the per-check section can
  be commented out without touching the rest.

### Bad

- **Slower `/v1/health/diagnose`** — embedding_version_check loads the
  model (cached after first call). First call: ~500ms. Subsequent: <50ms.
- **False positives** — non-loopback proxy env vars might be intentional
  (corporate proxy, dev tunnelling). User must read the findings and
  decide.
- **PRAGMA integrity_check on huge DBs is slow** — for admin private
  DB at 50MB+, ~50-200ms. Acceptable for a diagnose endpoint that runs
  on demand.

### Risks

- **Proxy check is best-effort** — `HTTPS_PROXY` is the standard name,
  but some users set `GRPC_PROXY`, `ALL_PROXY`, etc. We don't check those
  yet. Mitigation: add to future S-candidate.
- **integrity_check is local** — it doesn't check the FS layer (bad
  blocks, stale snapshots). Mitigation: pair with periodic backup
  verification (already in `smart_backup.py`).
- **embedding_version_check doesn't verify the loaded model matches
  what wrote the corpus** — that's a more expensive check (read first
  embedding's dim from DB, compare to current). S-candidate.

## Related

- `astor_memory/server.py:_astor_diagnose_route` — the expanded handler.
- `tests/test_diagnose_expansion.py` — 5 tests cover proxy + DB
  corruption logic (embedding check is integration-level, requires live
  server).
- ADR-0001 — diagnose operates on the 9-DB layout (one DB per user /
  tier / store).
- `docs/competitive-sheet.md` § 5 "Observability / Operations" row —
  MemU's doctor is the source citation.
- v1.14.43 CHANGELOG entry.
