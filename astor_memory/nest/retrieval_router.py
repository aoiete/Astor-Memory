"""retrieval_router.py — Ship P1 (2026-10-03)

Learnable Routing for /v1/read. Decides whether a query should go through
graph-first expansion (heavier, finds implicit preferences via entity graph)
or dense-only (cheaper, best for explicit keyword queries).

Inspired by MLSys 2026 "Ontology-Guided Long-Term Agent Memory for
Conversational RAG" (Hill Research), specifically the Learnable Routing
component (M3: cost-aware retrieval). The paper uses contextual bandit —
we ship a simpler heuristic baseline that captures the same intent:
- graph-first: query is short, has implicit/alias risk, or looks multihop
- dense-only: query has explicit keywords, is a factoid lookup

Heuristic rationale (cheaper than bandit, no eval set needed):
  - len(query) < 8 tokens → likely implicit/preference, use graph
  - "based on", "after", "then", "之前" multihop markers → use graph
  - "什么", "who", "when", "where" + has proper noun → dense
  - default → hybrid (back-compat with v1.10.9)

Wire-in:
  /v1/read body.routing_strategy = "auto" | "graph" | "dense" | "hybrid"
    - "auto"   → call choose_route() to dispatch
    - "graph"  → force graph-first (skip synonym expansion, run graph)
    - "dense"  → skip graph entirely
    - "hybrid" → current behavior (default; back-compat)

Response adds routing_decision field with strategy, reason, and the
query features used to choose.

Cost: 0 LLM tokens; <1ms per call.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional


# Query-feature signals. Cheap regex heuristics — no LLM call.
_MULTIHOP_MARKERS_CN = re.compile(
    r"(基于|之后|之前|然后|因为|所以|根据|接着|后来|当时|从那时|那时候|过去|当时|"
    r"那段时间|那阵子|那会儿|上回|上次|那次|不久前|上周|上月|去年|当时)",
)
_MULTIHOP_MARKERS_EN = re.compile(
    r"\b(based on|after that|before that|then|because|therefore|since|"
    r"following|previously|earlier|later|at that time|afterwards|back then|"
    r"last week|last month|last year|recently)\b",
    re.IGNORECASE,
)
# Factoid question markers → likely dense-first (explicit keywords work)
_FACTOID_QUESTION_MARKERS = re.compile(
    r"(?:什么|谁|哪|where|who|when|what|which|how many|how much)\b"  # factoid words
    r"|\b\d{4}\b"  # bare 4-digit year
    r"|\b\d{1,2}月\b"  # bare month marker
    r"|\b\d{1,2}日\b",  # bare day marker
    re.IGNORECASE,
)
# Explicit keyword tokens — if query has 1+ capitalized token or quote,
# caller likely wants exact match.
_HAS_PROPER_NOUN = re.compile(r'[A-Z][a-z]{2,}|"[^"]+"|' + r"'[^']+'")


@dataclass
class RoutingDecision:
    strategy: str           # 'graph' | 'dense' | 'hybrid'
    reason: str             # human-readable explanation
    query_features: dict = field(default_factory=dict)


def _query_features(query: str) -> dict:
    """Compute lightweight query signals. No LLM, no DB."""
    if not query:
        return {
            "len_tokens": 0, "len_chars": 0,
            "has_multihop_marker": False,
            "is_factoid": False,
            "has_proper_noun": False,
            "has_quote": False,
        }
    tokens = query.split()
    return {
        "len_tokens": len(tokens),
        "len_chars": len(query),
        "has_multihop_marker": bool(
            _MULTIHOP_MARKERS_CN.search(query)
            or _MULTIHOP_MARKERS_EN.search(query)
        ),
        "is_factoid": bool(_FACTOID_QUESTION_MARKERS.search(query.strip())),
        "has_proper_noun": bool(_HAS_PROPER_NOUN.search(query)),
        "has_quote": ('"' in query) or ("'" in query and len(query.split("'")) > 2),
    }


def choose_route(query: str, body: Optional[dict] = None) -> RoutingDecision:
    """Decide routing strategy for /v1/read.

    Heuristic baseline (no bandit training needed):
      - If caller passes body.routing_strategy != "auto" → honor it (caller knows).
      - Empty/very short query → graph (likely implicit).
      - Multihop markers → graph (multi-hop decomposer already triggers).
      - Factoid (what/who/when/numbers) → dense.
      - Default → hybrid (back-compat with v1.10.9 baseline).

    Args:
      query: the user's recall query.
      body: optional request body (used to honor explicit overrides).

    Returns:
      RoutingDecision with strategy + reason + features.
    """
    body = body or {}
    feat = _query_features(query)
    explicit = (body.get("routing_strategy") or "").strip().lower()

    # Honor explicit override (caller forces a strategy).
    if explicit in ("graph", "dense", "hybrid"):
        reason_map = {
            "graph": "caller forced graph-first routing",
            "dense": "caller forced dense-only routing",
            "hybrid": "caller forced hybrid (back-compat)",
        }
        return RoutingDecision(
            strategy=explicit,
            reason=reason_map[explicit],
            query_features=feat,
        )

    # Heuristic dispatch (explicit == "" or "auto").
    if feat["len_tokens"] == 0:
        return RoutingDecision(
            strategy="hybrid", reason="empty query — fall back to hybrid baseline",
            query_features=feat,
        )

    # Multihop markers → graph-first (highest priority).
    if feat["has_multihop_marker"]:
        return RoutingDecision(
            strategy="graph",
            reason=(
                f"query has multihop marker "
                f"({feat['len_tokens']} tokens) — graph-first "
                f"expansion reaches implicit preferences"
            ),
            query_features=feat,
        )

    # Factoid question → dense (before short check, so explicit lookups
    # like "who is Caroline" or "2025 events" go dense even when short).
    if feat["is_factoid"]:
        return RoutingDecision(
            strategy="dense",
            reason=(
                f"factoid query ({feat['len_tokens']} tokens) — "
                f"dense match is faster and accurate for explicit lookups"
            ),
            query_features=feat,
        )

    # Very short, no proper noun → graph (implicit preference territory).
    if feat["len_tokens"] <= 4 and not feat["has_proper_noun"]:
        return RoutingDecision(
            strategy="graph",
            reason=(
                f"short query ({feat['len_tokens']} tokens, no proper noun) "
                f"— keyword recall misses implicit preferences"
            ),
            query_features=feat,
        )

    # Default: hybrid (back-compat with v1.10.9 baseline).
    return RoutingDecision(
        strategy="hybrid",
        reason=(
            f"default — query ({feat['len_tokens']} tokens) is ambiguous, "
            f"run graph + dense in parallel"
        ),
        query_features=feat,
    )


def routing_decision_to_dict(decision: RoutingDecision) -> dict:
    """Serialize RoutingDecision for API response."""
    return asdict(decision)


__all__ = [
    "RoutingDecision",
    "choose_route",
    "_query_features",
    "routing_decision_to_dict",
]
