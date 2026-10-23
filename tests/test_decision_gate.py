"""Tests for the decision gate.

Two layers, deliberately separate:

- Offline tests exercise every branch that does not need a network call,
  including the degradation paths. These run in CI with no key set.
- Live tests are skipped unless BAMBOO_API_KEY is set, and hit the real model.

The offline layer is the one that matters most. A gate that cannot be trusted
to degrade is a gate that can take a request path down, and that failure only
shows up in production unless it is tested here.
"""

from __future__ import annotations

import json
import os

import pytest

from astor_memory._internal.decision_gate import (
    DEFAULT_GATE,
    NONE_PROBABILITY_THRESHOLD,
    DecisionGate,
    GateResult,
    gate_available,
    get_gate,
    redact,
)


# --------------------------------------------------------------------------
# Offline: payload construction
# --------------------------------------------------------------------------


def test_default_gate_is_the_published_one():
    """0.6 is the library default; 0.9 is what the published numbers use.

    Dropping the gate to 0.6 takes DecisionPivot pair accuracy from 88.1% to
    68.4% on the 27B, and the questions this module exists to answer are
    exactly the rule-and-arithmetic kind. Silently inheriting 0.6 would make
    every gate quietly worse than the benchmark it is judged against.
    """
    assert DEFAULT_GATE == 0.9


def test_none_threshold_is_the_published_one():
    """0.2 is StartLux's own guidance: 66.3% recall, 2.2% false flags."""
    assert NONE_PROBABILITY_THRESHOLD == 0.2


def test_gate_reads_env_when_not_given(monkeypatch):
    monkeypatch.setenv("BAMBOO_DECISION_GATE", "0.75")
    monkeypatch.setenv("BAMBOO_DECISION_MODEL", "some-other-model")
    g = DecisionGate(api_key="k")
    assert g.gate == 0.75
    assert g.model == "some-other-model"


def test_gate_explicit_argument_wins_over_env(monkeypatch):
    monkeypatch.setenv("BAMBOO_DECISION_GATE", "0.75")
    assert DecisionGate(api_key="k", gate=0.5).gate == 0.5


def test_payload_shape_is_what_the_api_accepts():
    """One question must reach the wire as {"q": {bare question with type}}.

    When choose/yes_no/score wrapped the bare question themselves, the wire
    payload became {"q": {"q": {...}}} and the API answered HTTP 400 "Each
    question must have a nonempty ID and type choice, score, or noul" — the
    inner object has a "q" where its "type" belongs. This asserts the exact
    nesting so the bug cannot come back.
    """
    g = DecisionGate(api_key="k")
    payload = g._payload("state text", {"q": {"type": "noul", "instructions": "Q?"}}, None)
    question = payload["questions"]["q"]
    assert set(question) == {"type", "instructions"}
    assert question["type"] == "noul"
    assert "q" not in question
    assert payload["think_mode"] == "auto"
    assert payload["gate"] == DEFAULT_GATE
    assert payload["state"] == "state text"


def test_convenience_wrappers_pass_bare_questions(monkeypatch):
    """choose/yes_no/score must not pre-wrap; _ask owns the id.

    One wrong level of nesting produces a 400 that reads like a schema
    problem, so it is worth pinning down with a captured payload.
    """
    g = DecisionGate(api_key="k")
    captured = []

    def fake_request(state, questions, gate, timeout):
        captured.append({"state": state, "questions": questions})
        return {"answers": {"q": {"type": "noul", "noul": 0.8}},
                "model": "m", "latency_ms": 1, "usage": {}}

    monkeypatch.setattr(g, "_request", fake_request)
    g.yes_no("s", "Is it urgent?")
    g.score("s", "How bad?", ["low", "high"])
    g.choose("s", "Which team?", {"a": "A", "b": "B"})

    assert len(captured) == 3
    for call in captured:
        assert list(call["questions"]) == ["q"]
        assert "type" in call["questions"]["q"], f"double-wrapped: {call['questions']}"
    assert captured[0]["questions"]["q"]["type"] == "noul"
    assert captured[1]["questions"]["q"]["type"] == "score"
    assert captured[2]["questions"]["q"]["type"] == "choice"


