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
are monkey-patched to add ``astor_auto_observe`` (Phase E) and
``astor_lock_rules`` (Phase C-D).
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from astor_memory.forge.extractor import astor_auto_observe
from astor_memory.mcp_auto_observe import (
    astor_auto_observe_call,
    astor_auto_observe_tool_schema,
)


_TOOL_SCHEMA = astor_auto_observe_tool_schema()[0]

# Phase C-D: astor_lock_rules tool + 5-minute in-memory cache so a single
# handshake can prefetch current LOCK rules without hammering /v1/read/multi.
_LOCK_RULES_TOOL_SCHEMA: dict[str, Any] = {
    "name": "astor_lock_rules",
    "description": (
        "Phase C-D (2026-09-17): return the currently effective LOCK rules. "
        "Cached server-side for 5 minutes; pass refresh=true to bypass cache. "
        "Returns {lock_rules: [...], cache_miss: bool, fetched_at: float}. "
        "Empty list with cache_miss=true means the upstream fetch failed and "
        "the caller should not assume any rule is in force."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "refresh": {
                "type": "boolean",
                "default": False,
                "description": "When true, bypass the 5-minute cache.",
            },
        },
    },
}

_LOCK_CACHE_TTL_SECONDS = 300
_LOCK_CACHE: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}

_PATCH_DONE = False


def _fetch_lock_rules(request_fn, user_id: str, agent_id: str) -> list[dict[str, Any]]:
    """Fetch LOCK-tagged rules from the upstream REST API.

    Returns a list of dicts (each = one LOCK fact). On any error returns [].
    The caller is responsible for marking cache_miss so an empty result may
    mean "fetch failed", not "no rules in force".
    """
    try:
        # astor-memory-rest-mcp signature: request_fn(path, params, body)
        body = {"tag": "LOCK", "limit": 200, "user": user_id}
        result = request_fn("v1/read/multi", None, body)
    except Exception:
        return []
    if not isinstance(result, dict):
        return []
    rules = result.get("facts") or result.get("records") or result.get("items") or []
    if not isinstance(rules, list):
        return []
    return [r for r in rules if isinstance(r, dict)]


def astor_lock_rules_call(
    arguments: dict[str, Any],
    *,
    request_fn,
    trusted_profile: dict[str, Any],
) -> dict[str, Any]:
    """Pure handler for the astor_lock_rules tool.

    Args:
        arguments: tool arguments (refresh?: bool)
        request_fn: callable ``request_fn(path, params, body)`` that hits
                     upstream REST and returns parsed JSON.
        trusted_profile: dict containing ``user_id`` / ``agent_id``.
    """
    refresh = bool((arguments or {}).get("refresh"))
    user_id = str(trusted_profile.get("user_id", "admin"))
    agent_id = str(trusted_profile.get("agent_id", "astor_memory_mcp"))
    cache_key = (user_id, agent_id)
    now = time.monotonic()

    cached = _LOCK_CACHE.get(cache_key)
    if not refresh and cached is not None:
        cached_at, cached_rules = cached
        if now - cached_at < _LOCK_CACHE_TTL_SECONDS:
            return {
                "content": [
                    {"type": "text",
                     "text": json.dumps({
                         "lock_rules": cached_rules,
                         "cache_miss": False,
                         "fetched_at": cached_at,
                         "ttl_seconds": _LOCK_CACHE_TTL_SECONDS,
                     }, ensure_ascii=False)}
                ]
            }

    rules = _fetch_lock_rules(request_fn, user_id, agent_id)
    cache_miss = not rules  # empty fetch => upstream failed, mark so
    _LOCK_CACHE[cache_key] = (now, rules)
    return {
        "content": [
            {"type": "text",
             "text": json.dumps({
                 "lock_rules": rules,
                 "cache_miss": cache_miss,
                 "fetched_at": now,
                 "ttl_seconds": _LOCK_CACHE_TTL_SECONDS,
             }, ensure_ascii=False)}
        ]
    }


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
        tools = body.setdefault("tools", [])
        if not any(t.get("name") == "astor_auto_observe" for t in tools):
            tools.append(_TOOL_SCHEMA)
        if not any(t.get("name") == "astor_lock_rules" for t in tools):
            tools.append(_LOCK_RULES_TOOL_SCHEMA)
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
        if name == "astor_lock_rules":
            return astor_lock_rules_call(
                arguments=arguments or {},
                request_fn=server_module._request,
                trusted_profile=trusted_profile,
            )
        return original_ct(name, arguments)

    server_module.list_tools = patched_list_tools  # type: ignore[assignment]
    server_module.call_tool = patched_call_tool  # type: ignore[assignment]
    _PATCH_DONE = True
    return True
