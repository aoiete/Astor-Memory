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


def test_the_raw_conversation_never_reaches_the_shared_bus():
    """v1.17.55: the raw turn text was appended straight to the bus.

    That write bypasses the write path's PII gate, so a line like "my
    password is X" rode along with a shareable lesson into the shared
    tier -- the classifier had only judged the extracted fact, not the
    text it came from.
    """
    p = _make_provider()
    with patch("astor_memory.forge.extractor.astor_auto_observe") as obs, \
            patch("astor_memory._internal.acl.open_own_private") as own, \
            patch("astor_memory.astor_bus") as bus_mod, \
            patch("astor_memory.forge.extractor.astor_capture_intent") as aci:
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
        own.return_value = MagicMock()
        bus_mod.return_value.append_event = MagicMock()
        p.sync_turn(
            user_content="remember this: my password is hunter2 and the lesson is to rotate keys",
            assistant_content="noted, will save that preference.",
        )
        # The adapter writes other events too (the audit trail), so the
        # assertion is about the CONTENT, not about the call count.
        contents = [
            kw.get("content", "")
            for call in bus_mod.return_value.append_event.call_args_list
            for kw in [call.kwargs]
        ]
        assert not any("hunter2" in c for c in contents), \
            f"the raw turn text must not be written to the shared bus: {contents}"
        assert own.called, "the observation belongs in the user's own store"
        own.assert_called_once_with("alice")
        assert aci.call_args.kwargs["user_id"] == "alice", \
            "the capture must carry the real user, so the classifier " \
            "consults that user's own consent switch"


if __name__ == "__main__":
    pass
