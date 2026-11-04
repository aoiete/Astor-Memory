# Intentions — facts with a due date

Status: design, not implemented.
Source: Tencent Hunyuan Hy-Memory 2.0 (`hy-memory` 2.0.13, 2026-11),
whose chat pyramid has an L7 layer — a forward-looking intention carrying
an expiry, which degrades to an ordinary fact once the date passes.

## The gap this fills

Astor already has bi-temporal facts: `memory_canonical.valid_until` is
NULL while a fact is current, and every read filters
`WHERE tombstoned = 0 AND valid_until IS NULL`. The machinery for
"this stops being true at time T" is in place.

What is missing is the other half: a fact that is *about the future* and
stops being a live obligation when its date passes. Today, statements
like

    the 60-day window for the burst scanners is not evaluable until 2027-01
    the intraday go-live gate needs 15 trades before it will say GO
    the first weekly action-plan delivery is 2026-11-06

are stored as ordinary facts. They never expire. Six months later a
recall surfaces them and the agent reads them as still pending, because
nothing in the record says they were time-boxed.

Hy-Memory's L7 is the same idea: an intention is a fact with an expiry,
and after the expiry it is demoted rather than deleted — the history
stays, the obligation does not.

## The smallest complete change

Three places, no schema migration — `valid_until` already exists.

1. `astor_memory/bus/store.py` — the insert hardcodes `valid_until = None`
   (around line 696). Add a `valid_until: str | None = None` parameter
   and pass it through.

2. `astor_memory/cli/main.py` — `am write` / `am learn` get an
   `--until <ISO timestamp>` flag, passed to the store call. The write
   path already skips `valid_until` when copying fields
   (line 1314), so nothing else needs to know about it.

3. `astor_memory/cli/main.py` — `kind` accepts `intention`. The kind set
   lives in one place; `AUTO_COMMONS_KINDS` governs what may be
   promoted into the shared tier, and an intention about one user's
   pending work is personal by nature, so it should not be in that set.

## What it deliberately does not do

* **No new table, no new tier, no new kind of row.** An intention is a
  fact with `valid_until` set. It stays in `memory_canonical`, stays in
  the same tier as the rest of that user's facts, and is expired by the
  existing bi-temporal path.
* **No "demote to ordinary fact" rewrite.** Hy-Memory demotes an
  expired intention instead of dropping it. Astor already excludes
  expired facts from reads while keeping the row, which gives the same
  practical behaviour: the history is there for an audit, the live
  obligation is not surfaced. Rewriting the row would cost a write and
  lose the fact's identity, for no gain.
* **No automatic expiry sweep.** The existing daily sweep already handles
  unconfirmed facts; `valid_until` is read-time filtered, so an expired
  intention disappears from recall the moment it passes, with no sweep
  required.

## How it would be used

    am write "the burst scanner 60-day window is not evaluable until 2027-01" \
        --kind intention --until 2027-01-15

The capture hook can do this without the operator naming it: a fact
whose content carries a resolvable date and whose kind is `decision` or
`fact` can be written with `valid_until` set to that date. That is a
policy decision, not a mechanism change, so it can be tuned later
without a schema change.

## Cost

`valid_until` is a nullable column that already exists and is already
indexed by the read path. The write path gains one optional argument.
There is no migration, no backfill, and no change to any existing fact.

## Related reading

Hy-Memory's other ideas were checked against this codebase and were
already present or judged out of scope:

| Hy-Memory | Astor today | Verdict |
|---|---|---|
| scene routing (chat/working/coding) | `scene` column exists, default `casual` | field present, per-scene pipelines are not |
| fact ↔ experience cross-links | `parent_fact_ids`, `source_facts` | present |
| conflict reconciliation | `nest/ecv.py` `scan_conflicts` | present |
| L0–L7 pyramid | L0/L1/L2 | three levels is enough; no evidence for seven |
| coding: record coordinates, not code | no coordinate field | real gap, but a separate piece of work |
| coding: version diff chain | `/v1/fact/<id>/versions` | exists, but no record of reverted versions |
