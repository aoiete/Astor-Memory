"""Decision gate: a StartLux-Decision call that says yes, no, or how likely.

Why this exists
---------------
An LLM asked "are these search results good enough?" will almost always say
yes. It has no way to express "none of these fit", so a retrieval gate built on
an LLM silently degrades into a rubber stamp. A decision model returns a
calibrated probability per option plus an explicit ``none_probability``, which
is the signal a gate actually needs.

The model is small and stateless: it is given a state and a set of typed
questions, and returns numbers. It never writes prose, so it cannot be talked
into a fluent justification for a bad answer.

What is NOT here
----------------
No text generation, no agent loop, no tool calls. This module answers exactly
one question per call and returns numbers.

Degradation is deliberate
-------------------------
Every entry point returns a ``GateResult``. When the model is unreachable,
misconfigured or slow, ``available`` is False and the caller falls back to its
own judgement. A gate that can take the request path down is worse than no gate.

Privacy
-------
The state text is sent to the hosted API. Callers must not pass raw user
content here without deciding that is acceptable; ``redact`` exists to make the
decision explicit rather than implicit.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence

__all__ = [
    "GateResult",
    "DecisionGate",
    "get_gate",
    "gate_available",
]

# The published results use 0.9. The default of 0.6 drops DecisionPivot pair
# accuracy from 88.1% to 68.4% on the 27B, and gates that need rules or
# arithmetic are exactly what this module is for.
DEFAULT_GATE = 0.9

# StartLux's own guidance: at 0.2 this catches 66.3% of questions whose listed
# options are all wrong, while flagging 2.2% of questions that do have a right
# answer.
NONE_PROBABILITY_THRESHOLD = 0.2

DEFAULT_TIMEOUT = 12.0


QuestionType = Literal["choice", "noul", "score"]


@dataclass(frozen=True)
class GateResult:
    """One decision, as numbers.

    ``available`` False means the model was not consulted. The other fields
    then hold neutral values, so a caller may read them without branching first
    — but it must branch on ``available`` before treating a value as advice.
    """

    available: bool
    #: For "choice": the winning option. For "score": None — read ``score``.
    choice: str | None = None
    #: For "noul": P(yes). For "choice"/"score": the top probability.
    probability: float = 0.0
    #: For "score": the expected level, with the legend that maps it back.
    score: float | None = None
    legend: Mapping[str, str] = field(default_factory=dict)
    #: P(no listed option fits). Above ~0.2 the options are wrong, not the state.
    none_probability: float = 0.0
    #: Full per-option probabilities, for logging and for callers that want a
    #: second-best fallback rather than a hard reject.
    probabilities: Mapping[str, float] = field(default_factory=dict)
    model: str | None = None
    latency_ms: int = 0
    usage: Mapping[str, int] = field(default_factory=dict)
    #: Set when available is False. Never raises out of this module.
    error: str | None = None

    @property
    def options_all_wrong(self) -> bool:
        """True when the model thinks no listed option fits."""
        return self.available and self.none_probability >= NONE_PROBABILITY_THRESHOLD

    def top_n(self, n: int = 2) -> list[tuple[str, float]]:
        """The n most likely options, most likely first."""
        if not self.probabilities:
            return []
        ordered = sorted(self.probabilities.items(), key=lambda kv: kv[1], reverse=True)
        return ordered[:n]

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "choice": self.choice,
            "probability": round(self.probability, 6),
            "score": self.score,
            "legend": dict(self.legend),
            "none_probability": round(self.none_probability, 6),
            "options_all_wrong": self.options_all_wrong,
            "probabilities": {k: round(v, 6) for k, v in self.probabilities.items()},
            "model": self.model,
            "latency_ms": self.latency_ms,
            "usage": dict(self.usage),
            "error": self.error,
        }


def _unavailable(error: str, latency_ms: int = 0) -> GateResult:
    return GateResult(available=False, error=error, latency_ms=latency_ms)


class DecisionGate:
    """Client for one decision model.

    The client is stateless and safe to share: a request carries no session,
    and the only per-call state is local to ``_request``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        gate: float | None = None,
        timeout: float | None = None,
        *,
        think_budget: int | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("BAMBOO_API_KEY", "")
        self.base_url = (
            base_url or os.environ.get("BAMBOO_BASE_URL") or "https://api.bamboonode.dev"
        ).rstrip("/")
        self.model = (
            model or os.environ.get("BAMBOO_DECISION_MODEL")
            or "startlux-decision-27b-autothink"
        )
        gate_env = os.environ.get("BAMBOO_DECISION_GATE")
        self.gate = float(gate if gate is not None else (gate_env if gate_env else DEFAULT_GATE))
        self.timeout = float(timeout if timeout is not None else DEFAULT_TIMEOUT)
        self.think_budget = think_budget
        #: Last transport error, for diagnostics. This gate never raises, so
        #: this is how a caller finds out why it got ``available=False``.
        self._last_error: str | None = None

    # -- public API ---------------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def choose(
        self,
        state: str | Mapping[str, Any],
        question: str,
        options: Mapping[str, str] | Sequence[str],
        *,
        gate: float | None = None,
        timeout: float | None = None,
    ) -> GateResult:
        """Pick one option, or report that none of them fit.

        ``options`` may be a mapping of option id to its description, or a
        plain sequence used as both.
        """
        criteria = options if isinstance(options, Mapping) else {str(o): str(o) for o in options}
        return self._ask(
            state,
            {"type": "choice", "instructions": question, "criteria": criteria},
            gate,
            timeout,
        )

    def yes_no(
        self,
        state: str | Mapping[str, Any],
        question: str,
        *,
        gate: float | None = None,
        timeout: float | None = None,
    ) -> GateResult:
        """P(yes). ``probability`` is the answer; there is no choice."""
        return self._ask(state, {"type": "noul", "instructions": question}, gate, timeout)

    def score(
        self,
        state: str | Mapping[str, Any],
        question: str,
        levels: Sequence[str],
        *,
        gate: float | None = None,
        timeout: float | None = None,
    ) -> GateResult:
        """Expected level on an ordered scale, plus the distribution over it."""
        return self._ask(
            state,
            {"type": "score", "instructions": question, "criteria": list(levels)},
            gate,
            timeout,
        )

    def ask(
        self,
        state: str | Mapping[str, Any],
        questions: Mapping[str, Mapping[str, Any]],
        *,
        gate: float | None = None,
        timeout: float | None = None,
    ) -> dict[str, GateResult]:
        """Several typed questions in one call — the model's native shape.

        One round trip instead of N. Prefer this over repeated single calls.
        """
        if not self.configured:
            return {k: _unavailable("BAMBOO_API_KEY not set") for k in questions}
        raw = self._request(state, questions, gate, timeout)
        if raw is None:
            reason = self._last_error or "request failed"
            return {k: _unavailable(reason) for k in questions}
        return {k: self._parse_one(raw, k) for k in questions}

    # -- answer mapping -----------------------------------------------------

    @staticmethod
    def _parse_one(raw: Mapping[str, Any], key: str) -> GateResult:
        """Map one API answer onto a GateResult.

        ask() and _ask() both route through here so the two paths cannot
        disagree about what an answer means.
        """
        answers = raw.get("answers") or {}
        answer = answers.get(key)
        latency = int(raw.get("latency_ms") or 0)
        if not isinstance(answer, dict):
            return _unavailable(f"missing answer for {key!r}", latency)

        probs = {str(k): float(v) for k, v in (answer.get("probabilities") or {}).items()}
        model = raw.get("model")
        usage = raw.get("usage") or {}

        if answer.get("type") == "noul":
            p_yes = float(answer.get("noul") or 0.0)
            return GateResult(
                available=True,
                probability=p_yes,
                probabilities=probs or {"yes": p_yes, "no": 1.0 - p_yes},
                model=model,
                latency_ms=latency,
                usage=usage,
            )

        top = max(probs.values()) if probs else float(answer.get("confidence") or 0.0)
        return GateResult(
            available=True,
            choice=answer.get("choice"),
            probability=top,
            score=answer.get("score"),
            legend={str(k): str(v) for k, v in (answer.get("legend") or {}).items()},
            none_probability=float(answer.get("none_probability") or 0.0),
            probabilities=probs,
            model=model,
            latency_ms=latency,
            usage=usage,
        )

    # -- transport ----------------------------------------------------------

    def _ask(
        self,
        state: str | Mapping[str, Any],
        question: Mapping[str, Any],
        gate: float | None,
        timeout: float | None,
    ) -> GateResult:
        if not self.configured:
            return _unavailable("BAMBOO_API_KEY not set")
        # ``question`` is one BARE question dict with no id of its own — the id
        # belongs here. When choose/yes_no/score also wrapped it, the wire
        # payload became {"q": {"q": {...}}} and the API rejected it with
        # "Each question must have a nonempty ID and type choice, score, or
        # noul": the inner entry has a "q" where its "type" should be.
        # Rule: callers pass bare questions, _ask owns the id.
        raw = self._request(state, {"q": question}, gate, timeout)
        if raw is None:
            return _unavailable(self._last_error or "request failed")
        return self._parse_one(raw, "q")

    def _payload(
        self,
        state: str | Mapping[str, Any],
        questions: Mapping[str, Mapping[str, Any]],
        gate: float | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "state": state,
            "questions": questions,
            "think_mode": "auto",
            "gate": float(self.gate if gate is None else gate),
        }
        if self.think_budget is not None:
            payload["think_budget"] = int(self.think_budget)
        return payload

    def _request(
        self,
        state: str | Mapping[str, Any],
        questions: Mapping[str, Mapping[str, Any]],
        gate: float | None,
        timeout: float | None,
    ) -> dict[str, Any] | None:
        url = f"{self.base_url}/v1/decisions"
        body = json.dumps(self._payload(state, questions, gate)).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=float(timeout or self.timeout)) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read()[:300].decode("utf-8", "replace")
            except Exception:
                pass
            self._last_error = f"HTTP {exc.code}: {detail}"
            return None
        except Exception as exc:  # noqa: BLE001 - a gate must never raise
            self._last_error = f"{type(exc).__name__}: {exc}"
            return None
        elapsed = int((time.monotonic() - t0) * 1000)
        if not isinstance(payload, dict):
            self._last_error = "response was not a JSON object"
            return None
        payload["latency_ms"] = elapsed
        self._last_error = None
        return payload


