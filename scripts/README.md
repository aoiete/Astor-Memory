# astor-doctor

Standalone repair CLI for astor-memory DBs. Idempotent, safe, no daemon.

## Why

When you upgrade astor-memory across versions, the bus schema (the
`memory_canonical` table inside `astor_bus_*.db`) gains new columns
and new NOT NULL requirements. New installs get the latest schema
from `CREATE TABLE IF NOT EXISTS`; older installs that have been
upgraded several times can end up with NULLs in columns that the
newest server code expects to be NOT NULL. That's the "bus conn
closed under concurrent /v1/forget" symptom — the server is
re-validating rows on every open.

`astor-doctor` lets you inspect and repair this state without
restarting the server or modifying any source.

## Subcommands

### `check`

Read-only scan of every DB under `ASTOR_DIR`. Prints schema + counts
per DB and exits non-zero if any DB has NULLs in required NOT NULL
columns or orphan candidates.

```bash
python scripts/astor_doctor.py check
# Found 8 DBs under D:\AI\Astor-Memory-Runtime
# public\memory\astor_bus_public.db
#    memory_canonical: 52 cols, 4290 rows, user_version=1
#    memory_candidates: 4701 rows (0 orphan)
#    events: 5459 rows
# ...
# All DBs healthy.
```

### `repair-conn`

Backfills NULL values in the four required NOT NULL columns of
`memory_canonical` and sets `PRAGMA user_version` to 1 so the next
run is a no-op. **Dry-run by default — pass `--apply` to write.**

| Column | Backfill value |
| --- | --- |
| `candidate_id`, `event_id` | `1` (safe fallback) |
| `content`, `namespace` | `"<repaired:column>"` (visible in audit) |

```bash
python scripts/astor_doctor.py repair-conn         # dry-run
python scripts/astor_doctor.py repair-conn --apply # write
```

### `repair-orphan`

Deletes rows from `memory_candidates` whose `event_id` no longer
exists in `events`. These are leftover state from interrupted writes
or partial upgrades. Dry-run by default.

```bash
python scripts/astor_doctor.py repair-orphan
python scripts/astor_doctor.py repair-orphan --apply
```

### `stats`

One-line counts per DB. Useful for cron monitoring:

```bash
$ python scripts/astor_doctor.py stats
DB                                                     MC   Cand    Evt   Orph  UV
--------------------------------------------------------------------------------
public\memory\astor_bus_public.db                    4290   4701   5459      0   1
source\memory\astor_bus_source.db                    3436    383    485      0   1
```

## Environment

- `ASTOR_DIR` — override the runtime dir (default `D:/AI/Astor-Memory-Runtime`).
  Useful for tests and CI.

## Tests

```bash
python tests/test_astor_doctor.py
# 8 passed, 0 failed
```

No pytest required; the test file uses only stdlib.

## Idempotency

- `repair-conn` is a no-op on a DB that already has `user_version=1` and no NULLs.
- `repair-orphan` is a no-op on a DB with zero orphan candidates.
- Both wrap each DB in one transaction; safe to interrupt mid-run
  (the next run will pick up where it left off).

## When to run

After every `pip install --upgrade astor-memory`:

```bash
python scripts/astor_doctor.py repair-conn --apply
```

For a regular sanity sweep, schedule weekly via cron or the
`astor-doctor-weekly` job.

## What it does NOT do

- Does **not** modify server source code or the running server.
- Does **not** restart any service.
- Does **not** require the server to be offline.
- Does **not** drop or recreate tables.

## Version

v1.16.58 — 2026-10-03.
