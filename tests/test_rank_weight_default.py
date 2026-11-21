
"""The bm25/vector split is a ratio, and both weights move together.

The 0.6/0.4 default was measured on a 22-fact corpus. The corpus is
5723 facts now, and only the ratio decides the ranking -- so a change
to one weight without the other silently moves the ratio.

Measured on both eval sets (2026-11-20):
    0.60:0.40 -> paraphrase hit 0.654 / mrr 0.383, default hit 0.982 / mrr 0.817
    0.50:0.50 -> paraphrase hit 0.692 / mrr 0.385, default hit 0.982 / mrr 0.831
    0.45:0.55 -> default hit 0.945 / mrr 0.706  (falls off a cliff)
The even split is the peak on both sets, and the peak is sharp: both
neighbours drop, which is what a real signal looks like and noise
does not.
"""
from __future__ import annotations

import os

from astor_memory.server import _resolve_rank_weights


def test_the_default_split_is_even():
    os.environ.pop("ASTOR_BM25_WEIGHT", None)
    os.environ.pop("ASTOR_VEC_WEIGHT", None)
    bm25, vec = _resolve_rank_weights({})
    assert bm25 == 0.5, bm25
    assert vec == 0.5, vec


def test_the_env_can_still_move_both():
    os.environ["ASTOR_BM25_WEIGHT"] = "0.6"
    os.environ["ASTOR_VEC_WEIGHT"] = "0.4"
    try:
        assert _resolve_rank_weights({}) == (0.6, 0.4)
    finally:
        os.environ.pop("ASTOR_BM25_WEIGHT", None)
        os.environ.pop("ASTOR_VEC_WEIGHT", None)


def test_a_body_value_wins_over_the_env():
    os.environ["ASTOR_BM25_WEIGHT"] = "0.6"
    os.environ["ASTOR_VEC_WEIGHT"] = "0.4"
    try:
        assert _resolve_rank_weights(
            {"bm25_weight": 0.5, "vec_weight": 0.5}) == (0.5, 0.5)
    finally:
        os.environ.pop("ASTOR_BM25_WEIGHT", None)
        os.environ.pop("ASTOR_VEC_WEIGHT", None)


def test_a_zero_weight_is_a_real_request():
    """0.0 is the naive lexical arm -- it must not fall through to the
    default."""
    assert _resolve_rank_weights(
        {"bm25_weight": 1.0, "vec_weight": 0.0}) == (1.0, 0.0)
    assert _resolve_rank_weights(
        {"bm25_weight": 0.0, "vec_weight": 1.0}) == (0.0, 1.0)
