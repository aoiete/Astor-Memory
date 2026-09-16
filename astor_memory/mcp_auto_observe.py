"""Phase E (2026-09-16): server-side auto-memory hook tool handler.

Used by ``astor-memory-rest-mcp`` (MCP gateway) when invoked from any
agent framework (EvoX, Hermes, Claude Desktop, Cursor, Aider, etc.).

Drop-in tool handler that runs ``astor_auto_observe`` semantics locally
and posts the result to Astor REST. Designed to be imported from
``server.py`` without modifying the high-sensitivity path's existing
``list_tools()`` / ``call_tool()`` registrations directly.

Usage from ``C:\\Users\\TheNuts\\.evox\\agent\\mcp-servers\\astor-memory-rest-mcp\\server.py``::

    from astor_memory.mcp_auto_observe import (
        astor_auto_observe_tool_schema,
        astor_auto_observe_call,
    )

    # in list_tools():
    tools.extend(astor_auto_observe_tool_schema())

    # in call_tool():
    if name == "astor_auto_observe":
        return astor_auto_observe_call(arguments)
"""

from __future__ import annotations

import json
from typing import Any

from astor_memory.forge.extractor import astor_auto_observe


def astor_auto_observe_tool_schema() -> list[dict[str, Any]]:
    return [
        {
            "name": "astor_auto_observe",
            "description": (
                "Phase E (2026-09-16): server-side auto-memory hook. "
                "Caller passes a turn's text; the server runs the noise filter, "
                "outcome classifier and tier routing locally and forwards the "
                "result. text <30 chars or pure noise returns observed=false "
                "with a skipped_reason. Other turns are persisted to astor via "
                "/v1/write with tier=auto (or public+mode=none fallback)."
            ),
            "inputSchema": {
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {
                        "type": "string",
                        "minLength": 30,
                        "description": "Turn content (user + assistant concatenated).",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Optional session id; default 'unknown'.",
                    },
                    "namespace": {
                        "type": "string",
                        "description": "Optional namespace override.",
                    },
                },
            },
        }
    ]


def astor_auto_observe_call(
    arguments: dict[str, Any],
    *,
    request_fn,
    trusted_profile: dict[str, Any],
) -> dict[str, Any]:
    """Pure logic for the auto-observe tool. No I/O dependency on server.py.

    Args:
        arguments: tool arguments (text, session_id?, namespace?)
        request_fn: callable ``request_fn(method, path, params, body)`` that
                     performs the upstream POST /v1/write and returns parsed
                     JSON or raises ``RuntimeError`` with an ASTOR_UPSTREAM_*
                     prefix.
        trusted_profile: dict containing at least
                         ``user_id``, ``agent_id``, ``source``.
    """
    text = (arguments.get("text") or "").strip()
    if not text:
        raise ValueError("ASTOR_IDENTITY_CONFLICT: text must be a non-empty string")
    if len(text) < 30:
        raise ValueError("ASTOR_AUTO_OBSERVE: text too short (min 30 chars)")

    agent_id = trusted_profile.get("agent_id", "astor_memory_mcp")
    user_id = trusted_profile.get("user_id", "admin")
    source = trusted_profile.get("source", "astor_memory_mcp")

    observe = astor_auto_observe(
        text=text,
        agent_id=agent_id,
        user_id=user_id,
        namespace=arguments.get("namespace")
            or f"mcp/{arguments.get('session_id', 'unknown')}",
    )
    if not observe.get("observed"):
        return {
            "content": [
                {"type": "text",
                 "text": json.dumps({
                     "observed": False,
                     "skipped_reason": observe.get("skipped_reason", "low_signal"),
                     "outcome": observe.get("outcome", "neutral"),
                     "importance": observe.get("importance", 0.0),
                 }, ensure_ascii=False)}
            ]
        }

    tier = observe["tier"] or "public"
    body = {
        "text": text,
        "user": user_id,
        "tier": tier,
        "agent_id": agent_id,
        "source": source,
        "namespace": observe.get("namespace")
            or arguments.get("namespace")
            or f"mcp/{arguments.get('session_id', 'unknown')}",
        "mode": "none",
        "importance": observe["importance"],
    }
    try:
        # v0.6+ astor-memory-rest-mcp signature: _request(path, params, body)
        result = request_fn("v1/write", None, body)
    except RuntimeError as exc:
        if "ASTOR_UPSTREAM_HTTP_400" in str(exc):
            body["tier"] = "public"
            body.pop("importance", None)
            try:
                result = request_fn("v1/write", None, body)
            except RuntimeError as exc2:
                return {
                    "content": [
                        {"type": "text",
                         "text": json.dumps({
                             "observed": True,
                             "persisted": False,
                             "reason": str(exc2),
                         }, ensure_ascii=False)}
                    ]
                }
        else:
            return {
                "content": [
                    {"type": "text",
                     "text": json.dumps({
                         "observed": True,
                         "persisted": False,
                         "reason": str(exc),
                     }, ensure_ascii=False)}
                ]
            }
    return {
        "content": [
            {"type": "text",
             "text": json.dumps({
                 "observed": True,
                 "persisted": True,
                 "tier": tier,
                 "outcome": observe["outcome"],
                 "importance": observe["importance"],
                 "result": result,
             }, ensure_ascii=False)}
        ]
    }
