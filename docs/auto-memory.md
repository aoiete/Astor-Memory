# Phase E Auto-Memory (2026-09-16)

Astor is now the **central auto-memory backend** for any agent framework
(Hermes, EvoX, OpenClaw, CLI, ...). Each agent emits raw turn text;
Astor's server-side hook chain (`capture_intent` → `astor_classify_outcome`
→ `forge.extractor` → `publishable`) handles noise filtering, outcome
classification, fact extraction and tier routing.

## What's new

### Astor-side (`D:\ai\astor-memory`)

| File | Change |
|---|---|
| `astor_memory/forge/extractor.py` | Added `MIN_AUTO_OBSERVE_LENGTH=30`, `NOISE_PREFIXES`, `astor_should_skip_auto_observe`, `_score_importance`, `_pick_tier`, `astor_auto_observe` |
| `astor_memory/hermes_adapter.py` | `sync_turn` now calls `astor_auto_observe` after audit logging; observation failure does NOT break audit |
| `astor_memory/mcp_auto_observe.py` | NEW — Pure-logic tool handler for MCP gateway (no I/O deps) |
| `astor_memory/mcp_server_extension.py` | NEW — Monkey-patches `list_tools`/`call_tool` for the MCP gateway |
| `tests/test_auto_observe.py` | NEW — 10 tests for noise/outcome/tier routing |
| `tests/test_hermes_auto_observe.py` | NEW — 4 tests for Hermes auto-observe hook |
| `tests/test_mcp_auto_observe.py` | NEW — 5 tests for the MCP tool handler |
| `docs/auto-memory.md` | NEW — This file |

Tests:

```text
54 passed (Astor + MCP auto_observe suites)
```

### Noise filter rules (`astor_should_skip_auto_observe`)

| Reason | When |
|---|---|
| `empty` | text is empty or whitespace-only |
| `too_short` | len(text) < 30 chars |
| `noise_prefix` | first whitespace-separated token or first 2 chars is in NOISE_PREFIXES |
| `low_signal` | not in above + outcome=neutral + importance < 0.5 |
| `no_agent_id` | agent_id is None or empty |

`NOISE_PREFIXES = ("嗯", "ok", "好", "好的", "thanks", "谢谢", "hi", "hello", "👍", "你好")`

### Outcome / importance / tier routing (`astor_auto_observe`)

| outcome | importance | tier |
|---|---|---|
| failure / lesson | 0.85 | source (admin-only) |
| success + capture_intent | 0.8 | public |
| success, long (>200) | 0.6 | public |
| success, medium (60-200) | 0.4 | None (skip) |
| neutral, long (>200) | 0.6 | public |
| neutral, medium (60-200) | 0.5 | public |
| neutral, short | 0.3 | None (skip) |

### Identity contract (unchanged)

```text
agent_id   = astor_memory_mcp
user_id    = admin
transport  = direct
source     = astor_memory_mcp
```

## How agents integrate

Any agent framework (Hermes, EvoX, OpenClaw, CLI) should:

1. After every turn, concatenate user + assistant text.
2. Call `astor_auto_observe(text, agent_id, user_id, namespace)` (or the MCP tool `astor_auto_observe` for MCP-based agents).
3. If `observed=True` and `tier` is set, POST to `http://127.0.0.1:7803/v1/write` with that tier.
4. If `observed=False`, do nothing.

That's it. The agent does not classify, decide tier, or filter noise — Astor does.

### Hermes integration

`astor_memory/hermes_adapter.py::sync_turn()` now calls `astor_auto_observe` automatically after audit logging. No changes needed in Hermes itself.

### EvoX / MCP integration

MCP gateway exposes `astor_auto_observe` tool. The plugin invokes after each turn:

```json
{
  "tool": "astor_auto_observe",
  "text": "...",
  "session_id": "..."
}
```

## Manual patch steps (EvoX Desktop high-sensitivity path blocked writes)

The MCP workspace's `server.py` is in a high-sensitivity path that
EvoX Desktop blocks writes to without explicit user confirmation. The
Astor-side hook code is already complete and tested (54 tests pass).
The MCP-side and runtime patches need manual application.

### Step 1: patch MCP `server.py`

Open
`C:\Users\TheNuts\.evox\agent\mcp-servers\astor-memory-rest-mcp\server.py`
and add at the top (after the imports):

```python
import os as _os
_sys_path = _os.environ.get("ASTOR_MEMORY_SRC")
if _sys_path:
    import sys as _sys
    if _sys_path not in _sys.path:
        _sys.path.insert(0, _sys_path)
try:
    import mcp_server_extension  # noqa: F401
except Exception:
    pass
```

Then before running the MCP server, set:

```text
ASTOR_MEMORY_SRC=D:\ai\astor-memory
```

This monkey-patches `list_tools()` and `call_tool()` to add the
`astor_auto_observe` tool without touching the rest of `server.py`.

### Step 2: patch Astor runtime

```bash
cp -f /d/ai/astor-memory/astor_memory/forge/extractor.py \
      "/d/AI/Astor-Memory-Runtime/astor_memory/forge/extractor.py"

cp -f /d/ai/astor-memory/astor_memory/hermes_adapter.py \
      "/d/AI/Astor-Memory-Runtime/astor_memory/hermes_adapter.py"

find "/d/AI/Astor-Memory-Runtime/astor_memory" -name __pycache__ -type d -exec rm -rf {} +
am server stop && am server start
```

Verify:

```bash
curl -m 5 -sS http://127.0.0.1:7803/v1/health
```

### Step 3: verify auto-memory works end-to-end

```bash
cd /c/Users/TheNuts/.evox/agent/mcp-servers/astor-memory-rest-mcp
ASTOR_DIR='D:\AI\Astor-Memory-Runtime' python -c "
import sys
sys.path.insert(0, r'D:\ai\astor-memory')
import mcp_server_extension
import server, json
resp = server.handle({'jsonrpc':'2.0','id':1,'method':'tools/call',
                     'params':{'name':'astor_auto_observe',
                               'arguments':{'text':'remember this: user prefers dark mode for terminal sessions'}}})
print(json.dumps(resp, ensure_ascii=False))
"
```

Expected: `observed=true, persisted=true, tier=public, outcome=success`.

### Step 4: leak audit

```bash
sqlite3 /d/AI/Astor-Memory-Runtime/public/memory/astor_bus_public.db \
  "SELECT agent_id, source, substr(content,1,60) FROM events ORDER BY id DESC LIMIT 10;"
sqlite3 /d/AI/Astor-Memory-Runtime/public/memory/astor_canonical_public.db \
  "SELECT count(*) FROM memory_canonical WHERE content LIKE '%token%' OR content LIKE '%password%' OR content LIKE '%secret%';"
```

Expected: only known agents, second query returns 0.

## Disable auto-memory per agent

Set `ASTOR_AUTO_MEMORY_ENABLED=0` in the agent's environment to skip
`astor_auto_observe` entirely.
