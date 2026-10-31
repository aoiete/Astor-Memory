"""The peer fanout gate asks a document-level question.

The gate used to ask a corpus-level one: "does this corpus contain
30% of the query's words?". On a node with a few thousand facts that
is almost always true, so the gate classified nearly every question
as answerable locally and the peers were never asked. Measured on a
real node: 24 of 29 questions about subjects the node had never
stored were classified as local hits.

Two things are locked here.

First, the candidate slice. bm25_coverage used to take its candidates
from FTS5 in rowid order, so for a long question the document that
holds the answer fell outside the slice -- the answer was in the
corpus and held every query term, yet bm25_search found it first and
bm25_coverage missed it in 6 of 10 cases. The coverage was measuring
an arbitrary neighbour. Ordering the slice by BM25 rank fixed that,
and bm25_search and bm25_coverage now agree.

Second, the signal itself. idf_coverage measures how much of the
question one document here carries, weighted by IDF, and the gate
fires only when that share is below 40%. The threshold came from a
measurement, not a guess: 40 stored facts scored 91-100% and 29
questions about unknown subjects scored 0-29%.
"""
from __future__ import annotations

import pytest

from astor_memory.nest.lex_index import AstorLex, _tokenize


@pytest.fixture()
def lex(tmp_path, monkeypatch):
    """A fresh lexical index in a temporary ASTOR_DIR."""
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    instance = AstorLex(tier="private", user_id="u")
    yield instance
    try:
        instance.close()
    except Exception:
        pass


def _add(lex, fact_id: int, text: str) -> None:
    lex.index_fact(fact_id, text)


def test_coverage_finds_the_document_that_holds_the_answer(lex):
    """The candidate slice must be ranked, not taken in rowid order.

    Many documents share a common word, so an OR match over a long
    question matches all of them. The one that holds the answer has to
    survive the slice -- otherwise the coverage measures a neighbour
    and the gate cannot tell "this node knows" from "this node has
    something close".
    """
    _add(lex, 1, "alpha alpha alpha alpha shared")
    _add(lex, 2, "alpha alpha alpha alpha shared")
    _add(lex, 3, "alpha alpha alpha alpha shared")
    _add(lex, 4, "alpha alpha alpha alpha shared")
    _add(lex, 5, "alpha alpha alpha alpha shared")
    _add(lex, 6, "alpha alpha alpha alpha shared")
    _add(lex, 7, "alpha alpha alpha alpha shared")
    _add(lex, 8, "alpha alpha alpha alpha shared")
    _add(lex, 9, "alpha alpha alpha alpha shared")
    _add(lex, 10, "alpha alpha alpha alpha shared")
    # the answer, added last so its rowid is past the slice
    _add(lex, 11, "alpha quantum entanglement calibration alpha")

    q = "alpha quantum entanglement calibration"
    share, matched = lex.idf_coverage(q, limit=5)
    assert share > 0.5, f"the answer document was not found: share={share}"
    assert matched >= 3


def test_coverage_is_zero_for_a_subject_the_corpus_never_saw(lex):
    _add(lex, 1, "weixin passcode session hygiene")
    share, _matched = lex.idf_coverage("kubernetes pod sidecar", limit=5)
    assert share == 0.0


def test_rare_term_carries_more_than_common_terms(lex):
    """IDF weighting is the point: a rare word identifies the topic.

    When the only document that matches holds the common word but not
    the rare one, the coverage is a fraction rather than the whole
    question -- the rare term is missing, and a missing rare term is
    what makes the question about something this node does not have.
    """
    for i in range(1, 12):
        _add(lex, i, f"common word filler number {i}")

    # "common" appears in eleven documents, "passcode" in none
    only_common, _m = lex.idf_coverage("common passcode", limit=5)
    assert 0.0 < only_common < 1.0, (
        "a missing rare term must lower the coverage, not be ignored")

    # now the rare term is present, in a document that also holds the
    # common one -- the whole question is covered
    _add(lex, 50, "common word passcode")
    both, _m2 = lex.idf_coverage("common passcode", limit=5)
    assert both == 1.0


def test_coverage_of_a_stored_fact_is_high(lex):
    _add(lex, 1, "the ledger rules for a daily cron job on astor")
    _add(lex, 2, "another note about cron jobs and ledgers entirely")
    q = "the ledger rules for a daily cron job on astor"
    share, _m = lex.idf_coverage(q, limit=5)
    assert share > 0.9


def test_empty_query_is_a_miss_not_a_crash(lex):
    share, matched = lex.idf_coverage("", limit=5)
    assert share == 0.0
    assert matched == 0


def test_gate_threshold_separates_stored_from_unknown(lex):
    """The gate fires below 40% and stays quiet above it."""
    _add(lex, 1, "peer fanout latency threshold measured in seconds")
    for i in range(2, 12):
        _add(lex, i, f"filler document number {i} about unrelated subjects")

    known_share, _m = lex.idf_coverage("peer fanout latency threshold", limit=5)
    unknown_share, _m2 = lex.idf_coverage(
        "quantum entanglement calibration protocol", limit=5)

    assert known_share >= 0.40, "a stored subject must not fan out"
    assert unknown_share < 0.40, "an unknown subject must fan out"


def test_bm25_coverage_and_bm25_search_agree(lex):
    """The two lexical paths must reach the same documents.

    They used to disagree, because one ordered its candidate slice by
    rank and the other did not.
    """
    for i in range(1, 15):
        _add(lex, i, f"shared token document number {i}")
    _add(lex, 99, "shared token rarephrase rarephrase rarephrase")

    q = "shared token rarephrase"
    search_ids = [i for i, _s in lex.bm25_search(q, limit=20)]
    cov_ids = [c[0] for c in lex.bm25_coverage(q, limit=5)]
    assert 99 in search_ids
    assert 99 in cov_ids


def test_tokenizer_gives_the_same_terms_the_index_was_built_with(lex):
    _add(lex, 1, "quantum entanglement calibration")
    tokens = set(_tokenize("quantum entanglement calibration"))
    share, _m = lex.idf_coverage("quantum entanglement calibration", limit=5)
    assert tokens
    assert share > 0.0
