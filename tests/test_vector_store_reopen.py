"""Regression test for v1.14.4 vector_store conn reopen on closed-but-not-None.

Symptom (2026-09-07): astor-server log showed
"sqlite3.ProgrammingError: Cannot operate on a closed database" at
vector_store.py:245 (cold path: rows = self.conn.execute(...).fetchall()).
Root cause: v1.14.3 conn property only reopened when _conn IS None,
not when _conn was a closed Connection object. After CLI teardown or
race conditions, _conn could be a closed-but-not-None handle, causing
subsequent .conn property accesses to return the dead connection.
"""
import os
import sqlite3
import shutil
import tempfile

import numpy as np
import pytest


@pytest.fixture
def vs_db():
    """AstorNest on a temp dir; closes+reopens inside each test."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = os.path.join(td, 't.db')
        from astor_memory.nest.vector_store import AstorNest
        yield AstorNest(db_path=db), db


def test_conn_reopens_when_none(vs_db):
    vs, _ = vs_db
    vs.close()  # sets _conn = None
    # Property must rebuild
    rows = vs.conn.execute("SELECT 1").fetchall()
    assert rows == [(1,)]


def test_conn_reopens_when_closed_but_not_none(vs_db):
    """v1.14.4 regression: closed Connection object (not None) was missed."""
    vs, db = vs_db
    # Inject a closed-but-not-None handle (simulates the production bug)
    closed = sqlite3.connect(db)
    closed.close()
    vs._conn = closed
    # Property must detect and reopen
    rows = vs.conn.execute("SELECT 1").fetchall()
    assert rows == [(1,)]
    # After reopen, _conn must be the new live connection
    assert vs._conn is not closed


def test_open_conn_passes_through(vs_db):
    """Property must not reopen when _conn is already live (perf)."""
    vs, _ = vs_db
    original = vs._conn
    _ = vs.conn  # access property
    assert vs._conn is original  # unchanged


def test_search_after_reopen(vs_db):
    """End-to-end: store+search must work after closed-conn reopen."""
    vs, _ = vs_db
    vs.close()
    # Re-acquire via property
    vs.conn.execute("SELECT 1").fetchall()
    # Store via the actual API (text -> embed)
    vs.store(fact_id=42, text="hello world reopen test", model_name='test')
    # Invalidate cache so search hits DB
    vs._search_cache = {}
    emb = vs._embed("hello world reopen test")
    hits = vs.search(query_embedding=emb, limit=1, model_name='test')
    assert hits, "search after reopen returned empty"
    assert hits[0][0] == 42  # fact_id match


def test_reopen_preserves_schema(vs_db):
    """v1.14.3 patch: _reopen must re-init schema so partial recovery works."""
    vs, _ = vs_db
    vs.close()
    # After reopen, schema must still exist (table queryable, no exceptions)
    rows = vs.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='embeddings'"
    ).fetchall()
    assert rows, "embeddings table missing after reopen"
