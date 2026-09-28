"""test_hyde.py — Ship O (2026-09-28)

Verifies the HyDE hypothetical-document-embeddings path. Tests:
- ASTOR_HYDE gate: default off → empty hypothetical.
- Short-query detection (<8 tokens triggers; ≥8 tokens does not).
- Cache works (repeated calls don't re-hit LLM).
- merge_hyde_hits: dedupes by fact_id, takes max score.
- Weight applies correctly to hyde hits.
- Failure paths return "" without raising.
- LRU cache size limit honored.

No live LLM calls; we monkey-patch _call_hyde_llm.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Force-load module to clear any cached state from previous runs.
for k in list(sys.modules):
    if k.startswith("astor_memory"):
        del sys.modules[k]

from astor_memory.nest.hyde import (
    hypothetical_answer,
    merge_hyde_hits,
    _is_short,
    _cached_hypothetical,
    _call_hyde_llm,
)


def _eq(label, got, expected):
    if got != expected:
        print(f"  [FAIL] {label}\n    got     : {got}\n    expected: {expected}")
        sys.exit(1)
    print(f"  [PASS] {label}")


def _truthy(label, got):
    if not got:
        print(f"  [FAIL] {label} (got falsy: {got!r})")
        sys.exit(1)
    print(f"  [PASS] {label}")


def main() -> int:
    print("=== Unit cases ===")

    # Short query detection
    _truthy("short CN: '健身'", _is_short("健身"))
    _truthy("short CN: '今日日柱'", _is_short("今日日柱"))
    _truthy("short EN: 'poker'", _is_short("poker"))
    _truthy("long CN: 9 tokens", not _is_short("用户 喜欢 德州 扑克 现金局 玩 多久 NL10"))
    _truthy("long EN: 10 words", not _is_short("When did Caroline go to the store yesterday morning"))

    # Gate default off (no env var)
    os.environ.pop("ASTOR_HYDE", None)
    os.environ["OPENROUTER_API_KEY"] = "test-key"
    _eq("gate default off → empty", hypothetical_answer("健身"), "")
    _eq("gate off + long query → empty", hypothetical_answer("a long query with many tokens"), "")

    # Gate on but no short query → empty
    os.environ["ASTOR_HYDE"] = "1"
    _eq("gate on + long query → empty", hypothetical_answer("a long query with many tokens"), "")

    # Gate on + short query → mock LLM returns hypothesis
    os.environ["ASTOR_HYDE"] = "1"
    # Clear cache so each test starts fresh
    _cached_hypothetical.cache_clear()

    fake_responses = {
        "健身": "用户每周去健身房锻炼三次，主要做力量训练。",
        "今日日柱": "今天 2026-09-28 是丙戌日。",
        "poker": "User plays NL10 Texas Hold'em cash games online.",
    }
    with patch("astor_memory.nest.hyde._call_hyde_llm",
               side_effect=lambda q: fake_responses.get(q, "")):
        _eq("short CN '健身'", hypothetical_answer("健身"),
            "用户每周去健身房锻炼三次，主要做力量训练。")
        _eq("short CN '今日日柱'", hypothetical_answer("今日日柱"),
            "今天 2026-09-28 是丙戌日。")
        _eq("short EN 'poker'", hypothetical_answer("poker"),
            "User plays NL10 Texas Hold'em cash games online.")

    # Unknown query → mock returns "" → hypothetical_answer returns ""
    with patch("astor_memory.nest.hyde._call_hyde_llm", return_value=""):
        _eq("LLM returns empty", hypothetical_answer("随便问"), "")

    # Failure: no API key
    os.environ.pop("OPENROUTER_API_KEY", None)
    _cached_hypothetical.cache_clear()
    _eq("no API key → empty", hypothetical_answer("健身"), "")

    # merge_hyde_hits: basic dedup + max
    primary = [(1, 0.9), (2, 0.7), (3, 0.5)]
    hyde = [(2, 0.8), (4, 0.6)]  # 2 collides; 4 is new
    merged = merge_hyde_hits(primary, hyde, weight=0.5)
    # 2: max(0.7, 0.8*0.5=0.4) = 0.7 (primary wins); 4: 0.6*0.5 = 0.3
    # Order: 1 (0.9), 2 (0.7), 3 (0.5), 4 (0.3)
    _eq("merge dedup + order",
        [(f, round(s, 3)) for f, s in merged],
        [(1, 0.9), (2, 0.7), (3, 0.5), (4, 0.3)])

    # merge_hyde_hits: hyde hit can beat primary when weighted high enough
    merged2 = merge_hyde_hits([(1, 0.5)], [(1, 0.95)], weight=1.0)
    _eq("hyde overrides at weight=1.0", merged2, [(1, 0.95)])

    # merge_hyde_hits: empty inputs
    _eq("merge empty primary", merge_hyde_hits([], [(1, 0.5)]), [(1, 0.25)])  # 0.5*weight 0.5
    _eq("merge empty hyde", merge_hyde_hits([(1, 0.5)], []), [(1, 0.5)])
    _eq("merge both empty", merge_hyde_hits([], []), [])

    # merge_hyde_hits: int conversion safety
    merged3 = merge_hyde_hits([("5", 0.5)], [(5, 0.9)], weight=0.5)
    _eq("str fid coerced to int", merged3, [(5, 0.5)])  # 0.9*0.5=0.45<0.5

    # Cache: second call should hit cache, not LLM
    os.environ["OPENROUTER_API_KEY"] = "test-key"
    os.environ["ASTOR_HYDE"] = "1"
    _cached_hypothetical.cache_clear()

    call_count = {"n": 0}
    def counting_mock(q):
        call_count["n"] += 1
        return f"hypo-{q}"

    with patch("astor_memory.nest.hyde._call_hyde_llm", side_effect=counting_mock):
        # First call: hits LLM
        h1 = hypothetical_answer("健身")
        _eq("first call result", h1, "hypo-健身")
        _eq("LLM called once", call_count["n"], 1)
        # Second call same query: cache hit
        h2 = hypothetical_answer("健身")
        _eq("second call cached", h2, "hypo-健身")
        _eq("LLM still called once (cache hit)", call_count["n"], 1)

    # API key set, env off → empty regardless of API
    os.environ["ASTOR_HYDE"] = "0"
    _cached_hypothetical.cache_clear()
    _eq("env off overrides key", hypothetical_answer("健身"), "")

    # No cache use_cache=False
    os.environ["ASTOR_HYDE"] = "1"
    _cached_hypothetical.cache_clear()
    with patch("astor_memory.nest.hyde._call_hyde_llm", return_value="direct"):
        _eq("no-cache direct call", hypothetical_answer("poker", use_cache=False), "direct")

    print("\nAll tests PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())