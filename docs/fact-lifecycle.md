# Fact Lifecycle Rules — Astor-Memory

> For the engine. Companion to `architecture.md` (which describes the *what* — schemas, stores, layers).
> This doc describes the **how** — how facts flow from write → store → recall → verify → expire → destroy.

Authoritative here. **If any other doc contradicts this, this wins.**

## 4-layer architecture (TencentDB-aligned)

Astor-Memory adopts Tencent's 4-tier memory model (`D:/AI/wiki/entities/01-tencentdb-agent-memory.md`):

| Layer | Name | Where | What |
|---|---|---|---|
| L0 | Raw | `bus_discrete` per-turn log | Every message + tool call + observation |
| L1 | Fact | `memory_canonical` (SQLite) | Extracted structured facts |
| **L2** | **Scenario** | **`scenarios.db` (SQLite)** | **Clustered facts by topic/project — this layer is what TencentDB calls "scenario clustering"** |
| L3 | Profile | `memu` (memU SDK) | Long-term user persona + stable preferences |

**L2 is the gap we filled 2026-07-31** via `scripts/scenario_clustering.py`. Without L2, recall is per-fact (noisy). With L2, recall queries top-N scenarios first, then drill down to the facts within those scenarios.

### Cold-start recall flow (L2-first)

```
query "weixin token renewal"
  ↓
bus (L1) → top-50 facts via BM25 + vector + rerank
  ↓
scenarios.db (L2) → keyword-cluster those 50 facts into 3 scenarios
  ↓
top-3 scenarios returned (relevance × decay × importance × access_count)
  ↓
drill down: each scenario's fact_ids → bus details
  ↓
memu (L3) profile layered on top if user-specific
```

This is what `recall_3store.py` session_start routine calls `<active_scenarios>` block.

## Why this doc exists

Astor-Memory's existing `Fact` schema (`architecture.md`) carries `kind`, `confidence`, `references`. But the **lifecycle** — when a fact graduates from "tentative" to "trusted", when it should decay, when a contradicted fact is "known wrong" vs "stale" — was scattered across session notes and bus hard rules. This doc centralizes it.

## Schema extension (additive — backwards compatible)

Every fact in `bus` carries these fields (existing `Fact` schema in `architecture.md` is unchanged; new fields are optional):

| Field | Type | Default | Notes |
|---|---|---|---|
| `kind` | enum | `fact` | `fact` / `user_preference` / `profile` / `risk_rule` / `lesson` |
| `success` | enum \| null | `None` | `True` (proven correct) / `False` (proven wrong) / `None` (unverified) |
| `confidence` | float 0-1 | `0.5` | Confidence score; see `## Confidence model` |
| `verified_at` | timestamp \| null | `None` | Last time truth was confirmed |
| `verified_by` | enum \| null | `None` | `user` / `auto` / `paper` / `test` / `cron` |
| `expires_at` | timestamp \| null | `None` | When fact becomes stale; auto-decay |
| `source` | string | `''` | `user` / `bus` / `forge` / `mempalace` / `paper:<id>` / `paper:<title>` |
| `references` | list[memory_id] | `[]` | cross-store backlinks (existing field) |

The runtime table `bus`/`nest`/`lex` may not have all columns yet — the API layer fills them in with defaults when reading.

## Confidence model

Confidence ∈ [0.0, 1.0]. Default writes 0.5. Increases via verification events; decreases via contradiction.

### Initial confidence (on write)

| Source | Default confidence |
|---|---|
| `user` explicit statement | 0.7 |
| `user` direct instruction ("always do X") | 0.85 |
| `llm` extracted from text | 0.4 |
| `paper` (peer-reviewed, arxiv id) | 0.7 |
| `paper` (preprint, blog, news) | 0.5 |
| `cron` action verified outcome | 0.85 |
| `backtest` proven | 0.9 |
| `test` written+pass | 0.95 |

### Confidence bumps (per verification)

| Event | Δ |
|---|---|
| Same fact seen again, no contradiction | +0.05 |
| User confirms explicitly | +0.1 |
| Repeatedly re-verified (3+ times) | +0.05 (cap at 0.95) |
| Contradicted by user | -0.3 |
| Contradicted by paper | -0.2 |
| Contradicted by backtest | -0.4 |
| Test fails | -0.5 (or set `success=False`) |

## `success` field rules

`success` is the **truth verdict**:

| Value | Meaning | When set |
|---|---|---|
| `True` | Verified correct (proven by user/test/backtest) | After `verified_at` event with positive outcome |
| `False` | Verified wrong (proven incorrect) | After `verified_at` event with negative outcome |
| `None` | Unverified (default) | All writes start here |