def test_choice_accepts_a_sequence_of_options(monkeypatch):
    """A list of options becomes a dict keyed by the option text."""
    g = DecisionGate(api_key="k")
    captured = {}

    def fake_request(state, questions, gate, timeout):
        captured.update(questions)
        return {"answers": {"q": {"type": "choice", "choice": "alpha",
                                  "probabilities": {"alpha": 1.0},
                                  "none_probability": 0.0}},
                "model": "m", "latency_ms": 1, "usage": {}}

    monkeypatch.setattr(g, "_request", fake_request)
    res = g.choose("s", "Which?", ["alpha", "beta"])
    assert captured["q"]["criteria"] == {"alpha": "alpha", "beta": "beta"}
    assert res.choice == "alpha"


# --------------------------------------------------------------------------
# Offline: degradation
# --------------------------------------------------------------------------


def test_missing_key_degrades_without_raising(monkeypatch):
    monkeypatch.delenv("BAMBOO_API_KEY", raising=False)
    g = DecisionGate(api_key="")
    assert not g.configured

    res = g.yes_no("state", "question?")
    assert res.available is False
    assert res.probability == 0.0
    assert "BAMBOO_API_KEY" in (res.error or "")

    chosen = g.choose("state", "which?", {"a": "A"})
    assert chosen.available is False
    assert chosen.choice is None


def test_ask_reports_unavailable_for_every_question(monkeypatch):
    monkeypatch.delenv("BAMBOO_API_KEY", raising=False)
    g = DecisionGate(api_key="")
    got = g.ask("state", {"a": {"type": "noul", "instructions": "?"},
                          "b": {"type": "choice", "instructions": "?",
                                "criteria": {"x": "X"}}})
    assert set(got) == {"a", "b"}
    assert all(not r.available for r in got.values())


def test_failed_request_carries_the_reason(monkeypatch):
    """A silent failure is the worst outcome: the caller cannot tell why."""
    g = DecisionGate(api_key="k")
    monkeypatch.setattr(g, "_request", lambda *a, **k: None)
    g._last_error = "OSError: connection reset by peer"
    res = g.yes_no("s", "q?")
    assert res.available is False
    assert "connection reset" in (res.error or "")


def test_http_error_detail_is_captured(monkeypatch):
    """The 400 body names the real problem; keep it instead of discarding."""
    import io
    import urllib.error

    g = DecisionGate(api_key="k", base_url="https://example.invalid")

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                     io.BytesIO(b'{"error":"bad shape"}'))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert g._request("s", {"q": {"type": "noul", "instructions": "?"}},
                      None, None) is None
    assert "HTTP 400" in (g._last_error or "")
    assert "bad shape" in (g._last_error or "")


def test_unavailable_never_invents_a_choice():
    """An outage must not turn into a confident wrong answer."""
    g = DecisionGate(api_key="")
    res = g.choose("s", "which team?", {"billing": "B", "infra": "I"})
    assert res.available is False
    assert res.choice is None, "a failed choice gate must not return an option"
    assert res.probability == 0.0


def test_non_json_object_response_degrades(monkeypatch):
    g = DecisionGate(api_key="k")

    class FakeResp:
        def read(self):
            return b"<html>gateway</html>"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: FakeResp())
    assert g._request("s", {"q": {"type": "noul", "instructions": "?"}},
                      None, None) is None
    # An HTML error page fails json.loads, so the reason names the decoder
    # rather than the shape — which is the more useful message when the
    # endpoint answered 200 with a proxy error page.
    assert "JSONDecodeError" in (g._last_error or "")


def test_json_array_response_degrades(monkeypatch):
    """A 200 whose body is a list is not an answer either."""
    g = DecisionGate(api_key="k")

    class FakeResp:
        def read(self):
            return b"[1, 2, 3]"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: FakeResp())
    assert g._request("s", {"q": {"type": "noul", "instructions": "?"}},
                      None, None) is None


# --------------------------------------------------------------------------
# Offline: answer parsing
# --------------------------------------------------------------------------


