"""Phase A unit tests for ``astor_memory.agent_identity``.

Verifies:
- AstorRequestContext dataclass is well-formed.
- ``parse_local_direct_profile`` defaults match the MCP local-direct contract.
- ``parse_local_direct_profile`` rejects non-empty platform/platform_id/chat_id.
- ``context_to_request_body`` only forwards non-empty fields and includes request_id.
"""

from __future__ import annotations

import pytest

from astor_memory.agent_identity import (
    AstorRequestContext,
    context_to_request_body,
    parse_local_direct_profile,
)


def test_default_profile_matches_astor_mcp_gateway():
    ctx = parse_local_direct_profile()
    assert ctx.agent_id == "astor_memory_mcp"
    assert ctx.user_id == "admin"
    assert ctx.transport == "direct"
    assert ctx.source == "astor_memory_mcp"
    assert ctx.enabled is True
    assert ctx.platform is None
    assert ctx.platform_id is None
    assert ctx.chat_id is None


def test_profile_overrides_keep_transport_direct():
    ctx = parse_local_direct_profile({"user_id": "alice", "agent_id": "alice-cli"})
    assert ctx.agent_id == "alice-cli"
    assert ctx.user_id == "alice"
    assert ctx.transport == "direct"


def test_rejects_platform_field():
    with pytest.raises(ValueError, match="platform"):
        parse_local_direct_profile({"platform": "telegram"})


def test_rejects_platform_id_field():
    with pytest.raises(ValueError, match="platform_id"):
        parse_local_direct_profile({"platform_id": "telegram:bot1"})


def test_rejects_chat_id_field():
    with pytest.raises(ValueError, match="chat_id"):
        parse_local_direct_profile({"chat_id": "12345"})


def test_context_to_request_body_includes_request_id_and_only_non_empty():
    ctx = AstorRequestContext(
        agent_id="astor_memory_mcp",
        user_id="admin",
        transport="direct",
        source="astor_memory_mcp",
        namespace=None,
        session_id=None,
        request_id="req-1",
    )
    body = context_to_request_body(ctx)
    assert body == {
        "agent_id": "astor_memory_mcp",
        "user_id": "admin",
        "transport": "direct",
        "source": "astor_memory_mcp",
        "request_id": "req-1",
    }


def test_context_to_request_body_omits_session_when_none():
    ctx = AstorRequestContext(
        agent_id="astor_memory_mcp",
        user_id="admin",
        transport="direct",
    )
    body = context_to_request_body(ctx)
    assert "session_id" not in body
    assert "namespace" not in body
    assert body["request_id"] == ctx.request_id
