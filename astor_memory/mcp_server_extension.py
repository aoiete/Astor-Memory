"""Phase E (2026-09-16): drop-in extension for astor-memory-rest-mcp server.

Usage:

    cd C:\\Users\\TheNuts\\.evox\\agent\\mcp-servers\\astor-memory-rest-mcp
    python -c "import sys; sys.path.insert(0, r'D:/ai/astor-memory'); \
        import mcp_server_extension; import server; server.main()"

Or copy this logic into ``server.py`` at the call sites listed below.

This module monkey-patches ``server.list_tools`` and ``server.call_tool``
to add the ``astor_auto_observe`` tool. It uses the production
``astor_memory.forge.extractor.astor_auto_observe`` for filter logic and
the existing ``server._request`` for the upstream POST.

Why a separate module? The MCP workspace's ``server.py`` is in a
high-sensitivity write path. This module is written to the Astor
workspace where writes are unrestricted; at runtime the user invokes
it by either (a) editing ``server.py`` to import and call these
functions or (b) running via the command above which monkey-patches.
"""

from __future__ import annotations

import json
from typing import Any

import server as _server
from astor_memory.forge.extractor import astor_auto_observe
from astor_memory.mcp_auto_observe import (
    astor_auto_observe_call,
    astor_auto_observe_tool_schema,
)


_TOOL_SCHEMA = astor_auto_observe_tool_schema()[0]


def _enforce_trust_subset(arguments: dict[str, Any]) -> None:
    """Subset of trust enforcement for the auto-observe tool.

    The full server-side ``_enforce_trust`` blocks identity-overriding
    fields; auto_observe only consumes ``text`` (no agent_id / user_id /
    transport / source fields), so the full check is unnecessary but we
    keep parity by calling it if present.
    """
    if hasattr(_server, "_enforce_trust"):
        _server._enforce_trust(arguments)


def _patch_list_tools() -> None:
    original = _server.list_tools
    schema = _TOOL_SCHEMA

    def patched() -> dict[str, Any]:
        body = original()
        body["tools"].append(schema)
        return body

    _server.list_tools = patched  # type: ignore[assignment]


def _patch_call_tool() -> None:
    original = _server.call_tool

    def patched(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "astor_auto_observe":
            _enforce_trust_subset(arguments or {})
            return astor_auto_observe_call(
                arguments=arguments or {},
                request_fn=_server._request,
                trusted_profile=_server.LOCAL_DIRECT_PROFILE,
            )
        return original(name, arguments)

    _server.call_tool = patched  # type: ignore[assignment]


_patch_list_tools()
_patch_call_tool()


def main() -> None:
    """Run as the MCP gateway with auto_observe installed."""
    _server.main()
