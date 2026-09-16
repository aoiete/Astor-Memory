# MCP Gateway Phase A: local-direct profile

Phase A only.

## Identity contract

```text
agent_id   = astor_memory_mcp
transport  = direct
source     = astor_memory_mcp
user_id    = admin      (overridable via ASTOR_MEMORY_USER_ID)
platform*  = None       (must not be set on local-direct)
```

The MCP package is framework-agnostic (``astor-memory-rest-mcp`` v0.6.0+).
``agent_id`` is **not** tied to a specific framework such as EvoX or Hermes;
it identifies the Astor memory adapter itself.

## What Phase A adds

1. ``astor_memory.agent_identity.AstorRequestContext`` dataclass
2. ``parse_local_direct_profile(profile: dict | None = None) -> AstorRequestContext``
3. ``context_to_request_body(ctx: AstorRequestContext) -> dict``

These are *wire-level* helpers used by the MCP gateway. ``parse_local_direct_profile`` rejects any
``platform`` / ``platform_id`` / ``chat_id`` field because local-direct agents must NOT claim a bot
transport. ACL still resolves through ``user_id``; ``agent_id`` describes the producer.

## What Phase A does NOT add

- bot resolver (Phase B)
- peer-public sharing (Phase C)
- write/forget tools

## MCP gateway tools after Phase A

| name              | schema change          | behaviour change       |
|-------------------|------------------------|------------------------|
| ``astor_health``  | none                   | unchanged              |
| ``astor_read``    | none                   | + identity-conflict guard |
| ``astor_recall``  | none                   | + identity-conflict guard |
| ``astor_context`` | new, ``inputSchema {}``| returns trusted profile |
| ``astor_capabilities`` | new, ``inputSchema {}`` | returns capability flags |

## Conflict rule

Tool arguments may not override the trusted profile fields:

```text
agent_id | transport | source | user_id | namespace
```

When a tool argument matches one of these keys and its value differs from the profile, the
gateway raises ``ASTOR_IDENTITY_CONFLICT: <key>='...' does not match trusted <key>='...'``.

## Verification

- ``python -m pytest -q tests/test_agent_identity_phase_a.py`` should pass 7 tests.
- ``python -m pytest -q tests/test_agent_identity.py tests/test_bot_binding.py tests/test_platform_bridge.py`` should remain ≥ 28 tests.
