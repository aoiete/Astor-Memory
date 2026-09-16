"""Common identity model for bot and direct Astor agent integrations.

Bot integrations (Telegram/Discord/Weixin/etc.) identify an inbound chat via
``platform_id + chat_id`` and resolve it to ``user_id``. Direct integrations
(MCP, SDK, CLI, local agents) have no chat transport and identify themselves by
an explicit stable ``agent_id`` instead.

This module is deliberately transport-only metadata. ACL still uses the
canonical ``user_id`` and actor resolution in the server.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

Transport = Literal["bot", "direct"]


@dataclass(frozen=True)
class AgentIdentity:
    """Validated identity context attached to an Astor request."""

    agent_id: str
    user_id: str
    transport: Transport = "direct"
    platform: str | None = None
    source: str | None = None
    namespace: str | None = None
    session_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("agent_id", "user_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if self.transport not in ("bot", "direct"):
            raise ValueError("transport must be 'bot' or 'direct'")
        if self.transport == "bot" and not self.platform:
            raise ValueError("bot transport requires platform")
        if self.transport == "direct" and self.platform:
            raise ValueError("direct transport must not declare a bot platform")

    @property
    def actor(self) -> str:
        """Canonical ACL actor; this is not a replacement for user_id."""
        return f"agent:{self.agent_id}"

    def as_request_fields(self) -> dict[str, str]:
        """Return only non-empty, protocol-safe context fields."""
        fields = {
            "agent_id": self.agent_id,
            "user_id": self.user_id,
            "transport": self.transport,
        }
        for key in ("platform", "source", "namespace", "session_id"):
            value = getattr(self, key)
            if value:
                fields[key] = value
        return fields


def direct_agent(
    agent_id: str,
    user_id: str,
    *,
    source: str | None = None,
    namespace: str | None = None,
    session_id: str | None = None,
) -> AgentIdentity:
    """Build an agent that does not arrive through an external bot."""
    return AgentIdentity(
        agent_id=agent_id,
        user_id=user_id,
        transport="direct",
        source=source,
        namespace=namespace,
        session_id=session_id,
    )


def bot_agent(
    agent_id: str,
    user_id: str,
    platform: str,
    *,
    source: str | None = None,
    namespace: str | None = None,
    session_id: str | None = None,
) -> AgentIdentity:
    """Build a bot-backed agent; chat binding remains a separate concern."""
    return AgentIdentity(
        agent_id=agent_id,
        user_id=user_id,
        transport="bot",
        platform=platform,
        source=source,
        namespace=namespace,
        session_id=session_id,
    )


@dataclass(frozen=True)
class AstorRequestContext:
    """Wire-level context for a single Astor request.

    Independent of :class:`AgentIdentity`: the latter describes a single
    agent factory input, while this dataclass captures the resolved
    identity + transport + session fields that propagate over REST/MCP.
    ACL still resolves through ``user_id``; ``agent_id`` is the producer.
    """

    agent_id: str
    user_id: str
    transport: Transport = "direct"
    platform: str | None = None
    platform_id: str | None = None
    chat_id: str | None = None
    source: str | None = None
    namespace: str | None = None
    session_id: str | None = None
    scopes: tuple[str, ...] = field(default_factory=tuple)
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    enabled: bool = True


DEFAULT_LOCAL_DIRECT = {
    "agent_id": "astor_memory_mcp",
    "user_id": "admin",
    "source": "astor_memory_mcp",
    "transport": "direct",
    "enabled": True,
}


def parse_local_direct_profile(
    profile: dict | None = None,
) -> AstorRequestContext:
    """Resolve the local-direct profile for the Astor MCP Gateway.

    Defaults match ``astor-memory-rest-mcp`` v0.6.0 (``agent_id=astor_memory_mcp``,
    ``source=astor_memory_mcp``, ``transport=direct``, ``user_id=admin``). The
    MCP package is framework-agnostic; ``agent_id`` no longer couples the
    request to a specific agent framework such as EvoX or Hermes.

    ``platform`` / ``platform_id`` / ``chat_id`` are explicitly rejected because
    local-direct agents must NOT claim a bot platform; use ``parse_bot_profile``
    for bot transports.
    """
    merged: dict = {**DEFAULT_LOCAL_DIRECT, **(profile or {})}

    for name in ("platform", "platform_id", "chat_id"):
        value = merged.get(name)
        if isinstance(value, str) and value.strip():
            raise ValueError(
                f"local-direct profile must not declare {name}; "
                "use bot resolver for bot transports"
            )

    return AstorRequestContext(
        agent_id=str(merged["agent_id"]).strip(),
        user_id=str(merged["user_id"]).strip(),
        transport=str(merged.get("transport", "direct")).strip(),
        source=(str(merged["source"]).strip() if merged.get("source") else None),
        enabled=bool(merged.get("enabled", True)),
    )


def context_to_request_body(ctx: AstorRequestContext) -> dict:
    """Return only non-empty fields accepted by ``_resolve_agent_context``.

    ``request_id`` is forwarded so audit rows can be correlated across
    gateway hops; downstream must ignore unknown fields.
    """
    body: dict[str, str] = {
        "agent_id": ctx.agent_id,
        "user_id": ctx.user_id,
        "transport": ctx.transport,
        "request_id": ctx.request_id,
    }
    for name in ("source", "namespace", "session_id"):
        value = getattr(ctx, name)
        if value:
            body[name] = value
    return body
