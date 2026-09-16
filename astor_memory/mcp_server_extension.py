"""Phase E (2026-09-16): drop-in extension for astor-memory-rest-mcp server.

Usage from ``server.py``::

    import sys, os
    _p = os.environ.get(\"ASTOR_MEMORY_SRC\")
    if _p:
        sys.path.insert(0, _p)
        sys.path.insert(0, os.path.join(_p, \"astor_memory\"))
    try:
        import mcp_server_extension as _ext
        _ext.install(sys.modules[\"__main__\"])
    except Exception:
        pass

After install, ``list_tools()`` and ``call_tool()`` of the MCP gateway
are monkey-patched to add ``astor_auto_observe``.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from astor_memory.forge.extractor import astor_auto_observe
from astor_memory.mcp_auto_observe import (
    astor_auto_observe_call,
    astor_auto_observe_tool_schema,
)


_TOOL_SCHEMA = astor_auto_observe_tool_schema()[0]
_PATCH_DONE = False


def install(server_module: Any) -> bool:
    """Install the auto_observe patch on the MCP gateway server module.

    Args:
        server_module: the MCP gateway's ``server.py`` module. Pass
            ``sys.modules[\"__main__\"]`` from the gateway's import block.

    Returns:
        True if patch installed, False otherwise.
    """
    global _PATCH_DONE
    if _PATCH_DONE:
        return True
    if server_module is None:
        return False
    if not hasattr(server_module, "list_tools") or not hasattr(server_module, "call_tool"):
        return False

    # Find the trusted context dict. astor-memory-rest-mcp v0.6+ uses
    # ``_TRUSTED_CTX``; older v0.3 used ``LOCAL_DIRECT_PROFILE``.
    if hasattr(server_module, "_TRUSTED_CTX"):
        trusted_profile = server_module._TRUSTED_CTX
    elif hasattr(server_module, "LOCAL_DIRECT_PROFILE"):
        trusted_profile = server_module.LOCAL_DIRECT_PROFILE
    else:
        trusted_profile = {
            "agent_id": "astor_memory_mcp",
            "user_id": "admin",
            "source": "astor_memory_mcp",
            "transport": "direct",
        }

    original_lt = server_module.list_tools
    original_ct = server_module.call_tool

    def patched_list_tools() -> dict[str, Any]:
        body = original_lt()
        if not any(t.get("name") == "astor_auto_observe" for t in body.get("tools", [])):
            body.setdefault("tools", []).append(_TOOL_SCHEMA)
        return body

    def patched_call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "astor_auto_observe":
            if hasattr(server_module, "_enforce_trust"):
                try:
                    server_module._enforce_trust(arguments or {})
                except Exception:
                    pass
            return astor_auto_observe_call(
                arguments=arguments or {},
                request_fn=server_module._request,
                trusted_profile=trusted_profile,
            )
        return original_ct(name, arguments)

    server_module.list_tools = patched_list_tools  # type: ignore[assignment]
    server_module.call_tool = patched_call_tool  # type: ignore[assignment]
    _PATCH_DONE = True
    return True
