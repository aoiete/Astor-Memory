"""The LLM call sites must be able to talk to the operator's own model.

astor shipped with every LLM call site hardwired to OpenRouter. The
operator runs a different model (MiniMax-M3), and the reranker needs
three things the OpenRouter default could not give:

  - a base URL the operator controls (ASTOR_LLM_BASE_URL);
  - that provider's key (ASTOR_LLM_API_KEY);
  - enough token budget for a reasoning model to think and then answer
    (MiniMax-M3.1-Flash spends ~300 tokens thinking before it answers;
    the old max_tokens=200 meant the answer never arrived).

And a reasoning model puts its thinking in <think>...</think>, or in a
separate reasoning_content field, so the parser has to cope.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from astor_memory.nest import hyde, llm_rerank


def test_the_reranker_reads_the_operator_slot(monkeypatch):
    monkeypatch.setenv("ASTOR_LLM_BASE_URL", "https://api.minimax.io/v1")
    monkeypatch.setenv("ASTOR_LLM_API_KEY", "operator-key")
    monkeypatch.setenv("ASTOR_RERANK_MODEL", "MiniMax-M3.1-Flash")
    import importlib
    mod = importlib.reload(llm_rerank)
    try:
        assert mod.BASE_URL == "https://api.minimax.io/v1"
        assert mod.OPENROUTER_API_KEY == "operator-key"
        assert mod.RERANK_MODEL == "MiniMax-M3.1-Flash"
        # The request goes to the operator's host, not to OpenRouter.
        assert "openrouter.ai" not in mod.BASE_URL
    finally:
        monkeypatch.undo()
        importlib.reload(llm_rerank)


def test_the_reranker_budget_fits_a_reasoning_model(monkeypatch):
    """200 tokens is not enough for a reasoning model to answer."""
    import importlib
    monkeypatch.setenv("ASTOR_RERANK_MAX_TOKENS", "1024")
    mod = importlib.reload(llm_rerank)
    try:
        assert mod.RERANK_MAX_TOKENS == 1024
        assert mod.RERANK_MAX_TOKENS > 200
    finally:
        monkeypatch.undo()
        importlib.reload(llm_rerank)


def test_hyde_reads_the_operator_slot(monkeypatch):
    import importlib
    monkeypatch.setenv("ASTOR_LLM_BASE_URL", "https://api.minimax.io/v1")
    monkeypatch.setenv("ASTOR_LLM_API_KEY", "operator-key")
    mod = importlib.reload(hyde)
    try:
        assert mod.BASE_URL == "https://api.minimax.io/v1"
        assert mod.OPENROUTER_API_KEY == "operator-key"
    finally:
        monkeypatch.undo()
        importlib.reload(hyde)


_REASONING_RESPONSE = {
    "choices": [{
        "message": {
            "role": "assistant",
            "reasoning_content": "thinking about it for a while",
            "content": '<think>still thinking</think>\n{"ids": [8344, 8345]}',
        },
    }],
}


def test_the_parser_understands_a_thinking_model(monkeypatch):
    """The answer may arrive wrapped in <think>...</think>."""
    import importlib
    mod = importlib.reload(llm_rerank)
    try:
        seen = {}

        class _Resp:
            def read(self):
                return json.dumps(_REASONING_RESPONSE).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(mod, "OPENROUTER_API_KEY", "k")
        monkeypatch.setattr(mod.urllib.request, "urlopen",
                            lambda *a, **k: _Resp())
        out = mod._call_rerank_llm("q", [(8344, "a"), (8345, "b"), (1, "c")])
        assert out == [8344, 8345], out
    finally:
        monkeypatch.undo()
        importlib.reload(llm_rerank)


def test_the_parser_understands_a_reasoning_content_field(monkeypatch):
    """MiniMax-M3.1-Flash sometimes puts the answer in content and the
    thinking in reasoning_content -- and sometimes the other way."""
    import importlib
    mod = importlib.reload(llm_rerank)
    try:
        payload = {
            "choices": [{"message": {
                "role": "assistant",
                "reasoning_content": "<think>hmm</think>[8344, 8345]",
                "content": None,
            }}],
        }

        class _Resp:
            def read(self):
                return json.dumps(payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(mod, "OPENROUTER_API_KEY", "k")
        monkeypatch.setattr(mod.urllib.request, "urlopen",
                            lambda *a, **k: _Resp())
        out = mod._call_rerank_llm("q", [(8344, "a"), (8345, "b"), (1, "c")])
        assert out == [8344, 8345], out
    finally:
        monkeypatch.undo()
        importlib.reload(llm_rerank)


if __name__ == "__main__":
    unittest_main = pytest.main
    sys.exit(unittest_main([__file__, "-q"]))
