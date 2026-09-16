"""Phase E tests for the MCP ``astor_auto_observe`` tool handler."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from astor_memory.mcp_auto_observe import astor_auto_observe_call


TRUSTED = {
    "agent_id": "astor_memory_mcp",
    "user_id": "admin",
    "source": "astor_memory_mcp",
}


def _fake_request(observed: bool = True, tier: str = "public"):
    """Return a fake request_fn simulating /v1/write."""
    def _fn(method, path, params, body):
        assert method == "POST"
        assert path == "v1/write"
        return {"event_id": 1, "fact_ids": [42], "tier": body.get("tier", "public")}
    return _fn


def test_short_text_raises():
    try:
        astor_auto_observe_call({"text": "hi"}, request_fn=_fake_request(),
                                trusted_profile=TRUSTED)
    except ValueError as exc:
        assert "too short" in str(exc)
        return
    raise AssertionError("should have raised")


def test_noise_text_not_observed():
    result = astor_auto_observe_call(
        {"text": "嗯，今天打牌运势极佳，快去玩两把。但今天事务繁忙，需要格外注意，避开黄颜色色"},
        request_fn=_fake_request(),
        trusted_profile=TRUSTED,
    )
    text = json.loads(result["content"][0]["text"])
    assert text["observed"] is False


def test_remember_intent_observed_persisted():
    req = _fake_request(observed=True, tier="public")
    result = astor_auto_observe_call(
        {"text": "remember this: user prefers dark mode for terminal sessions"},
        request_fn=req,
        trusted_profile=TRUSTED,
    )
    text = json.loads(result["content"][0]["text"])
    assert text["observed"] is True
    assert text["persisted"] is True
    assert text["tier"] == "public"


def test_error_observed_source():
    req = _fake_request(observed=True, tier="source")
    result = astor_auto_observe_call(
        {"text": "搞砸了，astor_memory /v1/read 在空 query 时崩溃了"},
        request_fn=req,
        trusted_profile=TRUSTED,
    )
    text = json.loads(result["content"][0]["text"])
    assert text["observed"] is True
    assert text["tier"] == "source"


def test_400_falls_back_to_public():
    calls = {"n": 0}

    def req(method, path, params, body):
        calls["n"] += 1
        if body.get("tier") == "source" and calls["n"] == 1:
            raise RuntimeError("ASTOR_UPSTREAM_HTTP_400: tier=source not allowed")
        return {"event_id": 2, "fact_ids": [99]}

    result = astor_auto_observe_call(
        {"text": "搞砸了，astor_memory /v1/read 在空 query 时崩溃了"},
        request_fn=req,
        trusted_profile=TRUSTED,
    )
    text = json.loads(result["content"][0]["text"])
    assert text["observed"] is True
    assert text["persisted"] is True
    assert calls["n"] >= 2
