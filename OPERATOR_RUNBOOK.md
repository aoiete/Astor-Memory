# Astor-Memory Operator Runbook

This file is for the operator (you, the admin) — not for end users. It
covers the day-to-day operational tasks for keeping an astor-memory
runtime healthy.

## Quick status check

```bash
curl http://127.0.0.1:7803/v1/health
```

Returns a JSON blob with status/version/facts/events/embeddings. If
`status` is `"ok"` and the others are non-zero, the runtime is healthy.

## Where the runtime lives

Default: `~/.astor/` (Linux/macOS) or `%USERPROFILE%\.astor\` (Windows).
Override with `ASTOR_DIR` env var.

```
~/.astor/
├── admin.lock              # first_admin bootstrap token (JSON)
├── bot-binding.db          # platform + user + binding metadata
├── public/memory/
│   ├── astor_bus_public.db
│   ├── astor_forge_public.db
│   └── astor_nest_public.db
├── source/memory/           # admin-only tier
├── audit/astor_audit.db    # cross-tier audit aggregator
├── lex/memory/              # BM25 inverted index (4th DB family)
└── users/<user_id>/memory/  # one 3-store layout per non-admin user
```

## First-time install

```bash
python -m astor_memory.cli.main init
python -m astor_memory.cli.main admin lock --user-id=admin
```

`init` creates the 9-DB layout under `$ASTOR_DIR`. `admin lock` writes
`admin.lock` so subsequent `am` commands recognize you as first_admin.

## Restart a dead runtime

The `MemoryServersWatch` NSSM service (Windows) or systemd unit
(Linux) auto-respawns the server on crash. If the watchdog is also
down, restart manually:

```bash
# Windows
sc query MemoryServersWatch
sc start MemoryServersWatch

# Linux
systemctl --user restart astor-server
```

The watchdog's 15-min cycle polls `/v1/health` and respawns on failure.

## Backup

See [BACKUP_FALLBACK.md](BACKUP_FALLBACK.md) for full procedure.
Quick version:

```bash
sqlite3 ~/.astor/source/memory/astor_bus_source.db ".backup /backup/source-bus.db"
sqlite3 ~/.astor/public/memory/astor_bus_public.db ".backup /backup/public-bus.db"
sqlite3 ~/.astor/audit/astor_audit.db ".backup /backup/audit.db"
# repeat for forge + nest + per-user 3-store layouts + lex
```

`sqlite3 .backup` is safe to run while astor is live.

## Upgrade

```bash
pip install --upgrade astor-memory
# Restart the server (kill -TERM the PID, watchdog respawns it)
```

The runtime is decoupled from the python install — `ASTOR_DIR` data
survives across upgrades. Only the Python-side ddl/migrations need a
restart.

## ACL setup

The first admin is created by `am admin lock`. To add a non-admin user:

```bash
am bot add-user --user-id=alice --short-alias=alice \\
    --role=user --plan=vip --active=1
am bot bind --platform=telegram --account-id=<bot_id> \\
    --chat-id=<chat_id> --user-id=alice --bound-by=first_admin
```

## Source-code is upstream of Runtime

If you edit Python files at runtime (e.g. via hermes agents), changes
land in `$ASTOR_DIR/astor_memory/*.py` (Runtime) but NOT in the source
tree (wherever you cloned the repo to, e.g. `<source_dir>/astor_memory/`).
After fixing a runtime bug, copy the patched file back to source:

```bash
cp $ASTOR_DIR/astor_memory/server.py <source_dir>/astor_memory/server.py
# then commit + push to GitHub
```

## R-class rules (read before any change)

| Rule | Description |
|---|---|
| R218 | Never kill the hermes gateway yourself — user restarts it |
| R252 | Edit source first, then patch Runtime (not the other way) |
| R365b | /v1/forget must check fact ownership; before_request must fall back body.user → body.user_id |
| R380 | "patched + verified" claims must include actual code diff grep, not just functional behavior |