def test_noul_answer_is_p_yes():
    g = DecisionGate(api_key="k")
    raw = {"answers": {"q": {"type": "noul", "noul": 0.87}},
           "model": "m", "latency_ms": 42}
    parsed = g._parse_one(raw, "q")
    assert parsed.available
    assert parsed.probability == pytest.approx(0.87)
    assert parsed.choice is None
    assert parsed.latency_ms == 42
    assert parsed.model == "m"


def test_noul_answer_synthesises_a_yes_no_distribution():
    """noul answers carry no probabilities dict; a caller still wants one."""
    g = DecisionGate(api_key="k")
    raw = {"answers": {"q": {"type": "noul", "noul": 0.25}},
           "model": "m", "latency_ms": 1}
    parsed = g._parse_one(raw, "q")
    assert parsed.probabilities["yes"] == pytest.approx(0.25)
    assert parsed.probabilities["no"] == pytest.approx(0.75)


def test_choice_answer_carries_none_probability():
    g = DecisionGate(api_key="k")
    raw = {
        "answers": {
            "q": {
                "type": "choice",
                "choice": "billing",
                "probabilities": {"billing": 0.995, "shipping": 0.0,
                                  "technical": 0.005},
                "none_probability": 0.002,
            }
        },
        "model": "m",
        "latency_ms": 10,
    }
    parsed = g._parse_one(raw, "q")
    assert parsed.choice == "billing"
    assert parsed.probability == pytest.approx(0.995)
    assert parsed.none_probability == pytest.approx(0.002)
    assert parsed.options_all_wrong is False


def test_choice_without_probabilities_falls_back_to_confidence():
    g = DecisionGate(api_key="k")
    raw = {"answers": {"q": {"type": "choice", "choice": "a", "confidence": 0.4}},
           "model": "m", "latency_ms": 1}
    parsed = g._parse_one(raw, "q")
    assert parsed.probability == pytest.approx(0.4)


def test_options_all_wrong_uses_the_published_threshold():
    """0.2 catches 66.3% of all-wrong questions, flags 2.2% of good ones."""
    assert GateResult(available=True, none_probability=0.19).options_all_wrong is False
    assert GateResult(
        available=True, none_probability=NONE_PROBABILITY_THRESHOLD
    ).options_all_wrong is True
    assert GateResult(available=True, none_probability=0.996).options_all_wrong is True


def test_options_all_wrong_is_false_when_unavailable():
    """An outage is not evidence that the options are wrong."""
    assert GateResult(available=False, none_probability=1.0).options_all_wrong is False


def test_score_answer_carries_legend():
    g = DecisionGate(api_key="k")
    raw = {
        "answers": {
            "q": {
                "type": "score",
                "score": 1.858,
                "confidence": 0.787,
                "legend": {"0": "cosmetic", "1": "annoying", "2": "blocks"},
                "probabilities": {"0": 0.005, "1": 0.133, "2": 0.863},
            }
        },
        "model": "m",
        "latency_ms": 30,
    }
    parsed = g._parse_one(raw, "q")
    assert parsed.score == pytest.approx(1.858)
    assert parsed.legend["2"] == "blocks"
    assert parsed.probability == pytest.approx(0.863)


def test_missing_answer_degrades_per_key():
    g = DecisionGate(api_key="k")
    parsed = g._parse_one({"answers": {}, "model": "m", "latency_ms": 5}, "missing")
    assert parsed.available is False
    assert "missing answer" in (parsed.error or "")


def test_ask_maps_each_key_independently(monkeypatch):
    """One bad answer must not blank out the good ones in the same call."""
    g = DecisionGate(api_key="k")
    monkeypatch.setattr(g, "_request", lambda *a, **k: {
        "answers": {"good": {"type": "noul", "noul": 0.9}},
        "model": "m", "latency_ms": 7, "usage": {}})
    got = g.ask("s", {"good": {"type": "noul", "instructions": "?"},
                      "bad": {"type": "noul", "instructions": "?"}})
    assert got["good"].available is True
    assert got["good"].probability == pytest.approx(0.9)
    assert got["bad"].available is False


