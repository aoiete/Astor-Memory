# Troubleshooting

> Common errors and fixes for Astor-Memory.

If your error isn't here, file an issue at https://github.com/flopworld/astor-memory/issues with:
- Output of `am doctor --verbose`
- Output of `am --version`
- Reproducible steps

---

## Table of contents

1. [Installation errors](#installation-errors)
2. [Initialization errors](#initialization-errors)
3. [Write errors](#write-errors)
4. [Read errors](#read-errors)
5. [Performance issues](#performance-issues)
6. [LLM provider errors](#llm-provider-errors)
7. [Migration errors](#migration-errors)
8. [Diagnostic commands](#diagnostic-commands)

---

## Installation errors

### `pip install astor-memory` fails with "No matching distribution"

Python version too old. Astor-Memory requires Python ≥ 3.10.

```bash
python --version  # check current version
python3.10 -m venv ~/.astor-venv
source ~/.astor-venv/bin/activate
pip install astor-memory
```

### `ImportError: No module named 'fastembed'` after install

Optional dependency not installed. The base install pulls `fastembed` for embeddings; if you used `--no-deps`, you need to install it manually.

```bash
pip install fastembed
```

### `OSError: [WinError 5] Access is denied` on Windows

File lock. Close any process that has `~/.astor/*.db` open (e.g. another terminal, a SQLite browser).

```powershell
# Find what's locking
Get-Process | Where-Object {$_.Path -like "*astor*"}

# Kill it
Stop-Process -Id <PID> -Force
```

### Install fails on Linux with "missing wheel for fastembed"

fastembed requires a C compiler for some platforms. Install build tools:

```bash
# Ubuntu/Debian
sudo apt install build-essential python3-dev

# Fedora
sudo dnf install gcc python3-devel
```

---

## Initialization errors

### `am init` fails with "Permission denied"

Default path `~/.astor/` not writable. Either:

```bash
# Option 1: fix permissions
chmod 700 ~/
mkdir ~/.astor
chmod 700 ~/.astor

# Option 2: use custom path
export ASTOR_HOME=/path/to/writable/dir
am init
```

### `am init` fails with "Database is locked"

Another `am` process is running. Find and stop it:

```bash
# Unix
ps aux | grep "am " | grep -v grep

# Windows
Get-Process | Where-Object {$_.ProcessName -like "*am*"}
```

### `am init` succeeds but `am doctor` shows "bus: NOT INITIALIZED"

DB files created but not initialized (no schema applied). Run:

```bash
am init --force
```

This re-runs schema migration. Existing data is preserved.

---

## Write errors

### `MemoryWriteError: write failed`

Generic write failure. Run with verbose to see why:

```bash
am write "test" --verbose
```

Common causes:
- Disk full
- SQLite DB corrupted (see below)
- LLM provider timeout

### `sqlite3.OperationalError: database is locked`

Concurrent write contention. Astor-Memory serializes writes via SQLite WAL mode; this error means another process has a write transaction open.

Fix:
```bash
# Check for stuck processes
am doctor --verbose  # shows active transactions
```

If a previous `am write` was killed mid-write, the DB may be in a locked state. Restart:

```bash
am serve --restart  # if running as daemon
# or: kill all am processes and restart
```

### `MemoryWriteError: tier 'private' requires user_id`

You wrote to `tier=private` without specifying which user. Either:

```python
write("alice's preference", tier="private", user_id="alice")
```

Or change the default tier:

```bash
am config tiers.default=public
```

---

## Read errors

### `read()` returns empty list when I know the fact exists

Four common causes:

1. **Wrong tier**: the fact is in `tier=private` but you're reading `tier=public`. Specify:
   ```python
   hits = read("query", tier="private", user_id="alice")
   ```

2. **Wrong user_id**: in multi-user mode, you must specify which user's private DB to read.

3. **Score below threshold**: default threshold is 0.3. Lower it:
   ```python
   hits = read("query", min_score=0.1)
   ```

4. **Decayed out**: if the fact was old + rarely accessed, decay may have lowered its score below threshold. Try `read("query", bypass_decay=True)`.

### `read()` is slow (>100 ms for 5 K docs)

Three causes:

1. **High top_k**: `read("query", top_k=100)` is 20x slower than `top_k=5`. Use the smallest `top_k` that meets your need.

2. **Cold cache**: first query after restart is slower. Subsequent queries hit NumPy cache.

3. **Large `astor_nest.db`**: > 50 K docs triggers brute-force scaling. Run `am compact` to merge near-duplicates, or upgrade to v2.0 for HNSW.

### Citations don't verify (`am verify <ref>` returns `valid: false`)

The fact was updated (new `revision_id`) or pruned. Either:

- Use `read()` to get current `references` (latest revision)
- Increase retention: `am config bus.retention_days=365`

---

## Performance issues

### `am doctor` shows slow forge (latency > 2 seconds)

LLM provider is slow. Either:

1. Switch provider: `am config llm.provider=anthropic` (or whatever's faster in your region)
2. Use local: `am config llm.provider=ollama` (requires Ollama running locally)
3. Use faster model: `am config llm.model=gpt-4o-mini` (vs `gpt-4`)

### Disk usage growing unbounded

Default TTL is 90 days for events. If you have lots of churn, prune:

```bash
am compact --prune-events-older-than-days=30
```

Or set in config:

```yaml
# ~/.astor/config.yaml
bus:
  retention_days: 30
lifecycle:
  auto_compact: true  # nightly cron
```

### `astor_nest.db` getting too large (> 1 GB)

You have many near-duplicate facts. Run merge:

```bash
am compact --merge-threshold=0.85
```

This consolidates cosine-similar facts. Expect 30-50% size reduction.

---

## LLM provider errors

### `AuthenticationError: invalid api_key`

API key not set or wrong. Check:

```bash
echo $OPENAI_API_KEY  # should print a key starting with sk-
```

If empty, set:

```bash
export OPENAI_API_KEY=sk-...
am config llm.provider=openai
```

Astor-Memory reads from env vars: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `ZHIPU_API_KEY`, `OLLAMA_HOST`.

### `RateLimitError: rate limit exceeded`

Provider rate limit hit. Two fixes:

1. **Use a different provider**: `am config llm.provider=deepseek` (often higher limits)
2. **Throttle writes**: `am config forge.max_concurrent=2` (vs default 10)

### `TimeoutError: forge took too long`

LLM call > 30 seconds. Either:

- Increase timeout: `am config forge.timeout_seconds=60`
- Switch to faster model: `am config llm.model=gpt-4o-mini`

---

## Migration errors

### `am migrate from-memory-bus` fails with "schema version mismatch"

Your legacy `bus.db` is from a version before schema v2. Upgrade legacy first:

```bash
# Run legacy's migration tool first
memory-bus migrate --to=v2
# Then re-run Astor-Memory migration
am migrate from-memory-bus --source=~/.memory-bus/bus.db
```

### Row count mismatch after migration

Some rows were skipped due to schema differences. Run with verbose:

```bash
am migrate from-memory-bus --source=... --target=... --verbose
```

Look for "skipped" lines. Common reasons:
- Events with unknown `kind` (legacy had 47 kinds; Astor-Memory has 12)
- Events with malformed references
- Events from before 2024 (timestamp format change)

To include all, force:

```bash
am migrate from-memory-bus --source=... --target=... --include-skipped
```

### Import cutover broke my code

`astor-migrate-imports` rewrote imports but your code used advanced features (e.g. `auto_route_v2.write_async` doesn't have a 1:1 mapping).

Check [`docs/migration.md`](./migration.md#side-by-side-reference) for the full mapping table. Common gotchas:

| Old | New |
|---|---|
| `auto_route_v2.write_async(...)` | `await write_async(...)` (now an `async def`) |
| `auto_route_v2.read_user(query, user_id)` | `read(query, user_id=user_id)` |
| `auto_route_v2.delete(fact_id)` | `am.delete(fact_id)` (CLI; no Python API for delete in v1.0 — soft-delete via new revision only) |

---

## ACL & permission errors

### `403 cross_user_forbidden` on a private-tier read/write

You're trying to access another user's private DB. By design:

- **Regular `user` role** can only access their own `private_<self>`.
  Cross-user reads/writes are denied.
- **`admin` role** (power user per plan §2624) can cross-read for support
  purposes. Cross-writes are also allowed but you should write an
  `astor_audit` row documenting the moderation event.
- **`first_admin` role** (the system root, user_id='admin' alias) can
  access any user's private DB.

If you got `403` and believe you should have access, check:

```bash
# What role is your user?
am platform list-users  # → look up your user_id's role
```

If you're missing an admin role, ask the first_admin to promote you:

```bash
am bot promote <your_user_id> --to=admin  # first_admin only
```

### `403 permission_denied` after working fine for days

This is `astor_check_*` failing from inside a route handler. Either:

- The route is being called with a stale `body.user` (some upstream code
  cached a default value)
- The user's `bot-binding.db user_meta.role` was changed and is now denied
- A tier mismatch: you're calling with `tier='private'` from a request
  that should be `tier='public'`

Check the server log:

```bash
tail -50 ~/.astor/logs/astor_server.log  # Linux/Mac
# or
Get-Content <runtime_dir>logs\astor_server.log -Tail 50  # PowerShell
```

The `astor_check_*` failure detail includes the actor, role, tier, and
target user_id — that tells you which constraint fired.

### `sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread` (Flask server only)

You're running v1.1.0 or earlier. Upgrade to **v1.1.1 or later** — the
fix adds `check_same_thread=False` to `bot_binding._connect()` because
Flask is multi-threaded and the ACL `before_request` hook now reads
`bot-binding.db` from worker threads.

Quick check:

```bash
am --version  # should print 1.1.1 or later
```

If you can't upgrade immediately, run the Flask server in single-threaded
mode:

```bash
flask --app astor_memory.server run --port 7803 --without-threads
```

### Health endpoint returns `403`

`/v1/health` should always return 200 for status monitoring. If you're
seeing 403 after the v1.1.1 ACL fix, restart the server so the new
`before_request` code is loaded — older worker threads may still have
the old ACL bind.

```bash
python <runtime_dir>restart.py restart
```

---



### `am doctor` — overall health

```bash
am doctor
```

Output:

```
bus:   OK (1,247 events, 12 MB)
forge: OK (provider=openai, latency=320ms)
nest:  OK (847 docs, 5 KB vector cache)
```

Verbose:

```bash
am doctor --verbose
```

Output includes:
- DB file paths and sizes
- Active LLM connection status
- Recent error count from logs

### `am doctor --repair` — auto-fix common issues

```bash
am doctor --repair
```

Auto-fixes:
- Missing indexes (rebuilds)
- Locked DBs (clears lock files)
- Stale `astor_forge.db` (clears cache; safe)

Does NOT auto-fix:
- Schema corruption (requires manual intervention)
- Provider auth failures (user must fix env vars)

### `am inspect <reference>` — debug a specific fact

```bash
am inspect f_8a3b2c1d:rev_2
```

Output:

```
fact_id: f_8a3b2c1d
revision_id: 2
content: "user prefers concise replies"
scope: long_term
tier: public
created_at: 2026-07-15T10:23:45Z
updated_at: 2026-08-01T14:11:22Z
access_count: 17
references: [f_3e1b2f9c, f_7c4d9e0a]
parent_revision: f_8a3b2c1d:rev_1
```

### `am logs` — recent activity

```bash
am logs --tail=100
```

Output: last 100 log lines (combined bus + forge + nest).

Filter:

```bash
am logs --level=error --tail=20
am logs --since=1h
```

### `am benchmark` — performance check

```bash
am benchmark --read-queries=100 --write-events=50
```

Output: throughput numbers (queries/second, writes/second, p50/p95/p99 latency).

Use to compare before/after config changes.

---

## Next

- [`docs/faq.md`](./faq.md) — frequently asked questions
- [`docs/contributing.md`](./contributing.md) — for contributors
- [`README.md`](../README.md) — quickstart and overview
