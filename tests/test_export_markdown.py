"""tests/test_export_markdown.py — the memory must be readable as an artifact.

The export is what makes astor's memory something a person can open,
read, diff and roll back instead of a database they have to query. The
properties that matter are therefore about the output, not the code
path: one memory per bullet, provenance on every line, duplicates
collapsed, removals separated from relocations, and an index that
points at real files.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from astor_memory.export.markdown import export, render, _read_rows

SCHEMA = """
CREATE TABLE memory_canonical (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER,
    event_id INTEGER,
    namespace TEXT,
    content TEXT,
    kind TEXT,
    confidence REAL,
    importance REAL,
    tags TEXT,
    metadata TEXT,
    promoted_at TEXT,
    promoted_by TEXT,
    last_confirmed_at TEXT,
    access_count INTEGER,
    tombstoned INTEGER DEFAULT 0,
    tombstoned_at TEXT,
    expires_at TEXT,
    scope_type TEXT,
    user_id TEXT,
    tier TEXT,
    entities_json TEXT,
    created_at TEXT,
    memory_class TEXT,
    source_ref TEXT,
    valid_from TEXT,
    valid_until TEXT,
    superseded_by INTEGER,
    invalidated_by TEXT,
    invalidated_at TEXT,
    invalidated_reason TEXT,
    status TEXT,
    verdict TEXT,
    provenance_kind TEXT,
    provenance_agent TEXT,
    provenance_depth INTEGER,
    provenance_at TEXT,
    event_date TEXT,
    event_date_precision TEXT,
    stable_id TEXT,
    revision INTEGER,
    parent_revision_id INTEGER,
    parent_fact_ids TEXT,
    embedding_version INTEGER,
    publishable INTEGER,
    origin_session_id TEXT,
    last_confirmed_session TEXT,
    scene TEXT,
    session_id TEXT,
    keywords TEXT,
    context TEXT,
    evidence_quote TEXT,
    source_hash TEXT,
    visibility TEXT,
    distinct_queries_hit INTEGER
);
"""


def _make_bus(root: Path, rows: list) -> Path:
    d = root / "public" / "memory"
    d.mkdir(parents=True, exist_ok=True)
    db = d / "astor_bus_public.db"
    con = sqlite3.connect(str(db))
    con.executescript(SCHEMA)
    for r in rows:
        cols = ",".join(r.keys())
        q = ",".join("?" * len(r))
        con.execute("INSERT INTO memory_canonical (%s) VALUES (%s)" % (cols, q),
                    list(r.values()))
    con.commit()
    con.close()
    return db


def _row(**kw):
    base = dict(id=1, candidate_id=1, event_id=1, namespace="admin",
                content="", kind="fact", confidence=0.7, importance=0.5,
                tags="[]", metadata="{}", promoted_at=None, promoted_by=None,
                last_confirmed_at=None, access_count=0, tombstoned=0,
                tombstoned_at=None, expires_at=None, scope_type="long_term",
                user_id="admin", tier="public", entities_json=None,
                created_at="2026-01-01T00:00:00Z", memory_class="world_fact",
                source_ref=None, valid_from="2026-01-01T00:00:00Z",
                valid_until=None, superseded_by=None, invalidated_by=None,
                invalidated_at=None, invalidated_reason=None, status="active",
                verdict="settled", provenance_kind="extracted",
                provenance_agent="nest.auto_link", provenance_depth=0,
                provenance_at=None, event_date=None, event_date_precision=None,
                stable_id=None, revision=1, parent_revision_id=None,
                parent_fact_ids=None, embedding_version=1, publishable=0,
                origin_session_id=None, last_confirmed_session=None,
                scene=None, session_id=None, keywords=None, context=None,
                evidence_quote=None, source_hash=None, visibility=None,
                distinct_queries_hit=0)
    base.update(kw)
    return base


@pytest.fixture()
def astor_dir(tmp_path):
    root = tmp_path / "astor"
    rows = [
        _row(id=1, content="the runtime lives on D drive", kind="fact",
             importance=0.9, confidence=0.8),
        _row(id=2, content="never restart the gateway yourself", kind="rule",
             importance=1.0),
        _row(id=3, content="a lesson that paid off", kind="lesson",
             importance=0.7),
        _row(id=4, content="a removed memory", kind="fact",
             tombstoned=1, tombstoned_at="2026-02-01",
             invalidated_reason="user_request"),
        _row(id=5, content="a relocated memory", kind="fact",
             tombstoned=1, tombstoned_at="2026-02-01",
             invalidated_reason="moved out of public tier by v1.16.79 migration"),
    ]
    _make_bus(root, rows)
    return root


def test_export_writes_an_index_and_one_file_per_kind(astor_dir, tmp_path):
    out = tmp_path / "exp"
    rep = export(astor_dir, out, tiers=["public"])
    assert rep["rows"] == 5
    assert (out / "MEMORY.md").exists()
    assert (out / "fact.md").exists()
    assert (out / "rule.md").exists()
    assert (out / "tombstoned.md").exists()


def test_every_bullet_carries_its_provenance(astor_dir, tmp_path):
    out = tmp_path / "exp"
    export(astor_dir, out, tiers=["public"])
    body = (out / "rule.md").read_text(encoding="utf-8")
    line = [l for l in body.split("\n") if l.startswith("- ")][0]
    # The pieces that make a memory reviewable without the database.
    assert "[id:2]" in line
    assert "[kind:rule]" in line
    assert "[conf:0.70]" in line
    assert "[prov:extracted]" in line
    assert "[by:nest.auto_link]" in line
    assert "[from:2026-01-01]" in line


def test_removals_and_relocations_are_separated(astor_dir, tmp_path):
    out = tmp_path / "exp"
    export(astor_dir, out, tiers=["public"])
    body = (out / "tombstoned.md").read_text(encoding="utf-8")
    assert "## removed" in body
    assert "## moved to another tier" in body
    removed = body.split("## removed")[1].split("## moved")[0]
    assert "a removed memory" in removed
    assert "a relocated memory" not in removed


def test_duplicates_across_tiers_collapse_to_one_bullet(tmp_path):
    """The same memory can exist in more than one tier's database. A
    readable export must show it once."""
    root = tmp_path / "astor"
    d = root / "public" / "memory"
    d.mkdir(parents=True, exist_ok=True)
    _make_bus(root, [_row(id=1, content="same memory", importance=0.9)])
    d2 = root / "admin" / "memory"
    d2.mkdir(parents=True, exist_ok=True)
    db2 = d2 / "astor_bus_admin.db"
    con = sqlite3.connect(str(db2))
    con.executescript(SCHEMA)
    r = _row(id=99, content="same memory", importance=0.5)
    con.execute("INSERT INTO memory_canonical (%s) VALUES (%s)"
                % (",".join(r.keys()), ",".join("?" * len(r))), list(r.values()))
    con.commit()
    con.close()

    out = tmp_path / "exp"
    export(root, out, tiers=["public", "admin"])
    body = (out / "fact.md").read_text(encoding="utf-8")
    assert body.count("same memory") == 1
    idx = (out / "MEMORY.md").read_text(encoding="utf-8")
    assert "collapsed" in idx


def test_index_links_point_at_files_that_exist(astor_dir, tmp_path):
    out = tmp_path / "exp"
    export(astor_dir, out, tiers=["public"])
    idx = (out / "MEMORY.md").read_text(encoding="utf-8")
    import re
    for target in re.findall(r"\[\[([^\]]+)\]\]", idx):
        assert (out / target).exists(), "index points at a missing file: %s" % target


def test_dry_run_writes_nothing(astor_dir, tmp_path):
    out = tmp_path / "exp"
    rep = export(astor_dir, out, tiers=["public"], dry_run=True)
    assert rep["dry_run"] is True
    assert not out.exists()


def test_tier_filter_excludes_other_tiers(tmp_path):
    root = tmp_path / "astor"
    d = root / "admin" / "memory"
    d.mkdir(parents=True, exist_ok=True)
    db = d / "astor_bus_admin.db"
    con = sqlite3.connect(str(db))
    con.executescript(SCHEMA)
    r = _row(id=1, content="admin only memory", tier="admin")
    con.execute("INSERT INTO memory_canonical (%s) VALUES (%s)"
                % (",".join(r.keys()), ",".join("?" * len(r))), list(r.values()))
    con.commit()
    con.close()

    rows = _read_rows(root, ["public"])
    assert rows == []
    out = tmp_path / "exp"
    export(root, out, tiers=["admin"])
    assert "admin only memory" in (out / "fact.md").read_text(encoding="utf-8")
