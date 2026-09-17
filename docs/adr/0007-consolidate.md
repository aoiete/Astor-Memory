# 0007. Consolidate — sleep-period memory maintenance

- Status: accepted
- Date: 2026-09-17
- Deciders: admin (first_admin), astor-memory maintainers
- Source: WeChat article `agent-memory` 2026-09-17 (fact 12072 style),
  WeChat article "AI 记忆系统产品经理综述" 2026-09-17,
  astor admin corpus observation: 4528/4565 facts at importance=0.5,
  25+ duplicate-prefix LESSON facts

## Context and Problem Statement

astor-memory's admin corpus has accumulated 4565 active facts, of which
**4528 (99.2%) sit at importance=0.5** with `kind=fact` (the default).
Two real problems:

1. **Capture is firehose, not curated.** The `capture_intent` hook
   auto-writes on every post_tool_call, so every lesson, every error,
   every debug observation lands in the bus. There's no quality gate
   upstream — only downstream decay sweep (v1.14.39, halve at 30d no-recall
   / tombstone at 90d no-recall).

2. **No consolidation step.** decay sweep only deletes; it doesn't
   merge duplicate observations, upgrade proven-valuable facts, or
   reclassify old facts to the 3-zone taxonomy
   (success_pattern / failure_pattern / lesson).

The WeChat article `agent-memory` (1317 stars, MIT) describes its own
answer: "session boundary auto-trigger write + sleep-period integration
process that keeps value and drops the rest." astor has the write step
but lacks the sleep-period integration.

## Considered Options

- **A — Do nothing.** Status quo: decay sweep tombstones 90d-stale facts.
  Pro: zero work. Con: 4528 same-weight facts dilute recall ranking,
  duplicate LESSONs waste search budget, the bus grows ~30 facts/day
  forever.

- **B — One-shot cleanup script.** Run a single dedup pass, write a
  report, archive. Pro: fast. Con: doesn't fix the underlying "no
  periodic integration" problem.

- **C — `astor consolidate` CLI with 4 actions (dedup / upgrade /
  classify / promote).** Manual + cron-triggered periodic maintenance.
  Pro: matches `agent-memory`'s design, gives admin explicit control,
  integrates with peer public network (dedup outputs are peer-shareable).
  Con: more code to write and test.

- **D — Always-on auto-consolidate on every recall.** Background
  consolidation runs when recall() is called. Pro: zero admin effort.
  Con: introduces latency variance, hard to debug, conflicts with
  decay sweep's existing per-read access_count bump.

## Decision Outcome

Chosen option: **C (`astor consolidate` CLI)**.

### Why C

- **Matches the user's locked insight** (fact 12274): when something
  works, lock it as a reusable pattern; when it fails, lock it as a
  failure_pattern. `astor consolidate` produces both kinds of facts
  (consolidation outcomes) so peer public tier can share them.
- **Reuses existing infrastructure**: decay sweep already runs on every
  `/v1/read` and tracks `access_count`. consolidate can use the same
  signals without inventing a new collection path.
- **Deterministic + idempotent**: re-running `astor consolidate` on
  an already-consolidated corpus produces 0 changes. This makes
  hermes cron weekly runs safe.

### Why not D

- Recall is the hot path; adding background consolidation there adds
  latency variance that admin will feel (recall p95 budget is
  ~250ms — consolidation would blow it).
- Hard to debug: "why did this recall miss?" becomes "did the
  consolidation step delete my fact?".

### Actions implemented

