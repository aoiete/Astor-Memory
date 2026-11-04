"""Tests for the LLM-assisted abstraction path.

The point of the module is that it fails closed. Every failure mode
must leave the note private, and the LLM must never be the thing that
decides a tier -- the rules judge the proposal afterwards.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astor_memory.nest import llm_judge


PRIVATE_STORY = (
    "我朋友在杭州用 TQQQ 亏了 3 万，后来他改成先设止损再进场，"
    "最后才把仓位放大到 5%。这个流程对杠杆型 ETF 都有用。"
)
METHOD_ONLY = (
    "先设置止损，再确认仓位上限，最后才按计划放大仓位。"
)


def _no_provider(monkeypatch):
    monkeypatch.setattr(llm_judge, "_candidates", lambda prefer=None: [])


def _one_provider(monkeypatch):
    monkeypatch.setattr(llm_judge, "_candidates",
                        lambda prefer=None: [("fake",
                                              "https://example.invalid/v1",
                                              "k", "fake-model")])


def _fake_call(monkeypatch, payload):
    def _call(text, timeout, prefer=None):
        return payload, "fake/model"
    monkeypatch.setattr(llm_judge, "_call", _call)


# --- fails closed -----------------------------------------------------

def test_no_provider_is_private(monkeypatch):
    _no_provider(monkeypatch)
    r = llm_judge.abstract_to_shareable(PRIVATE_STORY)
    assert r["shareable"] is False
    assert r["text"] == ""
    assert "provider" in r["reason"]


def test_call_failure_is_private(monkeypatch):
    _one_provider(monkeypatch)
    monkeypatch.setattr(llm_judge, "_call", lambda t, to, pr=None: (None, ""))
    r = llm_judge.abstract_to_shareable(PRIVATE_STORY)
    assert r["shareable"] is False
    assert r["text"] == ""


def test_model_says_not_shareable_is_private(monkeypatch):
    _one_provider(monkeypatch)
    _fake_call(monkeypatch, {"shareable": False, "method": "",
                              "reason": "only status"})
    r = llm_judge.abstract_to_shareable(PRIVATE_STORY)
    assert r["shareable"] is False
    assert r["text"] == ""


def test_empty_input_is_private(monkeypatch):
    _one_provider(monkeypatch)
    r = llm_judge.abstract_to_shareable("   ")
    assert r["shareable"] is False


def test_too_short_proposal_is_private(monkeypatch):
    _one_provider(monkeypatch)
    _fake_call(monkeypatch, {"shareable": True, "method": "好",
                              "reason": "x"})
    r = llm_judge.abstract_to_shareable(PRIVATE_STORY)
    assert r["shareable"] is False


# --- grounding --------------------------------------------------------

def test_ungrounded_proposal_is_private(monkeypatch):
    """A method the note never mentioned is a hallucination."""
    _one_provider(monkeypatch)
    _fake_call(monkeypatch, {
        "shareable": True,
        "method": "先在东京做空黄金期货再买入看跌期权对冲风险敞口",
        "reason": "x"})
    r = llm_judge.abstract_to_shareable(PRIVATE_STORY)
    assert r["shareable"] is False
    assert "grounded" in r["reason"]


def test_grounding_accepts_a_real_rewrite(monkeypatch):
    _one_provider(monkeypatch)
    _fake_call(monkeypatch, {
        "shareable": True,
        "method": "先设置止损，再确认仓位上限，最后按计划放大仓位",
        "reason": "x"})
    r = llm_judge.abstract_to_shareable(METHOD_ONLY)
    assert r["shareable"] is True
    assert "止损" in r["text"]


# --- the rules dispose ------------------------------------------------

def test_proposal_with_first_person_is_refused(monkeypatch):
    """The LLM forgot the third person. The rules catch it."""
    _one_provider(monkeypatch)
    _fake_call(monkeypatch, {
        "shareable": True,
        "method": "我们先设置止损，再确认仓位上限，最后放大仓位",
        "reason": "x"})
    r = llm_judge.abstract_to_shareable(PRIVATE_STORY)
    assert r["shareable"] is False
    assert "private" in r["reason"]


def test_proposal_with_credential_is_refused(monkeypatch):
    _one_provider(monkeypatch)
    _fake_call(monkeypatch, {
        "shareable": True,
        "method": "先设置止损，再确认仓位上限，最后用 ghp_abcdefghijklmnopqrstuvwxyz1234567890 拉取数据",
        "reason": "x"})
    r = llm_judge.abstract_to_shareable(PRIVATE_STORY)
    assert r["shareable"] is False


def test_clean_method_passes(monkeypatch):
    _one_provider(monkeypatch)
    _fake_call(monkeypatch, {
        "shareable": True,
        "method": "先设置止损，再确认仓位上限，最后按计划放大仓位",
        "reason": "extracted the procedure"})
    r = llm_judge.abstract_to_shareable(PRIVATE_STORY)
    assert r["shareable"] is True
    assert r["text"]
    assert r["reason"] == "extracted the procedure"


def test_provider_available_reports_false_without_key(monkeypatch):
    _no_provider(monkeypatch)
    ok, model = llm_judge.provider_available()
    assert ok is False
    assert model == ""


# --- the agent that connected picks the provider ----------------------

def test_provider_preference_reorders_the_candidates(monkeypatch):
    """A caller that named its provider gets it first."""
    monkeypatch.setenv("MINIMAX_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k2")
    monkeypatch.delenv("ASTOR_LLM_MODEL", raising=False)
    from astor_memory.nest import llm_judge as J
    plain = J._candidates()
    self_names = [c[0] for c in plain]
    prefer = J.normalise_provider("openrouter")
    assert prefer == ("openrouter", "")
    ordered = [c[0] for c in J._candidates(prefer)]
    assert ordered[0] == "openrouter"
    assert sorted(ordered) == sorted(self_names)


def test_unknown_provider_is_not_accepted():
    """A caller must not be able to point astor at its own endpoint."""
    from astor_memory.nest import llm_judge as J
    assert J.normalise_provider("http://evil.example") == ("", "")
    assert J.normalise_provider("") == ("", "")
    # the four astor knows are accepted
    for name in J.KNOWN_PROVIDERS:
        assert J.normalise_provider(name)[0] == name


def test_agent_table_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    from astor_memory.nest import llm_judge as J
    table = J.load_agent_table()
    assert table == {}
    table["hermes"] = {"provider": "minimax",
                       "model": "MiniMax-M3.1-Flash-Preview"}
    assert J.save_agent_table(table)
    assert J.resolve_agent_provider("hermes") == (
        "minimax", "MiniMax-M3.1-Flash-Preview")
    assert J.resolve_agent_provider("muse") == ("", "")
    # a table with an unknown provider is ignored, not honoured
    (tmp_path / "agents.json").write_text(
        '{"muse": {"provider": "http://evil.example"}}', encoding="utf-8")
    assert J.resolve_agent_provider("muse") == ("", "")
