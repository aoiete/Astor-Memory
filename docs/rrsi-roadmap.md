# RRSI roadmap for astor-memory (2026-09-22)

**Status:** Partially shipped (v1.15.6, v1.15.7). Full self-evolving
harness **NOT recommended** — see "Why we stopped at pass c" below.

## What is RRSI?

[Regularized Recursive Self-Improvement of Agent Harnesses](https://arxiv.org/abs/2609.24972)
(Google Cloud AI Research + UNC + Stanford). The paper proposes 7
constraints that let an LLM edit its own agent harness without
overfitting to a small eval set:

| # | Constraint | Goal |
|---|---|---|
| 1 | **Edit budget per round** (cosine annealing) | Allow broad edits early, narrow attribution late |
| 2 | **Edit ledger** (per-candidate record) | Track what changed, why, and what came of it |
| 3 | **Stagnation break** | Force exploration of untouched components |
| 4 | **Leakage audit** | Verify eval set wasn't tampered with |
| 5 | **Noise floor** | Reject score deltas inside the empirical variance band |
| 6 | **Cost rule** | Token-cost increase must be paid for by score gain |
| 7 | **Pruning** | Drop components that stopped earning their keep |

## What we shipped

### Pass a — constraint 4 (leakage audit)

`tests/eval_runner.py` v1.15.6 — appends three new fields to every
run summary:

- `astor_version` (from `__init__.py`)
- `git_commit` (HEAD hash)
- `eval_set_hash` (SHA-256 prefix of `tests/eval_set.jsonl`)

The trend tool can now distinguish "score jumped because the test
set was edited" from "score jumped because the harness improved".

### Pass b — constraint 5 (noise floor)

`am decay-sweep` v1.15.7 — `--require-no-recall-days N` flag on both
`run` and `stats`. A fact that was recalled in the last N days is
protected from decay even if it has low importance + low access
count, because it is clearly not noise.

Default `N=30` matches the daily_brief cadence. `0` = legacy off.

### Pass c — constraint 1 (edit budget alias)

`am decay-sweep run --max-sweep-size N` is an alias for `--limit N`.
Same SQL, same audit, just the RRSI name. Lets future cron scripts
match the paper's vocabulary.

### Spec only — constraint 2 (edit ledger) is partially there

astor already records audit log entries for every decay-sweep
(`actor=cli:decay-sweep`, `action=decay_sweep`). This is the per-action
ledger. What's missing:

- Per-candidate "what hypothesis was this trying to test" — currently
  the audit just says "swept fact X" with no rationale.
- Cross-run aggregation tool — `tests/eval_trend.py` exists for eval
  trends but no equivalent for decay-sweep trends.

Both are deferred. See "Why we stopped" below.

## What we did NOT ship

| Constraint | Why deferred |
|---|---|
| 2 (ledger rationale) | Needs a CLI UX change: every sweep caller would have to provide a `--reason` that's actually informative, not just `decay_sweep_v1.14.73`. Risk: callers use a generic string and the ledger is useless. |
| 3 (stagnation break) | Stagnation detector would need a window of N sweeps where "untouched components" matters. We don't yet have enough sweep history to know what "untouched" looks like. |
| 6 (cost rule) | "Token-cost increase must be paid for by score gain" needs a paired eval + token counter. Eval tracks latency_ms, not tokens. Adding token tracking is a separate ship. |
| 7 (pruning) | "Drop components that stopped earning their keep" — astor doesn't have a "components" concept at the harness level. Decay sweep is the closest analogue. |

## Why we stopped at pass c

The paper's full loop is **LLM modifies its own harness**. astor has
two distinct surfaces where "harness" means different things:
1. **Operator harness** — eval scripts, decay cron, daily_brief. The
   operator runs these; the LLM edits them.
2. **Agent harness** — the in-loop astor_recall / astor_write tools
   that the runtime agent calls during a conversation. Modifying
   this requires touching `hermes_adapter.py` and the agent's system
   prompt, which crosses R218 (no-gateway-changes rule).

Passes a + b + c ship observability + decay hygiene for surface 1.
The remaining constraints (3, 6, 7) need operator-side tooling we
don't have yet (window of stagnation, paired eval+token counter,
component taxonomy).

Ship decision per R12481 (entity_lex pass deferred for lack of real
data) + R11887 (n<20 trades is statistically unreliable): the full
RRSI loop needs ~1 week of post-ship data on the existing passes
a + b + c before we can claim the paper's results transfer to astor.

## Open follow-ups

- **v1.16.0** (proposed): cron `astor-rrsi-eval-weekly` that runs
  `tests/eval_runner.py` + `tests/eval_trend.py` and ships the
  trend report to `logs/rrsi_eval_history.jsonl`. This is the data
  collector for passes 2/3/6/7.
- **v1.17.0** (proposed): ledger-rationale CLI UX. Requires
  operators to commit to a `--reason` per sweep.
- **v1.18.0** (proposed): token-budget tracking for cost rule.

If we ship these three and then re-evaluate, that's the smallest
sequence that gives RRSI full coverage within R218.

## References

- RRSI paper: https://arxiv.org/abs/2609.24972
- WeChat article (Chinese): https://mp.weixin.qq.com/s/xwe31Ff7I5VsTSgiuQ60uA
- astor decay-sweep docs: `docs/architecture.md` (Decay section)
- astor eval harness: `tests/eval_runner.py`