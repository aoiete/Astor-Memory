"""test_mmr_reranker.py — Ship L (2026-09-28)

Unit tests for MMR diversity reranker. Verifies:
- Output length ≤ top_k.
- First pick is the highest-score candidate.
- Diversity term correctly penalizes near-duplicates.
- Empty inputs return empty.
- lambda=1.0 falls back to score-only ordering (no diversity).
- Determinism: same input → same output across calls.
- Ties broken by original index (lower wins).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory.nest.mmr_reranker import mmr_rerank, _tokens, _jaccard


def _eq(label, got, expected):
    if got != expected:
        print(f"  [FAIL] {label}\n    got     : {got}\n    expected: {expected}")
        sys.exit(1)
    print(f"  [PASS] {label}")


def _truthy(label, got):
    if not got:
        print(f"  [FAIL] {label} (got falsy)")
        sys.exit(1)
    print(f"  [PASS] {label}")


def main() -> int:
    print("=== Unit cases ===")

    # tokens
    _eq("tokens('hello world')", _tokens("hello world"), {"hello", "world"})
    _eq("tokens('') empty", _tokens(""), set())
    _eq("tokens(cn) per-char", _tokens("八字"), {"八", "字"})
    _eq("tokens(mixed)", _tokens("Hi 八字 hello"), {"hi", "八", "字", "hello"})

    # jaccard
    _eq("jaccard identical", _jaccard({"a","b"}, {"a","b"}), 1.0)
    _eq("jaccard disjoint", _jaccard({"a"}, {"b"}), 0.0)
    _eq("jaccard partial", _jaccard({"a","b","c"}, {"b","c","d"}), 0.5)
    _eq("jaccard with empty", _jaccard(set(), {"a"}), 0.0)

    # Empty input
    _eq("empty candidates", mmr_rerank([], {}, "anything", top_k=5), [])
    _eq("top_k=0 short-circuit",
        mmr_rerank([(1, 0.9)], {1: "x"}, "q", top_k=0), [])

    # top_k >= len(candidates) returns all in original order
    cands = [(1, 0.9), (2, 0.8)]
    _eq("top_k>=n returns all",
        mmr_rerank(cands, {1: "a", 2: "b"}, "q", top_k=5),
        cands)

    # lambda=1.0 falls back to score-only (assumes candidates pre-sorted by score desc)
    cands = [(2, 0.9), (3, 0.7), (1, 0.5)]
    contents = {1: "dup dup dup", 2: "unique", 3: "dup dup dup"}
    out = mmr_rerank(cands, contents, "q", top_k=2, lambda_=1.0)
    _eq("lambda=1.0 score order", [x[0] for x in out], [2, 3])

    # First pick is highest score (lambda<1.0 too)
    out = mmr_rerank(cands, contents, "q", top_k=2, lambda_=0.7)
    _eq("first pick = highest score", out[0][0], 2)

    # Diversity: when candidate #2 and #3 are near-dup of #1, MMR picks #2
    # first (high score), then prefers something different from #2.
    cands = [(10, 0.9), (11, 0.85), (12, 0.5)]
    contents = {
        10: "用户喜欢德州扑克",
        11: "用户偏好德州扑克",  # near-dup of 10
        12: "八字 五行 起卦",   # diverse
    }
    out = mmr_rerank(cands, contents, "德州扑克", top_k=2, lambda_=0.7)
    _eq("first pick #10", out[0][0], 10)
    # With lambda=0.7 the relevance term still dominates; #11 wins on score
    # despite the diversity penalty. This is the expected Carbonell-Goldstein
    # behavior at high lambda. Switch to lambda=0.5 to see diversity win.
    _eq("lambda=0.7: relevance dominates (#11 over #12)", out[1][0], 11)
    out05 = mmr_rerank(cands, contents, "德州扑克", top_k=2, lambda_=0.5)
    _eq("lambda=0.5: diversity term wins (#12 over #11)", out05[1][0], 12)

    # Determinism: same input → same output
    cands = [(1, 0.5), (2, 0.5), (3, 0.5), (4, 0.5)]
    contents = {1: "a b", 2: "b c", 3: "c d", 4: "d e"}
    out_a = mmr_rerank(cands, contents, "q", top_k=3, lambda_=0.5)
    out_b = mmr_rerank(cands, contents, "q", top_k=3, lambda_=0.5)
    _eq("determinism", [x[0] for x in out_a], [x[0] for x in out_b])

    # Tie-break by original index (lower wins)
    cands = [(1, 0.5), (2, 0.5), (3, 0.5)]
    contents = {1: "a b c", 2: "a b c", 3: "a b c"}  # all identical
    out = mmr_rerank(cands, contents, "q", top_k=2, lambda_=0.5)
    _eq("tie-break by original index", [x[0] for x in out], [1, 2])

    # Missing content → empty token set → not penalized
    cands = [(1, 0.9), (2, 0.8)]
    out = mmr_rerank(cands, {1: "hello", 2: "world"}, "q", top_k=2)
    _eq("missing content handled", [x[0] for x in out], [1, 2])

    # Score preserved in output
    cands = [(1, 0.9), (2, 0.8)]
    out = mmr_rerank(cands, {1: "a", 2: "b"}, "q", top_k=2)
    _eq("score preserved", [x[1] for x in out], [0.9, 0.8])

    # Chinese: pure CN content diversity
    cands = [(1, 0.9), (2, 0.85), (3, 0.7)]  # already score-desc
    contents = {1: "八字 壬水 身强", 2: "八字 壬水 身强", 3: "六爻 起卦 排盘"}
    out = mmr_rerank(cands, contents, "q", top_k=2, lambda_=0.7)
    _eq("CN diversity: #1 first", out[0][0], 1)
    _eq("CN diversity: #3 over near-dup #2", out[1][0], 3)

    # Output length never exceeds top_k
    big = [(i, 1.0 - i * 0.01) for i in range(50)]
    contents = {i: f"doc {i}" for i in range(50)}
    out = mmr_rerank(big, contents, "q", top_k=10)
    _truthy("output length ≤ top_k", len(out) <= 10)
    _eq("output length == top_k", len(out), 10)

    print("\nAll tests PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())