def test_top_n_orders_by_probability():
    res = GateResult(available=True, probabilities={"a": 0.1, "b": 0.7, "c": 0.2})
    assert res.top_n(2) == [("b", 0.7), ("c", 0.2)]
    assert res.top_n(10) == [("b", 0.7), ("c", 0.2), ("a", 0.1)]


def test_top_n_on_empty_probabilities():
    assert GateResult(available=False).top_n(2) == []


def test_as_dict_is_json_serialisable():
    """Callers log this; a non-serialisable field would break logging."""
    res = GateResult(available=True, choice="x", probability=0.5,
                     probabilities={"x": 0.5}, legend={"0": "a"})
    assert '"choice": "x"' in json.dumps(res.as_dict())


# --------------------------------------------------------------------------
# Offline: redact
# --------------------------------------------------------------------------


def test_redact_shortens_and_marks_the_cut():
    assert redact("x" * 5000, limit=100).endswith("[+4900 chars]")


def test_redact_leaves_short_text_alone():
    assert redact("short text") == "short text"


def test_redact_collapses_whitespace():
    assert redact("a\n\n   b\tc") == "a b c"


# --------------------------------------------------------------------------
# Offline: process-wide client
# --------------------------------------------------------------------------


def test_get_gate_is_cached():
    get_gate.__globals__["_GATE"] = None
    assert get_gate() is get_gate()
    get_gate.__globals__["_GATE"] = None


def test_gate_available_reflects_key(monkeypatch):
    get_gate.__globals__["_GATE"] = None
    monkeypatch.delenv("BAMBOO_API_KEY", raising=False)
    assert gate_available() is False
    monkeypatch.setenv("BAMBOO_API_KEY", "sk-test")
    get_gate.__globals__["_GATE"] = None
    assert gate_available() is True
    get_gate.__globals__["_GATE"] = None


# --------------------------------------------------------------------------
# Live: real model, only when a key is configured
# --------------------------------------------------------------------------

live = pytest.mark.skipif(
    not os.environ.get("BAMBOO_API_KEY"),
    reason="BAMBOO_API_KEY not set",
)


@live
def test_live_picks_the_right_team():
    got = get_gate().ask(
        "The user reports being charged twice for order #4411 and the app "
        "still shows it unpaid.",
        {
            "team": {
                "type": "choice",
                "instructions": "Which team should handle this ticket?",
                "criteria": {
                    "billing": "Payments and refunds",
                    "shipping": "Delivery",
                    "technical": "App and login",
                },
            }
        },
    )
    res = got["team"]
    assert res.available
    assert res.choice == "billing"
    assert res.probability > 0.7


@live
def test_live_flags_all_wrong_options():
    """The case an LLM gate cannot express: none of these fit."""
    res = get_gate().choose(
        "Kubernetes pod is in CrashLoopBackOff, OOMKilled, image pull failed.",
        "Which team handles this?",
        {"billing": "Payments and refunds", "shipping": "Delivery"},
    )
    assert res.available
    assert res.none_probability > 0.5
    assert res.options_all_wrong is True


@live
def test_live_accepts_the_right_option_when_offered():
    res = get_gate().choose(
        "Pod keeps restarting with OOMKilled; the memory limit is too low.",
        "Which team handles this?",
        {"infra": "Kubernetes and runtime", "billing": "Payments"},
    )
    assert res.available
    assert res.choice == "infra"
    assert res.none_probability < 0.2


@live
def test_live_batched_ask_is_one_round_trip():
    got = get_gate().ask(
        "Charged twice for order #4411, app still shows it unpaid.",
        {
            "team": {
                "type": "choice",
                "instructions": "Which team?",
                "criteria": {"billing": "Payments", "shipping": "Delivery",
                             "technical": "App"},
            },
            "urgent": {"type": "noul", "instructions": "Answer today?"},
        },
    )
    assert got["team"].choice == "billing"
    # Both answers report the same latency: they came from one call.
    assert got["team"].latency_ms == got["urgent"].latency_ms
    assert got["urgent"].probability > 0.5