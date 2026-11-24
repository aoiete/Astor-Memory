"""A knowledge_page must INDEX its evidence, not assert rules of its own.

The bug
-------
The topic pages were built from a hard-coded markdown template plus a
"Referenced: [ids]" footer. The template was a second source of truth,
and it had already drifted:

  - it cited "R106: position sizing -- no single stock over
    concentration limit", but R106 appears in ZERO astor facts. The real
    R106 (it lives in the stock-portfolio-strategy skill) is about
    single-token violations and cross-account basis differentials -- a
    different rule entirely.
  - it cited "R130", "11928", "R128", "R129" by number, which is fine
    while those numbers stay stable and is a lie the moment they do not.

A page that states a rule it cannot cite is worse than no page: the
agent reads it as authoritative.

The fix
-------
index_body() builds an index: the title, the evidence count, and the
first line of each evidence fact. The rules live in the evidence rows
(linked as parent_fact_ids), so there is exactly one copy of every rule
and it is the same copy the recall path serves.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from astor_memory.nest.knowledge_pages import index_body  # noqa: E402


def test_empty_evidence_says_so():
    out = index_body("Some Topic", [])
    assert "Some Topic" in out
    assert "no matching facts" in out


def test_lists_every_evidence_row():
    evidence = [
        {'id': 101, 'content': 'first rule text'},
        {'id': 202, 'content': 'second rule text'},
    ]
    out = index_body("Topic", evidence)
    assert '#101' in out
    assert '#202' in out
    assert 'first rule text' in out
    assert 'second rule text' in out
    assert '2 matching fact' in out


def test_body_contains_no_rule_assertions_of_its_own():
    """The body must not contain prose rules -- only the evidence index.

    A rule-shaped sentence in the body is the failure this function
    exists to prevent. Rule markers are things like "must", "always",
    "never", "trim the", "position sizing" -- none of which belong in a
    page whose job is to point at facts.
    """
    evidence = [{'id': 1, 'content': 'some fact content'}]
    out = index_body("Topic", evidence).lower()
    for marker in ('must ', 'always ', 'never ', 'position sizing',
                   'trim the', 'hard rule'):
        assert marker not in out, (
            f"the index body asserted a rule of its own ({marker!r}); "
            "the rules belong in the evidence facts, not in the page")


def test_long_content_is_truncated():
    long_text = 'x' * 500
    out = index_body("Topic", [{'id': 1, 'content': long_text}])
    assert 'x' * 160 in out
    assert 'x' * 161 not in out
    assert out.rstrip().endswith('...')


def test_newlines_are_flattened():
    out = index_body("Topic", [{'id': 1, 'content': 'line one\nline two'}])
    assert 'line one line two' in out


def test_missing_content_key_is_tolerated():
    out = index_body("Topic", [{'id': 7}])
    assert '#7' in out
