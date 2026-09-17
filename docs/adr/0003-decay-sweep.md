# 0003. Time-decay sweep default-on (v1.14.39)

- Status: accepted
- Date: 2026-09-16
- Deciders: admin (first_admin), astor-memory maintainers
- Source: MemPalace v3.3.6 living-memory dynamics (2026-06-06),
  competitive sheet comparison, internal R-class R12345 (decay
  observation across 6 months of operation)

## Context and Problem Statement

astor-memory facts accumulate over time. The corpus has 3914 facts
(2026-09-16) and grows by ~30 facts/day from agent extraction. Without
decay:

- **Stale facts dominate recall** — old decisions, completed projects,
  superseded rules all still appear in top-K results.
- **Cold-storage bloat** — the vector index grows forever; query latency
  increases.
- **Signal-to-noise drops** — users learn to ignore recall because it
  surfaces irrelevant history.

MemPalace v3.3.6 shipped **living-memory dynamics** in June 2026: Hebbian
potentiation (connections strengthen with use) + Ebbinghaus decay
(connections fade without reinforcement). Empirically validated at
LongMemEval R@5 = 96.6%.

astor has had `access_count` and `last_confirmed_at` columns since
v1.14.19 (2026-09-13), and a gated decay sweep since then. The sweep
was **off by default** (`ASTOR_DECAY_SWEEP=1` to enable).

## Considered Options

- **A — Keep decay off by default** (gated, opt-in)
- **B — Decay on by default** (`ASTOR_DECAY_SWEEP=0` to disable)
- **C — Decay on, but with a UI dashboard toggle** (admin can flip per
  environment)

## Decision Outcome

Chosen option: **B (decay on by default)**.

### Why B

- **MemPalace validates the direction** — same Hebbian + Ebbinghaus
  pattern, 96.6% LongMemEval R@5.
- **Admin's corpus is dense** — 30 facts/day × 365 days = ~11k facts/year.
  Without decay, recall quality will degrade measurably by month 6.
  Verified: top-10 recall already contains ~15% stale facts
  (last_confirmed_at > 90d) per R12345 internal audit.
- **Escape hatch is one env var** — `ASTOR_DECAY_SWEEP=0` for users
  who explicitly want to preserve all facts.

### Behavior (unchanged from v1.14.19)

- **30d no-recall** — `access_count = MAX(1, access_count / 2)` (floor
  at 1, never zero, so a single recall can resurrect any fact).
- **90d no-recall** — `tombstoned = 1` (archived, not deleted; can be
  resurrected via `/v1/forget --reverse=tombstoned` if needed).

### Where it runs

Per-read in `server.py:_astor_track_access` (after `/v1/read` returns
hits). Runs on every recall — but only the SQL UPDATE, no LLM cost.
Latency impact: <1ms.

## Consequences

### Good

- **Recall quality stays high** — stale facts fade out automatically.
- **Corpus stays bounded** — at steady state, only ~30-90 days of
  active facts survive, so the vector index stays small.
- **No LLM cost** — pure SQL UPDATE.
- **Reversible** — `ASTOR_DECAY_SWEEP=0` disables; tombstoned facts
  can be un-tombstoned manually.

### Bad

- **One env var to remember** — `ASTOR_DECAY_SWEEP=0` for users who
  want everything preserved.
- **Tombstoned ≠ deleted** — facts still occupy disk until explicit
  cleanup. Mitigated by monthly `astor_purge_tombstoned.py` cron (S-
  candidate, not yet shipped).
- **Multi-tenant fairness** — if some users have very active recall
  and others don't, the active users' facts dominate. Acceptable trade-
  off: real activity = real relevance.

### Risks

- **Aggressive decay** — if 30d halve is too aggressive for some user,
  they can `ASTOR_DECAY_SWEEP=0`. No global knob to tune yet
  (S-candidate: `ASTOR_DECAY_HALVE_DAYS` env var).
- **Race condition** — decay SQL runs on the read path. If the read
  fails mid-decay (rare), the halving is committed. Mitigated by
  `bus.conn.commit()` only after all 3 UPDATEs succeed.

## Related

- ADR-0001 — decay sweep operates per-tier in 9-DB layout.
- ADR-0002 — hybrid recall's hit list is what gets `access_count` bumped.
- `docs/competitive-sheet.md` § 2 "Time decay" row.
- `tests/test_decay_sweep.py` — 5 tests covering default-on contract,
  halve-at-30d, tombstone-at-90d, floor-at-1, env-var override.
- v1.14.39 CHANGELOG entry.
