"""The reranker must see a pool wider than top_k.

The LLM reranker was written to pick the best few out of the top 30
candidates -- llm_rerank.py says so in its own docstring and caps the
prompt at RERANK_K=30. But the read path cut `results` to top_k before
the reranker ever saw it, so the `results[:30]` slice could never hold
more than top_k candidates. The reranker could reorder what it already
had and could never promote a fact the hybrid ranker put at 11..30.

Measured on the paraphrase eval set: 7 of 26 answers sat in that band.

Two bugs, fixed together:
  1. the candidate pool was top_k deep, not 30;
  2. the by-id map was built from `results`, so even a fact the LLM
     picked from the deep pool was dropped when it was assembled back.
"""
from __future__ import annotations

import pytest

FILLER = (
    "a note about unrelated scheduling and calendar arrangements "
    "that has nothing to do with the query at hand"
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path / "astor"))
    from astor_memory.server import create_app
    return create_app(str(tmp_path / "astor")).test_client()


# The read path caches by query, so every test needs its own phrase.
QUERY = "unrelated scheduling calendar entry number"


def _write(client, text, tier="private"):
    r = client.post("/v1/write", json={
        "user": "admin", "tier": tier, "text": text,
    })
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def _seed(client, marker, n=40):
    for i in range(n):
        _write(client, f"{FILLER} {marker} entry number {i}")


def _read(client, marker, top_k=10):
    r = client.post("/v1/read", json={
        "user": "admin", "tier": "private",
        "query": f"{QUERY} {marker}", "top_k": top_k,
    })
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


def _mock_rerank(monkeypatch, chooser):
    """Patch the reranker and capture the pool it was handed."""
    from astor_memory.nest import llm_rerank
    captured = {}

    def fake(query, candidates):
        captured["fids"] = [fid for fid, _ in candidates]
        captured["n"] = len(candidates)
        return chooser(candidates)

    monkeypatch.setattr(llm_rerank, "rerank_candidates", fake)
    monkeypatch.setenv("ASTOR_RERANK", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    return captured


def test_the_reranker_sees_more_than_top_k_candidates(client, monkeypatch):
    """The pool handed to the reranker must be deeper than top_k."""
    _seed(client, "alphamarker")
    seen = _mock_rerank(monkeypatch, lambda c: [])

    _read(client, "alphamarker", top_k=10)

    assert seen.get("n", 0) > 10, (
        "the reranker still only sees top_k candidates (%r); the pool was "
        "cut before it got there" % seen.get("n"))


def test_a_deep_pick_can_enter_the_results(client, monkeypatch):
    """A fact the reranker picks from beyond top_k must survive."""
    _seed(client, "betamarker")
    # Pick the LAST candidate -- the deepest one in the pool.
    seen = _mock_rerank(monkeypatch, lambda c: [c[-1][0]] if c else [])

    body = _read(client, "betamarker", top_k=10)
    results = body.get("results") or []
    assert results, body

    # Before the fix the by-id map was built from `results` (top_k items),
    # so a pick from the deep pool was dropped and never came back.
    picked = seen["fids"][-1]
    ids = [r.get("fact_id") for r in results]
    assert picked in ids, (
        "the reranker picked fact %r from the deep pool but it is not in "
        "the results (%r)" % (picked, ids))


def test_the_result_count_does_not_grow_with_the_pool(client, monkeypatch):
    """A wider pool must not widen the response."""
    _seed(client, "gammamarker")
    _mock_rerank(monkeypatch, lambda c: [fid for fid, _ in c[:5]])

    body = _read(client, "gammamarker", top_k=10)
    assert len(body.get("results") or []) <= 10, body.get("count")
