"""The provenance read must not silently drop the evidence columns.

v1.14.74 added `evidence_quote`, `source_ref` and `source_hash` to
memory_canonical so a fact could carry the literal source text it was
extracted from. The provenance reader listed all three in its key
tuple while its SELECT asked for none of them -- and `dict(zip(keys,
row))` truncates to the shorter side, so every provenance response
came back without any evidence at all.

A database created before v1.14.74 does not have the columns at all,
and selecting a missing column is an error rather than an empty value,
so the reader has to ask the schema which columns exist.
"""
from __future__ import annotations

import sqlite3

import pytest

from astor_memory.nest import provenance


def _make_db(path, *, with_evidence: bool):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE memory_canonical ("
        " id INTEGER PRIMARY KEY,"
        " content TEXT,"
        " kind TEXT,"
        " importance REAL,"
        " tags TEXT,"
        " metadata TEXT,"
        " event_id TEXT,"
        " namespace TEXT,"
        " user_id TEXT,"
        " promoted_at TEXT,"
        " tombstoned INTEGER DEFAULT 0,"
        " parent_fact_ids TEXT DEFAULT '[]',"
        " provenance_kind TEXT,"
        " provenance_agent TEXT,"
        " provenance_depth INTEGER,"
        " provenance_at TEXT"
        + (", evidence_quote TEXT NOT NULL DEFAULT ''"
           ", source_ref TEXT"
           ", source_hash TEXT" if with_evidence else "")
        + ")"
    )
    if with_evidence:
        con.execute(
            "INSERT INTO memory_canonical"
            " (id, content, kind, importance, tags, metadata, event_id,"
            "  namespace, user_id, promoted_at, tombstoned, parent_fact_ids,"
            "  provenance_kind, provenance_agent, provenance_depth,"
            "  provenance_at, evidence_quote, source_ref, source_hash)"
            " VALUES (1, 'the fact', 'rule', 0.8, '[]', '{}', 'e1',"
            "  'ns', 'admin', '2026-10-27', 0, '[]', 'extracted',"
            "  'agent', 0, '2026-10-27',"
            "  'the literal source text', 'http://example.invalid/x', 'h1')"
        )
    else:
        con.execute(
            "INSERT INTO memory_canonical"
            " (id, content, kind, importance, tags, metadata, event_id,"
            "  namespace, user_id, promoted_at, tombstoned, parent_fact_ids,"
            "  provenance_kind, provenance_agent, provenance_depth,"
            "  provenance_at)"
            " VALUES (1, 'the fact', 'rule', 0.8, '[]', '{}', 'e1',"
            "  'ns', 'admin', '2026-10-27', 0, '[]', 'extracted',"
            "  'agent', 0, '2026-10-27')"
        )
    con.commit()
    con.close()


def _read(tmp_path, with_evidence):
    db = tmp_path / "bus.db"
    _make_db(db, with_evidence=with_evidence)
    monkey = provenance._open_conn
    provenance._open_conn = lambda tier='public', user_id=None: sqlite3.connect(
        "file:%s?mode=ro" % str(db).replace("\\", "/"), uri=True)
    try:
        return provenance._read_fact("public", None, 1)
    finally:
        provenance._open_conn = monkey


def test_evidence_columns_are_returned(tmp_path):
    rec = _read(tmp_path, with_evidence=True)
    assert rec is not None
    assert rec["evidence_quote"] == "the literal source text"
    assert rec["source_ref"] == "http://example.invalid/x"
    assert rec["source_hash"] == "h1"


def test_read_does_not_truncate_the_dict(tmp_path):
    rec = _read(tmp_path, with_evidence=True)
    # Every key the reader declares must be present in the result. A
    # zip() over a longer key tuple silently drops the tail.
    assert set(rec) == {
        "id", "content", "kind", "importance", "tags", "metadata",
        "event_id", "namespace", "user_id", "promoted_at", "tombstoned",
        "parent_fact_ids", "provenance_kind", "provenance_agent",
        "provenance_depth", "provenance_at",
        "evidence_quote", "source_ref", "source_hash",
    }


def test_old_database_without_the_columns_still_reads(tmp_path):
    rec = _read(tmp_path, with_evidence=False)
    assert rec is not None
    assert rec["id"] == 1
    assert "evidence_quote" not in rec
