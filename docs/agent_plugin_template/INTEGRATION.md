# Agent Plugin Template — Index

This directory ships the **framework-agnostic contract + HTTP client + 1
reference implementation** for integrating [astor-memory](https://github.com/your-org/astor-memory)
as a memory provider in **any** agent framework.

## Layout

```
docs/agent_plugin_template/
├── README.md                          # THIS file (directory index)
├── plugin.yaml                        # framework-agnostic plugin config schema
├── astor_memory_client.py             # framework-agnostic HTTP client (numpy + stdlib)
├── .gitignore                         # excludes __pycache__
└── frameworks/                        # one subdir per supported agent framework
    └── hermes/                        #   hermes-agent plugin (the canonical reference)
        ├── __init__.py                #     MemoryProvider implementation for hermes
        └── plugin.yaml                #     hermes-specific plugin.yaml
```

## Quick start (any framework)

```bash
# 1. Install astor server in its own venv
pip install astor-memory[server]
python -m astor_memory.server   # listens on :7803

# 2. Install only numpy in your agent framework's venv
pip install numpy

# 3. Drop the framework-agnostic client into your framework
cp astor_memory_client.py <your-framework>/plugins/astor/

# 4. Write a 20-50 line adapter that maps your framework's plugin lifecycle
#    onto the MemoryProvider ABC (see README.md in this dir)

# 5. Configure plugin to point at ASTOR_SERVER_URL (default http://127.0.0.1:7803)
```

The HTTP client (`astor_memory_client.py`) is **the only Python file you need
to vendor**. It only requires `numpy` + stdlib. Frameworks should bind to
the `MemoryProvider` ABC (in `README.md`) and delegate to this client.

## Why a framework-agnostic client + framework-specific adapters?

- **astor 独立 venv** carries the server + flask + sentence-transformers + scipy (~500MB).
  We do NOT want to push that into every agent framework.
- **agent framework venv** only needs `numpy` (~30MB) to call the HTTP API.
- **framework-specific adapter** (20-50 lines) implements the framework's
  plugin ABC by delegating to the framework-agnostic client.

This keeps the deployment footprint small and the integration portable.

## Reference implementation: hermes-agent

`frameworks/hermes/` is a complete, working example for the
[hermes-agent](https://github.com/your-org/hermes-agent) framework. To install:

```bash
cp frameworks/hermes/__init__.py ~/.hermes-agent/plugins/memory/astor_memory/__init__.py
cp frameworks/hermes/plugin.yaml ~/.hermes-agent/plugins/memory/astor_memory/plugin.yaml
```

Then in hermes config, set `memory.provider: astor_memory`. The plugin will
call `http://127.0.0.1:7803` to read/write/forget facts.

## Adding support for another agent framework

1. Create `frameworks/<your-framework>/`.
2. Write a thin adapter: implement your framework's plugin ABC by calling
   `astor_memory_client.AstorMemoryProvider` methods. See `README.md`
   for the standard `MemoryProvider` ABC that any framework should support.
3. Document the install + config steps in `frameworks/<your-framework>/README.md`.
4. Submit a PR.

## Required server endpoints

The client expects these on `ASTOR_SERVER_URL` (default `http://127.0.0.1:7803`):

- `GET /v1/health` → `{"status": "ok", "facts": N, "version": "x.y.z"}`
- `POST /v1/read` body `{"query": str, "user": str, "top_k": int}` → `{"results": [...]}`
- `POST /v1/write` body `{"text": str, "tier": str, "user": str}` → `{"fact_ids": [...]}`
- `POST /v1/forget` body `{"fact_id": int}` → `{"forgotten": [...]}`
- `GET /v1/audit/health` → 3-dim memory-native audit (Bannings 2026-08)

All endpoints return JSON. Errors return `{"error": "msg"}` with appropriate HTTP status.

## Versioning

Plugin version **must equal** server version. Bump together. See
`docs/RULES_astor_version_sync.md` for the locked rule. Both this template
and the reference implementations are versioned 1.11.0.

## License

MIT (matches astor-memory)
