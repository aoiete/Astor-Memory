"""v1.16.66 Ship P24 #1 — raw_chat_chunk dual-layer RAG tests.

Covers:
  - schema migration v16 -> v17 adds the chunk_* columns on events
  - schema migration v4 -> v5 adds the chat_chunk_embeddings nest table
  - bus.append_raw_chat_chunk writes a row with all 4 chunk columns populated
  - AstorNest.index_chat_chunk + search_chat_chunks roundtrip
  - Tier isolation (admin private vs source — different users see only their rows)
"""
import os
import sys
import tempfile
import json

# Ensure astor-memory is on sys.path
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest
import numpy as np


@pytest.fixture
def tmp_astor_home(monkeypatch, tmp_path):
    """Redirect ASTOR_DIR + isolate ACL so tests run on tmp paths."""
    env_dir = tmp_path / 'astor_home'
    env_dir.mkdir()
    monkeypatch.setenv('ASTOR_DIR', str(env_dir))
    # Force lazy singletons to rebuild on next call
    try:
        from astor_memory import _cleanup_nest_singleton
        _cleanup_nest_singleton(None)  # noop if signature mismatch — best-effort
    except Exception:
        pass
    yield env_dir


def _make_bus(tier='private', user_id='admin'):
    from astor_memory.bus.store import astor_bus_for
    return astor_bus_for(tier=tier, user_id=user_id)


def test_v17_events_has_chunk_columns(tmp_astor_home):
    """Migration v16 -> v17 adds chunk_role/window_id/turns/prefix on events."""
    bus = _make_bus()
    cols = {row[1] for row in bus._conn.execute(
        "PRAGMA table_info(events)"
    ).fetchall()}
    assert 'chunk_role' in cols
    assert 'chunk_window_id' in cols
    assert 'chunk_turns' in cols
    assert 'chunk_prefix' in cols


def test_nest_v5_has_chat_chunk_embeddings(tmp_astor_home):
    """Nest migration v4 -> v5 adds chat_chunk_embeddings table."""
    from astor_memory.nest.vector_store import astor_nest
    nest = astor_nest(tier='private', user_id='admin')
    tables = {row[0] for row in nest._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert 'chat_chunk_embeddings' in tables


def test_append_raw_chat_chunk_roundtrip(tmp_astor_home):
    """append_raw_chat_chunk writes a row with all 4 chunk columns."""
    bus = _make_bus()
    turns = [
        {'role': 'user', 'content': 'passport expiry 2025-02-18'},
        {'role': 'assistant', 'content': 'noted, will remind 60 days before'},
    ]
    eid = bus.append_raw_chat_chunk(
        namespace='private:admin',
        agent_id='admin',
        window_id='win-001',
        role='mixed',
        turns=turns,
        prefix='[user=admin ts=2026-10-05 topic=passport]',
    )
    assert eid > 0
    row = bus._conn.execute(
        "SELECT chunk_role, chunk_window_id, chunk_turns, chunk_prefix, action "
        "FROM events WHERE id = ?", (eid,)
    ).fetchone()
    assert row[0] == 'mixed'
    assert row[1] == 'win-001'
    parsed = json.loads(row[2])
    assert parsed == turns
    assert row[3] == '[user=admin ts=2026-10-05 topic=passport]'
    assert row[4] == 'raw_chat_chunk'


def test_index_chat_chunk_and_search_roundtrip(tmp_astor_home):
    """Index a chunk, then search for it via cosine — should rank #1."""
    bus = _make_bus()
    from astor_memory.nest.vector_store import astor_nest
    nest = astor_nest(tier='private', user_id='admin')

    # Seed 3 chunks with semantically distinct topics.
    chunks = [
        ('passport renewal form needs notarization', '[user=admin topic=passport]',
         [{'role': 'user', 'content': 'passport renewal form needs notarization'}]),
        ('kraken maker pocket fill at 100k', '[user=admin topic=kraken]',
         [{'role': 'user', 'content': 'kraken maker pocket fill at 100k'}]),
        ('coffee grind size 18 for v60', '[user=admin topic=coffee]',
         [{'role': 'user', 'content': 'coffee grind size 18 for v60'}]),
    ]
    eids = []
    for i, (text, prefix, turns) in enumerate(chunks):
        eid = bus.append_raw_chat_chunk(
            namespace='private:admin',
            agent_id='admin',
            window_id=f'win-{i:03d}',
            role='user',
            turns=turns,
            prefix=prefix,
        )
        eids.append(eid)
        nest.index_chat_chunk(
            event_id=eid, prefix=prefix, turn_count=len(turns),
            text=text,
        )

    # Query: about passport. The passport chunk should be top-1.
    from astor_memory.nest.embeddings import astor_get_embedding_model
    model = astor_get_embedding_model()
    q_emb = np.array(list(model.embed(['passport'])), dtype=np.float32)[0]
    hits = nest.search_chat_chunks(query_embedding=q_emb, limit=3)
    assert len(hits) == 3
    top = hits[0]
    assert top['event_id'] == eids[0], \
        f"expected passport chunk {eids[0]} as top, got {top['event_id']}"
    assert top['similarity'] > hits[1]['similarity']


def test_tier_isolation(tmp_astor_home):
    """A chunk in source:admin must NOT be visible to source:alice."""
    # Use source tier so two different users have isolated dbs.
    bus_admin = _make_bus(tier='source', user_id='admin')
    bus_alice = _make_bus(tier='source', user_id='alice')

    eid = bus_admin.append_raw_chat_chunk(
        namespace='source:admin',
        agent_id='admin',
        window_id='iso-001',
        role='user',
        turns=[{'role': 'user', 'content': 'admin-only secret'}],
        prefix='[secret]',
    )
    from astor_memory.nest.vector_store import astor_nest
    nest_admin = astor_nest(tier='source', user_id='admin')
    nest_alice = astor_nest(tier='source', user_id='alice')
    nest_admin.index_chat_chunk(
        event_id=eid, prefix='[secret]', turn_count=1,
        text='admin-only secret',
    )

    from astor_memory.nest.embeddings import astor_get_embedding_model
    model = astor_get_embedding_model()
    q_emb = np.array(list(model.embed(['secret'])), dtype=np.float32)[0]
    admin_hits = nest_admin.search_chat_chunks(query_embedding=q_emb, limit=5)
    alice_hits = nest_alice.search_chat_chunks(query_embedding=q_emb, limit=5)
    assert any(h['event_id'] == eid for h in admin_hits)
    assert not any(h['event_id'] == eid for h in alice_hits)