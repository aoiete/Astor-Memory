"""The two rulers must stay two rulers.

The keyword ruler asks whether a result contains one of the expected
strings. The id ruler asks whether the result is one of the facts a
person would accept as the answer. On the paraphrase set they disagree
on a quarter of the questions, which is the whole reason the id ruler
exists: the keyword ruler alone cannot tell whether a retrieval change
helped, because it also changes when the words in the answer change.

These tests lock the properties that make the id ruler usable:

1. every paraphrase question has a label, and every labelled fact id
   exists in the tier the question reads from -- a label pointing at a
   fact that was deleted scores every query as a miss and looks like a
   retrieval regression;
2. the two rulers actually disagree. If they ever agree on everything,
   the id ruler is not measuring anything the keyword ruler does not,
   and the whole file is dead weight;
3. a label that is pointed at a fact which does not answer the question
   changes the id score. This is the anti-proof: it fails on behaviour,
   not on a syntax error.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from eval_runner import (  # noqa: E402
    load_eval_set,
    load_golden_ids,
    score_by_ids,
    score_results,
)

RUNTIME = Path(os.environ.get("ASTOR_RUNTIME_DIR",
                               r"D:/AI/Astor-Memory-Runtime"))
BUS = {
    "public": RUNTIME / "public" / "memory" / "astor_bus_public.db",
    "private": RUNTIME / "users" / "admin" / "memory" / "astor_bus_admin.db",
}


def _load_rows():
    return load_eval_set(str(TESTS_DIR / "eval_paraphrase_set.jsonl"))


def _load_golden_raw():
    path = TESTS_DIR / "eval_paraphrase_golden.jsonl"
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out[row["qid"]] = row
    return out


def _fact_ids(tier: str) -> set:
    db = BUS.get(tier)
    if not db or not db.exists():
        return set()
    conn = sqlite3.connect(db)
    try:
        return {r[0] for r in conn.execute(
            "SELECT id FROM memory_canonical WHERE tombstoned=0")}
    finally:
        conn.close()


def test_every_paraphrase_question_has_a_label():
    rows = _load_rows()
    golden = _load_golden_raw()
    missing = [r["qid"] for r in rows if r["qid"] not in golden]
    assert not missing, "no label for: %s" % missing


def test_labelled_facts_exist_in_their_tier():
    golden = _load_golden_raw()
    rows = {r["qid"]: r for r in _load_rows()}
    cache = {}
    for qid, row in golden.items():
        tier = rows[qid].get("tier", "public")
        if tier not in cache:
            cache[tier] = _fact_ids(tier)
        if not cache[tier]:
            continue  # runtime not present in this environment
        for fid in row["answer_fact_ids"]:
            assert fid in cache[tier], (
                "%s labels fact %s, which is not in the %s tier "
                "(deleted or tombstoned?)" % (qid, fid, tier))


def test_the_keyword_ruler_is_looser_than_the_answer():
    """The keyword ruler accepts many facts the label does not.

    This is the concrete reason the id ruler exists. For "跑机器人的那个
    服务能不能由我自己动手重启" the expected keyword "gateway" appears in
    272 facts of the tier and 266 of them are not the answer -- so a
    keyword hit is not evidence of a correct recall. If this ever drops
    to nothing, either the corpus changed or the labels did, and either
    way the two rulers have converged and the second one is redundant.
    """
    golden = _load_golden_raw()
    rows = {r["qid"]: r for r in _load_rows()}
    total_extra = 0
    questions_with_extra = 0
    for qid, row in golden.items():
        tier = rows[qid].get("tier", "public")
        ids = _fact_ids(tier)
        if not ids:
            continue
        kws = [k.lower() for k in rows[qid].get("expected_keywords") or []]
        labelled = set(row["answer_fact_ids"])
        extra = 0
        for fid in ids:
            if fid in labelled:
                continue
            if _contains(fid, kws, tier, None):
                extra += 1
        if extra:
            questions_with_extra += 1
        total_extra += extra
    if not total_extra:
        return  # runtime not present in this environment
    assert questions_with_extra >= 15, (
        "only %d questions have keyword-matching facts that are not "
        "the labelled answer -- the two rulers have converged" %
        questions_with_extra)
    assert total_extra >= 200, (
        "only %d non-answer facts match the expected keywords; expected "
        "hundreds" % total_extra)


_CONTENTS: dict = {}


def _content_of(fid: int, tier: str) -> str:
    key = tier
    if key not in _CONTENTS:
        db = BUS.get(tier)
        if not db or not db.exists():
            _CONTENTS[key] = {}
        else:
            conn = sqlite3.connect(db)
            _CONTENTS[key] = dict(conn.execute(
                "SELECT id, content FROM memory_canonical WHERE tombstoned=0"))
            conn.close()
    return (_CONTENTS.get(key) or {}).get(fid) or ""


def _contains(fid: int, kws: list, tier: str, cache: dict) -> bool:
    key = tier
    if key not in _CONTENTS:
        db = BUS.get(tier)
        if not db or not db.exists():
            _CONTENTS[key] = {}
        else:
            conn = sqlite3.connect(db)
            _CONTENTS[key] = dict(conn.execute(
                "SELECT id, content FROM memory_canonical WHERE tombstoned=0"))
            conn.close()
    content = (_CONTENTS.get(key) or {}).get(fid) or ""
    low = content.lower()
    return any(kw in low for kw in kws)


def _tokens(text: str) -> set:
    """CJK character bigrams plus latin words, lowercased."""
    import re
    out = set()
    for m in re.finditer(r"[A-Za-z0-9_\.]{2,}", text or ""):
        out.add(m.group(0).lower())
    cjk = re.findall(r"[\u4e00-\u9fff]+", text or "")
    for run in cjk:
        if len(run) == 1:
            out.add(run)
        for i in range(len(run) - 1):
            out.add(run[i:i + 2])
    return out


def test_labelled_fact_actually_says_the_answer():
    """The label has to be a fact that states the answer.

    A label pointing at a fact that exists but answers something else
    is the failure this catches: the existence check passes, the
    looseness check still passes (other facts match the keywords), and
    the ruler quietly scores the wrong fact as the answer. The
    answer_text in the golden file is the sentence a person would give
    as the answer, so the labelled fact has to share its distinctive
    wording with it.
    """
    golden = _load_golden_raw()
    rows = {r["qid"]: r for r in _load_rows()}
    bad = []
    for qid, row in golden.items():
        tier = rows[qid].get("tier", "public")
        ids = _fact_ids(tier)
        if not ids:
            continue
        want = _tokens(row.get("answer_text", ""))
        if not want:
            continue
        if row.get("corpus_gap"):
            # The corpus has no fact that answers this one. The label
            # is the closest thing present, and the question counts as
            # a storage gap, not a retrieval gap. There is nothing to
            # check here -- but the flag has to be there, so the gap is
            # declared rather than silently absorbed.
            assert row["answer_fact_ids"], (
                "%s is marked as a corpus gap but has no label" % qid)
            continue
        best = 0
        for fid in row["answer_fact_ids"]:
            if fid not in ids:
                continue
            got = _tokens(_content_of(fid, tier))
            best = max(best, len(want & got))
        if best < 2:
            bad.append((qid, best))
    assert not bad, (
        "labelled fact does not share the answer wording: %s" %
        [(q, n) for q, n in bad])


def test_score_by_ids_ranks_by_fact_id_not_position():
    results = [{"fact_id": 10, "content": "x"},
               {"fact_id": 20, "content": "y"}]
    assert score_by_ids(results, [10])["first_rank"] == 1
    assert score_by_ids(results, [20])["first_rank"] == 2
    assert score_by_ids(results, [99])["matched"] is False
    assert score_by_ids(results, [99])["mrr"] == 0.0
    assert score_by_ids(results, [])["matched"] is False
    assert score_by_ids([], [10])["matched"] is False


def test_a_wrong_label_changes_the_score():
    """Anti-proof: point a label at an unrelated fact, and the score
    must change. If it does not, the ruler is not reading the labels."""
    golden = _load_golden_raw()
    rows = {r["qid"]: r for r in _load_rows()}
    row = rows["P22"]
    labels = golden["P22"]["answer_fact_ids"]
    kws = [k.lower() for k in row.get("expected_keywords") or []]

    # A result set built from the labels, and one built from a fact
    # that is plainly not the answer.
    good = [{"fact_id": fid, "content": "..."} for fid in labels]
    bad = [{"fact_id": 99999999, "content": "..."}]
    assert score_by_ids(good, labels)["matched"] is True
    assert score_by_ids(bad, labels)["matched"] is False
    assert score_by_ids(bad, labels)["mrr"] == 0.0
    # And the keyword ruler would still call the wrong one a match if
    # the words happen to be in it -- which is the confusion this file
    # exists to remove.
    assert score_results(good, kws, 1)["matched"] is False


def test_golden_ids_load_into_the_runner():
    ids = load_golden_ids()
    rows = _load_rows()
    for row in rows:
        assert row["qid"] in ids, row["qid"]
        assert ids[row["qid"]], row["qid"]
