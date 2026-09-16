from __future__ import annotations

import pytest

from astor_memory.agent_identity import AgentIdentity, bot_agent, direct_agent


def test_direct_agent_has_no_platform():
    identity = direct_agent(
        "evox", "admin", source="evox_mcp", namespace="evox/admin"
    )
    assert identity.transport == "direct"
    assert identity.platform is None
    assert identity.actor == "agent:evox"
    assert identity.as_request_fields() == {
        "agent_id": "evox",
        "user_id": "admin",
        "transport": "direct",
        "source": "evox_mcp",
        "namespace": "evox/admin",
    }


def test_bot_agent_requires_platform():
    identity = bot_agent("hermes-telegram", "admin", "telegram")
    assert identity.transport == "bot"
    assert identity.platform == "telegram"

    with pytest.raises(ValueError, match="requires platform"):
        AgentIdentity("hermes", "admin", transport="bot")


def test_direct_agent_rejects_platform():
    with pytest.raises(ValueError, match="must not declare a bot platform"):
        AgentIdentity("evox", "admin", transport="direct", platform="telegram")


def test_client_identity_fields_are_optional_and_backward_compatible():
    from astor_memory.client import AstorClient

    legacy = AstorClient(user_id="admin")
    legacy_fields, legacy_headers = legacy._identity_fields()
    assert legacy_fields == {}
    # 2026-09-16: X-Actor header auto-derived from user_id.
    assert legacy_headers == {"X-Actor": "admin:admin"}

    direct = AstorClient(
        user_id="admin", agent_id="evox", transport="direct", source="evox_mcp"
    )
    direct_fields, _ = direct._identity_fields()
    assert direct_fields == {
        "agent_id": "evox",
        "transport": "direct",
        "source": "evox_mcp",
    }

    with pytest.raises(ValueError, match="must not declare platform"):
        AstorClient(
            user_id="admin", agent_id="evox", transport="direct", platform="telegram"
        )._identity_fields()


def test_client_write_uses_rest_text_field():
    from astor_memory.client import AstorClient

    client = AstorClient(user_id="admin", agent_id="evox", transport="direct")
    body = {
        "text": "A sufficiently long memory fact",
        "user_id": client.user_id,
    }
    assert "content" not in body
