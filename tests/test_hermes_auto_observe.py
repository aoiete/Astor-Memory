"""Phase E tests for Hermes adapter auto-memory hook."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from astor_memory.hermes_adapter import AstorMemoryProvider


def _make_provider(platform: str = "telegram") -> AstorMemoryProvider:
    p = AstorMemoryProvider()
    p.initialize(session_id="test-session", platform=platform)
    p._platform_user_id = "alice"
    return p


def test_short_input_skipped():
    p = _make_provider()
    p.sync_turn(user_content="hi", assistant_content="hey")
    # No bus.append_event for auto_observe should be added; audit still runs.


def test_greeting_skipped():
    p = _make_provider()
    p.sync_turn(
        user_content="嗯",
        assistant_content="this is a long assistant reply that should not be observed either as noise prefix from user.",
    )


def test_real_content_observed():
    p = _make_provider()
    with patch("astor_memory.forge.extractor.astor_auto_observe") as obs, patch(
        "astor_memory.astor_bus"
    ) as bus_mod:
        obs.return_value = {
            "observed": True,
            "tier": "public",
            "importance": 0.8,
            "outcome": "success",
            "skipped_reason": "",
            "facts": ["some fact"],
            "namespace": "hermes/telegram/test-session",
            "agent_id": "astor_memory_adapter",
            "user_id": "alice",
        }
        bus_mod.return_value.append_event = MagicMock()
        p.sync_turn(
            user_content="remember this: user prefers dark mode for terminal sessions",
            assistant_content="noted, will save that preference.",
        )
        # observation function called once
        assert obs.called


def test_observation_failure_does_not_break_sync():
    p = _make_provider()
    with patch("astor_memory.forge.extractor.astor_auto_observe") as obs:
        obs.side_effect = RuntimeError("downstream broken")
        # Should NOT raise — observation failure is non-fatal
        p.sync_turn(
            user_content="remember this is a long enough content to trigger observe",
            assistant_content="ok noted",
        )
