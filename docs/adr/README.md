# ADR Index — Architecture Decision Records

This directory contains the architectural decision records for astor-memory.
Each ADR documents one significant design decision: the context, the
options considered, the chosen path, and the consequences.

ADRs are written in the **MADR** (Markdown Any Decision Records) style
because it travels well in plain Markdown without extra tooling — anyone
with a text editor can read or write one.

## Format

```markdown
# NNNN. Short title

- Status: proposed / accepted / superseded
- Date: YYYY-MM-DD
- Deciders: <who decided>
- Source: <where the decision came from — chat, paper, prior ADR>

## Context and Problem Statement

<What is the issue?>

## Considered Options

- Option A
- Option B
- Option C

## Decision Outcome

Chosen option: <X>, because <reasons>.

## Consequences

- Good: <benefits>
- Bad: <tradeoffs>
- Risks: <what could go wrong>
```

## Status meanings

- **proposed** — drafted, awaiting decision
- **accepted** — locked in, this is what we ship
- **superseded** — replaced by a later ADR (kept for history, link forward)

## Index

| # | Title | Status | Date |
|---|---|---|---|
| 0001 | 9-DB SQLite layout (3-tier × 3-store) | accepted | 2026-08-15 |
| 0002 | Hybrid retrieval by default, graph optional (R201 lock) | accepted | 2026-09-15 |
| 0003 | Time-decay sweep default-on (v1.14.39) | accepted | 2026-09-16 |
| 0004 | L1/L2 multi-granularity recall (cluster summary → fact ranking) | accepted | 2026-09-16 |
| 0005 | (reserved for Ship C — diagnose expansion) | proposed | TBD |

## Conventions

- New ADRs append to this index in chronological order.
- Status changes (proposed → accepted / superseded) update both the
  individual ADR and this index.
- ADRs reference each other by `ADR-NNNN` short form, never by section
  number (section numbers drift when files get edited).
- When reviewing T-sheet (`docs/competitive-sheet.md`), the "Lessons
  Learned" table should link to ADRs where applicable.
