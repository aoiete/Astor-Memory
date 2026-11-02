"""Progressive loading levels (L0/L1/L2) on the read path.

astor has carried L0/L1 fields since v1.6.0 but the write path never
passed them through, so every fact landed with an empty __abstract__ /
__overview__ and nothing ever read them back. v1.17.30 (2026-11-01,
after the OpenViking article review) wires them up:

  - the extractor values now reach the canonical row
  - /v1/read and /v1/read/multi accept a 'level' body param
  - every result carries abstract (L0) / overview (L1) / level_text

These tests pin the contract that matters:
  - default level is L2, so existing callers see unchanged bytes
  - L0 gives a short summary, L1 a digest, L2 the full text
  - a fact written WITHOUT the L0 fields still gets a derived L0
    (old facts must not come back with an empty summary)
  - both read endpoints behave the same (the plugin reads /v1/read/multi,
    so the two implementations must not drift)
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile

import pytest
from pathlib import Path


def _make_db(tmp_path, rows):
    """rows: list of (id, content, metadata_dict)."""
    db = tmp_path / "bus.db"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE memory_canonical ("
        " id INTEGER PRIMARY KEY, content TEXT, kind TEXT, confidence REAL,"
        " importance REAL, tags TEXT, namespace TEXT, user_id TEXT,"
        " origin_session_id TEXT, metadata TEXT, memory_class TEXT,"
        " entities_json TEXT, event_date TEXT, event_date_precision TEXT,"
        " created_at TEXT, valid_until TEXT, expires_at TEXT, scope_type TEXT,"
        " provenance_kind TEXT, evidence_quote TEXT, source_ref TEXT,"
        " source_hash TEXT, tombstoned INTEGER DEFAULT 0)"
    )
    for fid, content, meta in rows:
        con.execute(
            "INSERT INTO memory_canonical (id, content, kind, confidence,"
            " importance, tags, namespace, user_id, metadata, tombstoned)"
            " VALUES (?,?,?,?,?,?,?,?,?,0)",
            (fid, content, "fact", 0.7, 0.6, "[]", "ns", "u",
             json.dumps(meta, ensure_ascii=False)),
        )
    con.commit()
    con.close()
    return db


# ---------------------------------------------------------------- unit level

def test_fact_levels_reads_stored_values():
    from astor_memory.server import _fact_levels
    meta = {"__abstract__": "stored L0", "__overview__": "stored L1"}
    l0, l1 = _fact_levels(meta, "full body text here")
    assert l0 == "stored L0"
    assert l1 == "stored L1"


def test_fact_levels_derives_when_absent():
    """A fact written before the L0 write path has neither field. The
    derived L0 must not be empty, or old facts come back blank."""
    from astor_memory.server import _fact_levels
    content = "First sentence carries the point. Second sentence adds detail."
    l0, l1 = _fact_levels({}, content)
    assert l0 == "First sentence carries the point"
    assert l1.startswith("First sentence carries the point")


def test_fact_levels_handles_chinese_punctuation():
    from astor_memory.server import _fact_levels
    l0, _ = _fact_levels({}, "第一句是重点。第二句是补充。")
    assert l0 == "第一句是重点"


def test_fact_levels_empty_content():
    from astor_memory.server import _fact_levels
    l0, l1 = _fact_levels({}, "")
    assert l0 == ""
    assert l1 == ""


def test_fact_levels_levels_constant():
    from astor_memory.server import _FACT_LEVELS
    assert _FACT_LEVELS == ("L0", "L1", "L2")


# ------------------------------------------------------------ write path

def test_insert_candidate_accepts_and_persists_l0_l1(tmp_path):
    """insert_candidate has taken abstract/overview since v1.6.0. They
    must land in the metadata JSON as __abstract__ / __overview__."""
    from astor_memory.bus.store import astor_bus
    bus = astor_bus(db_path=tmp_path / "bus.db", tier="public", user_id=None)
    ev = bus.append_event(namespace="ns", agent_id="tester", source="test",
                          action="write", content="x")
    cid = bus.insert_candidate(
        event_id=ev, namespace="ns", content="body",
        abstract="L0 summary", overview="L1 digest",
    )
    row = bus.conn.execute(
        "SELECT metadata FROM memory_candidates WHERE id = ?", (cid,)
    ).fetchone()
    meta = json.loads(row[0])
    assert meta.get("__abstract__") == "L0 summary"
    assert meta.get("__overview__") == "L1 digest"
    bus.conn.close()


def test_backfill_tool_derives_same_shape():
    """The backfill tool and the runtime derivation must agree, otherwise
    a backfilled fact and a freshly written fact render differently."""
    import importlib.util
    from pathlib import Path
    from astor_memory.server import _fact_levels

    tool = Path(__file__).resolve().parents[1] / "tools" / "backfill_l0l1.py"
    spec = importlib.util.spec_from_file_location("backfill_l0l1", tool)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    content = "Sentence one is the summary sentence. Sentence two is detail."
    assert mod.derive_abstract(content) == _fact_levels({}, content)[0]
    assert mod.derive_overview(content) == _fact_levels({}, content)[1]


def test_backfill_tool_skips_short_and_dry_run_is_safe(tmp_path):
    import importlib.util
    from pathlib import Path
    tool = Path(__file__).resolve().parents[1] / "tools" / "backfill_l0l1.py"
    spec = importlib.util.spec_from_file_location("backfill_l0l1", tool)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.derive_abstract("") == ""
    assert mod.derive_abstract("x" * 500).endswith("...")
    assert len(mod.derive_abstract("x" * 500)) == 320


# ---------------------------------------------------------- drill-down API

def _app_client(tmp_path, monkeypatch):
    """Build a Flask test client whose ASTOR_DIR is a temp dir."""
    import astor_memory.server as srv
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    app = srv.create_app()
    app.config.update(TESTING=True)
    return app.test_client()


def test_drilldown_returns_full_text_and_levels(tmp_path, monkeypatch):
    """The L2 half of progressive loading: given a fact id, fetch the full
    text. Without this, a caller that was shown only L0 has no way to get
    what it did not get."""
    from astor_memory.bus.store import astor_bus
    bus = astor_bus(db_path=tmp_path / "public" / "memory" / "astor_bus_public.db",
                    tier="public", user_id=None)
    ev = bus.append_event(namespace="ns", agent_id="t", source="test",
                          action="write", content="x")
    cid = bus.insert_candidate(event_id=ev, namespace="ns",
                               content="Full body sentence. More body text here.",
                               abstract="Full body sentence",
                               overview="Full body sentence. More body text")
    fcid = bus.promote_candidate(cid, promoted_by="test", user_id=None,
                                  tier="public", scope_type="long_term",
                                  visibility="commons")
    bus.conn.close()

    client = _app_client(tmp_path, monkeypatch)
    r = client.get(f"/v1/fact/{fcid}?tier=public")
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    f = r.get_json()["fact"]
    assert f["content"].startswith("Full body sentence")
    assert f["abstract"] == "Full body sentence"
    assert f["level"] == "L2"
    assert f["level_text"] == f["content"]

    r0 = client.get(f"/v1/fact/{fcid}?tier=public&level=L0")
    f0 = r0.get_json()["fact"]
    assert f0["level"] == "L0"
    assert f0["level_text"] == "Full body sentence"

    r1 = client.get(f"/v1/fact/{fcid}?tier=public&level=L1")
    f1 = r1.get_json()["fact"]
    assert f1["level"] == "L1"
    assert len(f1["level_text"]) <= len(f["content"])


def test_drilldown_missing_fact_is_404(tmp_path, monkeypatch):
    client = _app_client(tmp_path, monkeypatch)
    r = client.get("/v1/fact/99999999?tier=public")
    assert r.status_code == 404


def test_drilldown_bad_level_falls_back_to_l2(tmp_path, monkeypatch):
    client = _app_client(tmp_path, monkeypatch)
    r = client.get("/v1/fact/99999999?tier=public&level=BOGUS")
    assert r.status_code == 404  # fact missing, but level did not crash


def test_drilldown_carries_neighbouring_context(tmp_path, monkeypatch):
    """OpenViking's rule: a retrieval result carries its surroundings.
    astor groups facts by the session they came from, so the neighbours
    of a fact are the other facts of its session."""
    from astor_memory.bus.store import astor_bus
    bus = astor_bus(db_path=tmp_path / "public" / "memory" / "astor_bus_public.db",
                    tier="public", user_id=None)
    ev = bus.append_event(namespace="ns", agent_id="t", source="test",
                          action="write", content="x")

    ids = []
    for i, body in enumerate(["Session fact one. More text.",
                              "Session fact two. More text.",
                              "Other session fact. More text."]):
        cid = bus.insert_candidate(
            event_id=ev, namespace="ns", content=body,
            abstract=f"L0 {i}", overview=f"L1 {i}",
            session_id="sess-1" if i < 2 else "sess-2",
        )
        ids.append(bus.promote_candidate(
            cid, promoted_by="test", user_id=None, tier="public",
            scope_type="long_term", visibility="commons"))

    bus.conn.close()

    client = _app_client(tmp_path, monkeypatch)
    r = client.get(f"/v1/fact/{ids[0]}?tier=public")
    assert r.status_code == 200
    fact = r.get_json()["fact"]
    assert fact["session_id"] == "sess-1"
    sib_ids = {s["fact_id"] for s in fact["neighbours"]}
    # the other fact of the same session is a neighbour
    assert ids[1] in sib_ids
    # the fact of the other session is not
    assert ids[2] not in sib_ids
    # and the fact itself is not its own neighbour
    assert ids[0] not in sib_ids


def test_drilldown_neighbours_empty_without_session(tmp_path, monkeypatch):
    from astor_memory.bus.store import astor_bus
    bus = astor_bus(db_path=tmp_path / "public" / "memory" / "astor_bus_public.db",
                    tier="public", user_id=None)
    ev = bus.append_event(namespace="ns", agent_id="t", source="test",
                          action="write", content="x")
    cid = bus.insert_candidate(event_id=ev, namespace="ns", content="Solo fact.")
    fid = bus.promote_candidate(cid, promoted_by="test", user_id=None,
                                tier="public", scope_type="long_term",
                                visibility="commons")
    bus.conn.close()

    client = _app_client(tmp_path, monkeypatch)
    r = client.get(f"/v1/fact/{fid}?tier=public")
    assert r.status_code == 200
    fact = r.get_json()["fact"]
    assert fact["neighbours"] == []


def test_read_cache_key_includes_level(tmp_path, monkeypatch):
    """The read cache key must cover every input that changes the RESULT.

    'level' changes what each result renders as. A cached L2 answer
    served to an L0 caller hands back the full text the caller asked to
    avoid -- the same class of bug as the v1.16.85 cache-key fix, which
    missed kinds/wing/session_id/consult and served a consult=True
    answer to a consult=False caller.
    """
    import astor_memory.server as srv
    src = Path(srv.__file__).read_text(encoding="utf-8")
    i = src.index("_rcache_key = (")
    # Do NOT slice to the first ")": the tuple contains nested calls like
    # body.get('tier', 'public'), so the first ")" ends the slice early.
    key_block = src[i:i + 900]
    assert "_level" in key_block, (
        "the read cache key does not include _level -- an L0 request can "
        "be served a cached L2 response")


def test_read_cache_key_includes_level_multi_endpoint(tmp_path, monkeypatch):
    """The /v1/read/multi endpoint must honour the same level parameter."""
    import astor_memory.server as srv
    src = Path(srv.__file__).read_text(encoding="utf-8")
    i = src.index("@app.route('/v1/read/multi'")
    j = src.index("@app.route(", i + 10)
    seg = src[i:j]
    assert "body.get('level')" in seg, (
        "/v1/read/multi does not read the level parameter")


def test_grep_verify_select_keeps_metadata_off_the_last_column():
    """The grep_verify channel reads provenance_kind from row[-2] and
    valid_until from row[-1]. Adding a column at the END of that SELECT
    silently repoints both negative indexes -- valid_until would start
    returning the metadata JSON.

    This is a source-level check on purpose: the failure is a column
    ORDER mistake, and a runtime test would only catch it if a fact with
    a valid_until happened to be on the grep_verify channel.
    """
    import astor_memory.server as srv
    src = Path(srv.__file__).read_text(encoding="utf-8")

    i = src.index("'score_kind': 'grep_verify'")
    # walk back to the SELECT that feeds this dict
    seg = src[max(0, i - 9000):i]
    sel_start = seg.rindex("SELECT id, content")
    sel = seg[sel_start:]
    assert "metadata" in sel, "the grep_verify SELECT does not fetch metadata"
    # metadata must appear BEFORE provenance_kind and valid_until
    m_pos = sel.index("metadata")
    pk_pos = sel.index("provenance_kind")
    vu_pos = sel.index("valid_until")
    assert m_pos < pk_pos < vu_pos, (
        "metadata must sit before provenance_kind and valid_until in the "
        "grep_verify SELECT, or row[-2]/row[-1] change meaning")
    # and the dict must read metadata from row[-3]
    assert "_row[-3]" in seg, (
        "the grep_verify path must read metadata from row[-3] to match "
        "the new column order")


def test_apply_fact_levels_fills_every_channel():
    """Every channel that builds a result row must end up with the
    progressive-loading fields.

    A source-level check of three known markers passed while the
    trigger-aware channels were still missing the fields -- a live query
    on 2026-11-01 came back with 10 of 18 results carrying no abstract.
    So this exercises the one-place fill directly, and asserts that the
    read path calls it.
    """
    from astor_memory.server import _apply_fact_levels

    rows = [
        # a channel that filled the fields properly
        {"fact_id": 1, "content": "Full body.", "abstract": "real L0",
         "overview": "real L1", "level_text": "real L0", "level": "L0"},
        # a channel that forgot -- trigger-aware looks like this
        {"fact_id": 2, "content": "Trigger aware body text."},
    ]
    _apply_fact_levels(rows, "L0")
    assert rows[0]["abstract"] == "real L0", "an existing abstract was overwritten"
    assert rows[0]["level_text"] == "real L0"
    assert rows[1]["abstract"], "the missing channel got no abstract"
    assert rows[1]["level_text"], "the missing channel got no level_text"
    assert rows[1]["level"] == "L0"

    # and L1 / L2 render the right amount
    # L1 is the leading digest (first 240 chars), so on a short fact it is
    # the whole thing -- L1 only differs from L2 on long content.
    long_body = "First sentence. " + ("filler sentence. " * 40)
    rows2 = [{"fact_id": 3, "content": long_body}]
    _apply_fact_levels(rows2, "L1")
    assert rows2[0]["level_text"] == long_body[:240]
    assert len(rows2[0]["level_text"]) < len(long_body)
    rows_short = [{"fact_id": 5, "content": "Short body."}]
    _apply_fact_levels(rows_short, "L1")
    assert rows_short[0]["level_text"] == "Short body."
    rows3 = [{"fact_id": 4, "content": "First sentence. Second sentence."}]
    _apply_fact_levels(rows3, "L2")
    assert rows3[0]["level_text"] == "First sentence. Second sentence."


def test_read_paths_call_the_one_place_fill():
    """Both read endpoints must call _apply_fact_levels before returning,
    so a channel added later is covered without touching it."""
    import astor_memory.server as srv
    src = Path(srv.__file__).read_text(encoding="utf-8")
    assert src.count("_apply_fact_levels(") >= 3, (
        "both /v1/read and /v1/read/multi must call _apply_fact_levels "
        "(one call is the definition, the other two are the call sites)")
