# Migration Guide

> Upgrade from the legacy `memory-bus` system to Astor-Memory.

This guide walks through a 5-step migration. Plan for ~2 hours of focused work and a 1-2 week parallel-run window before cutover.

---

## Table of contents

1. [Why migrate](#why-migrate)
2. [Compatibility promise](#compatibility-promise)
3. [The 5-step migration](#the-5-step-migration)
4. [Side-by-side reference](#side-by-side-reference)
5. [Rollback procedure](#rollback-procedure)
6. [Common pitfalls](#common-pitfalls)

---

## Why migrate

The legacy `memory-bus` system was a 3-server architecture (bus_server, memu_server, mempalace_server) that ran for 33 ship sessions and ~6 months. It worked, but accumulated pain points:

| Pain point | Impact | How Astor-Memory fixes it |
|---|---|---|
| 3 GB venv (transformers + torch) | Disk space, slow install | No local LLM stack; < 50 MB install |
| 3 server processes | 3 health checks, 3 restarts, 3 ports | 1 daemon or pure library mode |
| chromadb 80 MB transitive deps | Slow upgrades, frequent breakages | SQLite + NumPy; 12 MB |
| memu.ai SDK proprietary | Cloud coupling | Vendor-neutral LLM adapter |
| No revision tracking | Silent overwrites on update | Append-only + `revision_id` |
| No citation in recall output | Hallucination cascades | `<ref>` embedded in every hit |
| No lifecycle (decay/merge/promote) | Unbounded memory growth | Self-evolving with `am compact` |

If you've been hitting any of these, migration is worth it.

---

## Compatibility promise

Astor-Memory v1.0 maintains **3 compatibility layers** to make migration gradual:

### 1. Env-var compat (zero-effort)

The old system used env vars like `MEMU_URL`, `MEMPALACE_URL`, `BUS_URL`. Astor-Memory keeps these as aliases:

| Old env var | New env var | Behavior |
|---|---|---|
| `MEMU_URL` | `ASTOR_FORGE_URL` | Both work; old is silently redirected |
| `MEMPALACE_URL` | `ASTOR_NEST_URL` | Same |
| `BUS_URL` | `ASTOR_BUS_URL` | Same |

Existing cron jobs that set `MEMU_URL=http://localhost:7801` will continue to work after `pip install astor-memory` — no env var changes required.

### 2. Import compat (cutover)

Old code:

```python
from memory_bus import auto_route_v2

auto_route_v2.write("user prefers concise replies")
hits = auto_route_v2.read_all("user preferences")
```

New code (preferred):

```python
from astor_memory import write, read

write("user prefers concise replies")
hits = read("user preferences")
```

For projects with 10+ import sites, see [Step 3: Bulk import cutover](#step-3-bulk-import-cutover).

### 3. Data compat (DB migration)

The old `memory_bus.db` SQLite schema is **not** directly compatible with Astor-Memory's `astor_bus.db`. A one-time migration CLI is shipped in v0.3+:

```bash
# Dry-run first to see what would migrate (no writes)
am migrate from-memory-bus --source=~/.memory-bus/bus.db --dry-run

# Actual migration
am migrate from-memory-bus --source=~/.memory-bus/bus.db --target=~/.astor
```

This:
- Reads old `events` / `memory_candidates` / `memory_canonical` tables
- Maps legacy `status` field (active/contested/archived) → astor `verdict` (settled/contested/thin)
- Creates new rows in `astor_bus.db` (events + candidates + canonical + audit_log)
- Preserves `stable_id` for idempotency (re-running skips already-migrated rows)
- Disables FK during migration (legacy rows may have FK refs to rows that migrate later)
- Embeddings NOT migrated (legacy format uncertain — user can re-embed via `am recall` on demand)

After migration, you have:
```
~/.astor/
├── astor_bus.db    # new events + canonical (with verdict, no embedding BLOB)
├── astor_nest.db   # NEW vector embeddings (1 row per fact, model_name indexed)
└── astor_forge.db  # placeholder; forge is pure-functions module in v0.x
```

### 4. Clean cutover (manual, v1.0+)

**DO NOT delete your legacy source DB directory automatically.** Per Plan § Week 5 step 4.8:

After verifying the migration worked (`am recall` returns expected facts, `am write` works as expected), the user manually cuts over:

```bash
# 1. Verify migration worked
am doctor --schema --memory
# → Memory RSS: ~50 MB
# → Schema: OK
# → Memory: N canonical, N events, N embeddings

# 2. Stop any process reading from memory-bus
# (check open processes: lsof | grep memory_bus OR Get-Process | findstr memory)

# 3. Archive (don't delete) memory-bus legacy
mv <legacy-dir>/memory-bus <legacy-dir>/memory-bus-archived-$(date +%Y-%m-%d)

# 4. Update any code still referencing memory_bus
# (grep your codebase: grep -rln "memory_bus" src/)
# → Replace with astor_memory equivalents (auto_route_write → astor_write, etc.)
```

**v1.0 ships with migration tool but does NOT auto-delete legacy.** User decision required.

Data integrity is verified after migration via row-count comparison.

---

## The 5-step migration

### Step 1: Install Astor-Memory alongside legacy

```bash
# Old system continues running (don't stop it yet)
pip install astor-memory
```

Verify install:

```bash
am --version
# → am 0.1.0 (astor-memory 0.1.0)

am doctor
# → bus: NOT INITIALIZED (expected; not yet running)
# → forge: NOT INITIALIZED
# → nest: NOT INITIALIZED
```

This installs Astor-Memory without affecting the legacy system. Both can run side-by-side.

### Step 2: Initialize Astor-Memory in parallel mode

```bash
am init --parallel --port=7804
```

The `--parallel` flag tells Astor-Memory to use a different port range (7804-7806) than the legacy system (7801-7803). Both systems run concurrently.

You now have:

```
7801 memu_server (legacy)
7802 mempalace_server (legacy)
7803 bus_server (legacy)
7804 astor_forge (new)
7805 astor_nest (new)
7806 astor_bus (new)
```

### Step 3: Bulk import cutover (if you have 10+ import sites)

For projects with 10+ import sites, use `astor-migrate-imports`:

```bash
astor-migrate-imports /path/to/your/codebase \
  --from "from memory_bus import auto_route_v2" \
  --to "from astor_memory import write, read"
```

This rewrites all imports in-place. Review the diff:

```bash
git diff --stat
# → 10 files changed, 23 insertions(+), 23 deletions(-)
```

Verify behavior is identical:

```python
# Before
auto_route_v2.write("test")
hits = auto_route_v2.read_all("test")

# After (rewritten)
write("test")
hits = read("test")
```

For projects with < 10 import sites, manual rewriting is faster.

### Step 4: Migrate data (one-time)

```bash
# Dry-run first
am migrate from-memory-bus --source=~/.memory-bus/bus.db --dry-run

# Actual migration
am migrate from-memory-bus --source=~/.memory-bus/bus.db --target=~/.astor/astor.db
```

Expected output:

```
Reading legacy bus.db...
  → 1,247 events found
Mapping kinds...
  → 1,189 facts (kind=fact)
  → 47 rules (kind=rule)
  → 11 candidates (kind=candidate)
Writing to ~/.astor/astor.db...
  → 1,247 rows inserted
Verifying...
  → row count match: ✓
  → timestamp preservation: ✓
  → reference integrity: ✓
Migration complete.
```

### Step 5: Cut over and verify

Once parallel-run has been stable for 1-2 weeks:

```bash
# Stop legacy services
systemctl stop memory-bus.service
systemctl stop memu-server.service
systemctl stop mempalace-server.service

# Switch Astor-Memory to canonical ports
am config bus.port=7803
am config forge.port=7801
am config nest.port=7802

# Restart Astor-Memory on canonical ports
am serve --detach
```

Verify health:

```bash
am doctor
# → bus: OK (1,247 events migrated)
# → forge: OK (provider=openai, latency=320ms)
# → nest: OK (847 docs indexed, 5 KB vector cache)
```

Run your existing test suite:

```bash
pytest tests/
```

All tests should pass. If any fail, see [Rollback procedure](#rollback-procedure).

---

## Side-by-side reference

### Write

| Old (memory-bus) | New (astor-memory) |
|---|---|
| `auto_route_v2.write("text")` | `write("text")` |
| `auto_route_v2.write("text", kind="fact")` | `write("text", scope="long_term", tier="public")` |
| Returns `None` (fire-and-forget) | Returns `FactId` (immediate; extraction async) |

### Read

| Old (memory-bus) | New (astor-memory) |
|---|---|
| `auto_route_v2.read_all("query")` | `read("query")` |
| `auto_route_v2.read_user("query", user_id="alice")` | `read("query", user_id="alice")` |
| Returns list of dicts (no structure) | Returns list of `Hit` objects with `.content`, `.references`, `.confidence` |

### Config

| Old (memory-bus) | New (astor-memory) |
|---|---|
| `MEMU_URL=http://localhost:7801` | `ASTOR_FORGE_URL=http://localhost:7801` (or legacy `MEMU_URL`) |
| `~/.memory-bus/config.yaml` | `~/.astor/config.yaml` |
| No priority order | CLI flag > env > yaml > defaults |

### Health check

| Old (memory-bus) | New (astor-memory) |
|---|---|
| `curl localhost:7801/health` | `am doctor` |
| `curl localhost:7802/health` | (combined in doctor) |
| `curl localhost:7803/health` | (combined in doctor) |

---

## Rollback procedure

If something goes wrong after cutover, rollback is a 3-step process:

### 1. Stop Astor-Memory

```bash
am serve --stop
```

Or if running in systemd:

```bash
systemctl stop astor-memory.service
```

### 2. Restart legacy services

```bash
systemctl start memory-bus.service
systemctl start memu-server.service
systemctl start mempalace-server.service
```

### 3. Verify legacy is operational

```bash
curl localhost:7801/health
# → {"status": "ok", ...}
```

Your application continues to work because env vars are still set (`MEMU_URL`, `MEMPALACE_URL`, `BUS_URL`) and Astor-Memory's env-var compat kept them aliased.

### Data rollback

If you need to roll back **data** (not just services):

```bash
# Astor-Memory data is in ~/.astor/
# Legacy data is in ~/.memory-bus/ (untouched during parallel-run)

# Switch env vars back
export MEMU_URL=http://localhost:7801
export MEMPALACE_URL=http://localhost:7802
export BUS_URL=http://localhost:7803

# Restart legacy services (above)
```

Since legacy data was never modified during parallel-run, this is safe.

---

## Common pitfalls

### Pitfall 1: Forgetting the env-var compat layer

After `pip install astor-memory`, the env vars still work. But if you explicitly set `ASTOR_FORGE_URL` to a different value than `MEMU_URL`, the env-var compat layer is bypassed.

**Fix**: Either set both to the same value, or set only the new var and unset the old.

### Pitfall 2: Data migration without dry-run

Don't run `am migrate` without `--dry-run` first. The legacy schema has subtle differences (e.g. `kind` taxonomy changed; old `kind="interaction"` maps to new `kind="event"`).

**Fix**: Always dry-run, review the mapping table, then run for real.

### Pitfall 3: Cutting over too fast

Don't cut over after 1 day of parallel-run. Plan for 1-2 weeks minimum. Watch for:
- Latency differences (Astor-Memory should be 10-100x faster in-process; slower if REST-only)
- Citation format changes (any code that string-matches old format breaks)
- Cron jobs that depend on specific ports

**Fix**: Use the 1-2 week parallel-run to observe and adjust.

### Pitfall 4: Import cutover without diff review

`astor-migrate-imports` rewrites 10+ files in one shot. Always review the diff before committing.

**Fix**: Use `--dry-run` flag first to preview the changes, then commit incrementally.

### Pitfall 5: Skipping the doctor check

After migration, `am doctor` is your single source of truth for system health. Don't trust individual `curl localhost:7801/health` calls — use `am doctor` to see all three stores at once.

**Fix**: Add `am doctor` to your monitoring/alerting pipeline.

---

## Next

- [`docs/agent-adapters.md`](./agent-adapters.md) — MCP / LangChain / REST / Python integration
- [`docs/faq.md`](./faq.md) — frequently asked questions
- [`docs/troubleshooting.md`](./troubleshooting.md) — common errors and fixes
- [`docs/contributing.md`](./contributing.md) — for contributors