_GATE: DecisionGate | None = None


def get_gate(**kwargs: Any) -> DecisionGate:
    """Process-wide client. Env vars are read once, at first use."""
    global _GATE
    if _GATE is None:
        _GATE = DecisionGate(**kwargs)
    return _GATE


def gate_available() -> bool:
    """True when a gate is configured. Cheap — no network."""
    return get_gate().configured


def redact(text: str, limit: int = 4000) -> str:
    """Shorten state text before it leaves the host.

    Exists so the "how much may we send" decision is a visible call rather
    than something each caller re-decides.
    """
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [+{len(text) - limit} chars]"


def _selftest() -> int:
    """Ask one known question and print the numbers.

    Run this before wiring the gate into a request path: it proves the key,
    the endpoint and the model name all line up, and it shows what a real
    distribution looks like.
    """
    g = get_gate()
    if not g.configured:
        print("BAMBOO_API_KEY is not set.")
        print("Add it to the env file, then re-run.")
        return 2
    print(f"endpoint {g.base_url}/v1/decisions")
    print(f"model    {g.model}")
    print(f"gate     {g.gate}")
    print()
    got = g.ask(
        "The user reports being charged twice for order #4411 and the app still shows it as unpaid.",
        {
            "team": {
                "type": "choice",
                "instructions": "Which team should handle this ticket?",
                "criteria": {"billing": "Payments and refunds", "shipping": "Delivery", "technical": "App and login"},
            },
            "urgent": {"type": "noul", "instructions": "Should this ticket be answered today?"},
            "severity": {
                "type": "score",
                "instructions": "How severe is the impact?",
                "criteria": ["cosmetic", "annoying", "blocks the customer"],
            },
        },
    )
    for name, res in got.items():
        print(f"--- {name}")
        print(json.dumps(res.as_dict(), indent=2, ensure_ascii=False))
    if all(not r.available for r in got.values()):
        print()
        print("All three questions failed. The gate degrades to unavailable;")
        print("check the key, the endpoint and outbound HTTPS.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
