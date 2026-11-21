"""The health check has to see the lexical index.

v1.17.78

astor has three stores: the bus (the facts), the nest (their
embeddings) and the lexical index (their inverted index for BM25).
The health endpoint probed two of them.

If the lexical index's file goes away or becomes unreadable, sqlite
opens a fresh empty database in its place and BM25 returns nothing.
The hybrid path then quietly becomes vector-only, and health keeps
saying ok. On the paraphrase set hybrid beats vector-only by 0.077 hit
rate, so the degradation is real and invisible at the same time.

This is the health-probe half of a deployment note that says the
production split between the business database and the vector
database needs a probe on it. astor splits three ways, so it probes
three ways.

The status stays "ok" when the index is missing: the server still
serves, retrieval is just weaker, and a restart would not bring an
index back. A failing health check would only invite a restart loop.
The degradation is reported instead.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path / "astor"))
    from astor_memory.server import create_app
    return create_app(str(tmp_path / "astor")).test_client()


def _lex_path(root: Path) -> Path:
    return root / "lex" / "memory" / "astor_lex_public.db"


def _write_lex(root: Path) -> Path:
    p = _lex_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    try:
        conn.execute("CREATE TABLE documents (fact_id INTEGER, content TEXT)")
        conn.execute("INSERT INTO documents VALUES (1, 'a fact')")
        conn.commit()
    finally:
        conn.close()
    return p


def test_a_missing_lex_index_is_reported(client, tmp_path):
    body = client.get("/v1/health").get_json()
    assert body["dbs"].get("lex") == "missing", body
    assert "lex" in body.get("degraded", []), body
    # The server is still serving: a missing index is weaker
    # retrieval, not a dead server.
    assert body["status"] == "ok", body


def test_a_present_lex_index_is_ok(client, tmp_path):
    root = tmp_path / "astor"
    _write_lex(root)
    body = client.get("/v1/health").get_json()
    assert body["dbs"].get("lex") == "ok", body
    assert "degraded" not in body, body


def test_the_probe_does_not_create_the_file(client, tmp_path):
    """A probe must not make the thing it is looking for."""
    root = tmp_path / "astor"
    client.get("/v1/health")
    assert not _lex_path(root).exists(), (
        "the health check created the lexical index it was checking for")
