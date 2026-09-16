# Phase E MCP server.py Patch (2026-09-16)

EvoX Desktop stores its MCP server at
`C:\Users\TheNuts\.evox\agent\mcp-servers\astor-memory-rest-mcp\server.py`.
That workspace is **not a git repository**, so the Phase E change
needs to be shipped out-of-band. This patch captures exactly what was
added to that file. Apply with `git apply`, or by hand at the
locations marked below.

## 1. Add 8 lines at the very top (after the docstring)

Just after the closing `"""` of the module docstring, **before** any
other code, add:

```python
from __future__ import annotations

import json
import os
import sys
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Phase E (2026-09-16): when ASTOR_MEMORY_SRC is set, import the
# monkey-patching extension that adds ``astor_auto_observe`` and exposes
# the source repo to the gateway. This keeps the high-sensitivity
# server.py untouched in spirit while allowing new tools without a
# full server.py rewrite.
import os as _os_e
_sys_path_e = _os_e.environ.get("ASTOR_MEMORY_SRC")
if _sys_path_e:
    import sys as _sys_e
    # The extension file lives at <ASTOR_MEMORY_SRC>/astor_memory/mcp_server_extension.py
    # so we need both the source root AND its astor_memory subpackage on sys.path.
    for _p in (_sys_path_e, os.path.join(_sys_path_e, "astor_memory")):
        if _p and _p not in _sys_e.path:
            _sys_e.path.insert(0, _p)
    try:
        import mcp_server_extension as _mcp_ext_e  # noqa: F401
        # Phase E: explicitly pass the MCP gateway module so the extension
        # doesn't accidentally patch ``astor_memory.server``.
        # When the script is invoked as ``python server.py``, ``__main__``
        # is the server module itself. When invoked via ``python -c`` or
        # ``import server``, ``__main__`` is the test driver and lacks
        # ``list_tools``. Probe both names; prefer whichever has the gateway
        # function.
        _gw = None
        for _name in ("__main__", "server"):
            _mod = sys.modules.get(_name)
            if _mod is not None and hasattr(_mod, "list_tools") and hasattr(_mod, "call_tool"):
                _gw = _mod
                break
        _mcp_ext_e.install(_gw)
    except Exception as _e_exc:
        # If the extension can't be imported (e.g. ASTOR_MEMORY_SRC
        # not on disk), the gateway silently falls back to its
        # built-in tools only.
        sys.stderr.write(f"[mcp_server] extension load failed: {_e_exc}\n")
del _os_e, _sys_path_e
try:
    del _mcp_ext_e
except NameError:
    pass
```

**Important**: this block must come **after** the docstring `"""` and
**before** any `from __future__` import. Place it directly under the
docstring.

## 2. Wrap `list_tools()` body with a deferred-install call

Find `def list_tools() -> dict[str, Any]:` near line 270 of
server.py. Replace the body's first line (currently empty or
docstring) with:

```python
def list_tools() -> dict[str, Any]:
    # Phase E (2026-09-16): re-attempt install now that list_tools is defined.
    try:
        import mcp_server_extension as _mcp_ext_e_rt  # noqa: F401
        _gw = None
        for _name in ("__main__", "server"):
            _mod = sys.modules.get(_name)
            if _mod is not None and hasattr(_mod, "list_tools") and hasattr(_mod, "call_tool"):
                _gw = _mod
                break
        _mcp_ext_e_rt.install(_gw)
    except Exception:
        pass
    return {
        "tools": [
```

This guarantees the patch is applied on the first `tools/list` call,
which lets the import block above succeed even when the patch was
imported before `list_tools` was defined.

## 3. Verify

Set the env vars before launching the MCP server:

```bash
export ASTOR_DIR='D:\AI\Astor-Memory-Runtime'
export ASTOR_MEMORY_SRC='D:\ai\astor-memory'

# Per-agent identity — each agent framework sets its own:
#   EvoX  desktop  → ASTOR_MEMORY_AGENT_ID=evox
#   Claude        → ASTOR_MEMORY_AGENT_ID=claude
#   Cursor        → ASTOR_MEMORY_AGENT_ID=cursor
#   (default if unset → astor_memory_mcp)
export ASTOR_MEMORY_AGENT_ID=evox

# Per-user identity (optional override, default = admin):
# export ASTOR_MEMORY_USER_ID=aoiete

python server.py
```

Then:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | python server.py
```

The second `tools/list` call must show **9 tools** including
`astor_auto_observe`. If only 8, the deferred patch install has not
fired — recheck the import order.

Verify that Astor sees the per-agent identity:

```bash
sqlite3 $ASTOR_DIR/public/memory/astor_bus_public.db \
  "SELECT agent_id, source, count(*) FROM events
   WHERE source LIKE '%auto_observe%' OR source = 'hermes.astor_auto_observe'
   GROUP BY agent_id, source"
```

Expected after EvoX + Hermes have run their respective auto-observe
hooks: distinct `agent_id="evox"` and `agent_id="astor_memory_adapter"`
(Hermes adapter's agent_id, distinct from EvoX's).

## Files referenced

- `astor_memory/mcp_server_extension.py` — the actual monkey-patching
  module (committed at `3eeab4b`).
- `astor_memory/mcp_auto_observe.py` — the pure-logic tool handler. **The
  `/v1/write` body MUST include `transport: "direct"` or Astor's
  `_resolve_agent_context` falls back to `agent_id='rest_api'`** (fixed
  in commit `dcb895e`).
- `astor_memory/forge/extractor.py` — provides
  `astor_auto_observe(text, agent_id, user_id, namespace)`.
- `docs/auto-memory.md` — full Phase E spec and integration guide.