**Rules**:
- `success=None` → normal info on recall
- `success=True` and `confidence ≥ 0.7` → "trusted" badge on recall
- `success=False` and `confidence ≥ 0.7` → "known wrong" warning on recall
- `success=False` does NOT auto-delete — it stays as a guard rail ("don't do this again")

## Write rules (engine)

Every write (`am write` / `am broadcast` / `am ingest`) must:

1. **Stamp `kind`, `confidence`, `source`** at minimum. Other fields default.
2. **Generate `memory_id`** consistently (UUID v7 preferred — time-prefixed for ordering).
3. **Walk the dedup table** before insert:
   - If same `kind` + normalized text exists within 7 days → UPDATE (don't INSERT duplicate)
   - If same `kind` + text exists in `mempalace` (cold) → cross-link, don't duplicate
4. **Append to `audit_log`** with: op, memory_id, source, confidence, who wrote it.
5. **Skip `__pycache__`, `.git`, `*.bak`** — never ingest these as facts.
6. **24h auto-clean** for `kind=forgettable` facts (test/development markers).

## Read rules (recall)

Recall composition order (most-relevant first):

1. **bus** (recent, hot, fast) — primary recall
2. **nest** (associations) — link to related facts
3. **lex** (vector cosine ≥ 0.35) — semantic neighbors
4. **mempalace** (cold) — long-tail recall, demote if `>90 days` AND `success=None`

Recall injection policy:

- `success=True`, confidence ≥ 0.7 → **inject** (trusted)
- `success=None`, confidence ≥ 0.5 → **inject with no badge**
- `success=False`, confidence ≥ 0.7 → **inject with warning** ("guard rail: known wrong")
- `success=None`, confidence < 0.5 → **inject with hedge** ("tentative — verify before acting")
- `success` any, confidence < 0.3 → **hide** (too noisy)
- **expired** (timestamp past `expires_at`) → **inject with stale-flag** ("stale, verify before acting")

Always retrieve **3 nearest + 1 conflict** (active disambiguation). Single-fact recall is forbidden.

## Decay & cleanup

- Facts with `expires_at` set — auto-demote to `mempalace` on expiry (do not delete)
- Facts with `success=False` and `confidence < 0.1` and `last_seen > 180 days` — safe to delete
- `kind=forgettable` — auto-delete after 24h
- `kind=lesson` — never auto-delete (user hard rule R33: "data质性判断 must fact-check sample")

## Cross-store sync

When ANY store is updated, broadcast to the other 2 stores (bus → nest → lex/mempalace). If broadcast fails, fall back to a "pending sync" queue. On startup, replay the queue.

Rule (locked 2026-07-15): **sync is mandatory** — never skip cross-store broadcast.

## Conflict resolution

When two facts contradict:

1. Older fact (`verified_at < newer.verified_at`) loses by default
2. Higher confidence wins
3. `success=True` beats `success=None`
4. `user`-verified beats `auto`-verified beats `paper`-verified
5. If still tied, both remain as "conflicting" — both recalled with ⚠ tag

Never auto-delete a fact on conflict. The user decides.

## Risk rules (inherited)

These are user-encoded rules from the 5-store hard rule set; included here for completeness:

- **R33**: Data quality judgement must fact-check sample, never auto-delete based on count/ratio alone. (Why: 2026-07-09 user caught agent nearly deleting meihua_案例/ziwei_四化 classical fortune corpus.)
- **R34**: Before GPU training/eval, run `PRE_V10_CHECK.md` 9-item verification. (Why: 2026-07-09 user enforced "preparation before work = stable".)
- **R35**: Astor fortune models = user-private only, never publish, never host externally, never open-source weights. (Why: "天机不可泄露" user directive.)

## CRON role-based enforcement

When a cron job stores a fact, it must set:

- `source=cron`
- `verified_by=cron` (if outcome was observed)
- `confidence=0.85` (cron is real-time observed)
- `expires_at` = `next_run_at + 1h` (cron facts are time-sensitive)

If the cron job's action failed, set `success=False` and stamp `verified_at` with the failure timestamp.

## Style reference (where to look)

For Python implementation details — see `contributing.md` section 6 ("Style reference") — it explains the LLM-style normalization, dedup algorithm, and bus schema used here.

## PR / change workflow

1. **Source of truth**: `<source_dir>docs\fact-lifecycle.md` (this file)
2. **Runtime copy**: `<runtime_dir>docs\fact-lifecycle.md` (synced via `astor_dev_watch.py`)
3. **Change process**: edit source → file watcher auto-syncs + restart → becomes live
4. **Backwards compat**: any change here must be additive — never break existing fields

## Change history

- 2026-08-19: initial draft (locked per user directive — rules are guidelines, not hard-codes; can be expanded)