| Action | When | How | Risk |
|---|---|---|---|
| `dedup` | weekly | facts sharing prefix≥30 chars AND cosine content similarity > 0.95 AND count >= 2: tombstone all but highest-importance fact | LOW: tombstone is reversible (R-class lesson #4433 already establishes this) |
| `upgrade` | weekly | facts with `access_count >= 5` AND `importance == 0.5`: bump to `importance = 0.7` (medium) | LOW: importance bump doesn't change recall ranking much |
| `classify` | weekly | facts with `kind = 'fact'` AND `provenance_kind IN ('manual','user')` AND content contains success/failure/lesson markers (e.g. "成功", "失败", "教训", "verified", "走不通"): reclassify to `success_pattern` / `failure_pattern` / `lesson` | MEDIUM: regex false positives — see Risk section |
| `promote` | weekly | facts in `tier = 'private'` that have been `access_count >= 10` AND `importance >= 0.85`: copy to `tier = 'public'` (peer-shareable) AND keep original private for cross-user reference | MEDIUM: may leak user-specific facts to public — see Risk section |

### CLI surface

```bash
astor consolidate --dry-run                # default; report only
astor consolidate --actions=dedup          # run only dedup
astor consolidate --actions=all --yes      # run all + commit
astor consolidate --age=30d                # only facts >30d old
astor consolidate --user=admin --tier=private
astor consolidate --report=consolidate-2026-09-17.json
```

### Implementation

New file: `astor_memory/consolidate.py` (~150 LOC).

Key API:
```python
def consolidate(
    actions: list[str] = ['dedup', 'upgrade', 'classify'],
    age_days: int = 30,
    dry_run: bool = True,
    user_id: str | None = 'admin',
    tier: str = 'private',
) -> ConsolidateReport:
    """Run consolidation actions. Returns a report of changes.
    Idempotent: re-running on an already-consolidated corpus yields 0 changes.
    """
```

Tests (5):
- `test_consolidate_dedup_merges_duplicate_prefix`
- `test_consolidate_upgrade_promotes_high_access_facts`
- `test_consolidate_classify_moves_fact_to_zone`
- `test_consolidate_dry_run_makes_no_changes`
- `test_consolidate_idempotent_second_run_yields_zero_changes`

## Consequences

### Good

- **Stops fact inflation.** 4565 facts → ~3000 after first dedup pass.
- **Makes 3-zone taxonomy real.** Currently 99% facts are `kind=fact`.
  After `classify`, ~30% should land in success/failure/lesson zones.
- **Feeds peer public network.** `promote` action outputs are tagged
  `outcome:success / outcome:failure / outcome:lesson`, ready for
  public-tier routing (peer gossip design, fact 12609-12611).
- **Aligns with `agent-memory`'s sleep-period integration.** Same
  design vocabulary as the leading open-source alternative.
- **Reversible.** All actions tombstone (don't DELETE), so bad
  consolidations can be reviewed and reversed via `astor_unforget`.

### Bad

- **`classify` regex false positives.** A fact mentioning "成功"
  in passing (e.g. "if this works successfully, then...") might get
  reclassified as `success_pattern`. Mitigation: only reclassify
  facts whose content STARTS WITH the marker keyword, not contains it.
- **`promote` may leak user-specific facts.** A fact about admin's
  specific moomoo account could be promoted to public. Mitigation:
  `promote` requires `--yes` flag AND admin must review the report
  before commit.
- **First run is slow.** Scanning 4500 facts + computing pairwise
  cosine similarity is O(N²). Mitigation: cap to 1000 most-recent
  facts in first iteration; subsequent weekly runs are incremental.

### Risks

- **Idempotency broken.** If consolidate changes the same fact twice
  on a second run, audit log gets noisy. Mitigation: store a hash of
  the consolidation actions in `metadata` field; skip facts whose
  metadata already contains the hash.
- **Concurrent write conflict with decay sweep.** Both modify
  `access_count` / `importance`. Mitigation: serialize via
  `astor_bus.acquire_advisory_lock('consolidate')` (S-candidate).
- **Recall ranking shift.** After dedup, top-K recall may return
  different facts. Mitigation: ship with `--dry-run` first, let admin
  review the dedup target list, then commit.

## Related

- ADR-0003 (decay sweep) — decay sweep is the passive delete;
  consolidate is the active integration.
- ADR-0006 (wing routing) — `promote` action targets the same kind
  of facts that wing=human / wing=agent filters on.
- `astor_memory/bus/schema.py` — schema v3→v4 already has `tombstoned`
  + `importance` columns; no schema change needed for this ADR.
- `docs/competitive-sheet.md` § 2 — MemPalace living-memory dynamics
  validate the decay-sweep direction; consolidate goes one step
  further (active merge + promote).
- v1.14.39 (decay sweep default-on) — sibling ship.
- WeChat article `agent-memory` 2026-09-17 — design inspiration.
- WeChat article "AI 记忆系统产品经理综述" 2026-09-17 — design framework
  (4 evaluation metrics + 5-question gate + 6-step design).
