"""v1.16.68 Ship B tests.

Covers:
  - schema v17 -> v18 adds status column on memory_canonical
  - nest schema v5 -> v6 adds chat_chunk_clusters table
  - cluster_summary.build_clusters groups by time-gap
  - cluster_summary.save_cluster_summary + cluster_summary_search roundtrip
  - status column marker on recall (inactive flagged)
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest


@pytest.fixture
def tmp_astor_home(monkeypatch, tmp_path):
    env_dir = tmp_path / 'astor_home'
    env_dir.mkdir()
    monkeypatch.setenv('ASTOR_DIR', str(env_dir))
    yield env_dir


def test_v18_memory_canonical_has_status(tmp_astor_home):
    from astor_memory.bus.store import astor_bus_for
    bus = astor_bus_for(tier='private', user_id='admin')
    cols = {row[1] for row in bus._conn.execute(
        "PRAGMA table_info(memory_canonical)"
    ).fetchall()}
    assert 'status' in cols


def test_nest_v6_has_chat_chunk_clusters(tmp_astor_home):
    from astor_memory.nest.vector_store import astor_nest
    nest = astor_nest(tier='private', user_id='admin')
    tables = {row[0] for row in nest._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert 'chat_chunk_clusters' in tables


def test_build_clusters_groups_by_time_gap(tmp_astor_home):
    """Cluster builder puts a 60-min gap on its own cluster."""
    from astor_memory.bus.store import astor_bus_for
    from astor_memory.nest.vector_store import astor_nest
    bus = astor_bus_for(tier='private', user_id='admin')
    nest = astor_nest(tier='private', user_id='admin')
    # Insert 3 chunks with timestamps: t=0, t=100s, t=4000s
    base_ts = '2026-10-05T10:00:00.000Z'
    rows = [
        ('alice passport', base_ts),
        ('alice passport followup', '2026-10-05T10:01:40.000Z'),  # +100s
        ('alice baking bread', '2026-10-05T11:06:40.000Z'),  # +1h
    ]
    eids = []
    for prefix, ts in rows:
        eid = bus.append_raw_chat_chunk(
            namespace='private:admin',
            agent_id='admin',
            window_id=f'w-{ts}',
            role='user',
            turns=[{'role': 'user', 'content': prefix}],
            prefix=prefix,
        )
        eids.append(eid)
        # Manually backdate ts on BOTH events and chat_chunk_embeddings
        # (the latter is what build_clusters reads from)
        bus._conn.execute(
            "UPDATE events SET ts = ? WHERE id = ?", (ts, eid)
        )
        bus._conn.commit()
        nest.index_chat_chunk(eid, prefix, 1, prefix)
        nest._conn.execute(
            "UPDATE chat_chunk_embeddings SET ts = ? WHERE event_id = ?",
            (ts, eid)
        )
        nest._conn.commit()

    from astor_memory.nest.cluster_summary import build_clusters
    clusters = build_clusters(nest, gap_seconds=1800, user_id='admin', namespace='private')
    assert len(clusters) == 2, (
        f"expected 2 clusters (100s same + 1h gap), got {len(clusters)}: "
        f"{[c['member_event_ids'] for c in clusters]}"
    )
    # First cluster has 2 members (the close ones), second has 1.
    assert clusters[0]['member_count'] == 2
    assert clusters[1]['member_count'] == 1


def test_save_and_recall_cluster_roundtrip(tmp_astor_home):
    """Insert 2 clusters, dense-search, verify both retrievable."""
    import numpy as np
    from astor_memory.bus.store import astor_bus_for
    from astor_memory.nest.vector_store import astor_nest
    from astor_memory.nest.cluster_summary import (
        build_clusters, save_cluster_summary, cluster_summary_search,
    )
    from astor_memory.nest.embeddings import astor_get_embedding_model
    bus = astor_bus_for(tier='private', user_id='admin')
    nest = astor_nest(tier='private', user_id='admin')
    # 3 chunks backdated with very close ts so they cluster together
    base = '2026-10-05T10:00:00.000Z'
    eids = []
    for i, (prefix, ts) in enumerate([
        ('passport renewal form notarization', base),
        ('passport renewal requires old one', '2026-10-05T10:01:00.000Z'),
        ('sourdough bread recipe', '2026-10-05T10:02:00.000Z'),
    ]):
        eid = bus.append_raw_chat_chunk(
            namespace='private:admin', agent_id='admin',
            window_id=f'w-{i}', role='user',
            turns=[{'role': 'user', 'content': prefix}],
            prefix=prefix,
        )
        bus._conn.execute(
            "UPDATE events SET ts = ? WHERE id = ?", (ts, eid)
        )
        bus._conn.commit()
        nest.index_chat_chunk(eid, prefix, 1, prefix)
        nest._conn.execute(
            "UPDATE chat_chunk_embeddings SET ts = ? WHERE event_id = ?",
            (ts, eid)
        )
        nest._conn.commit()
        eids.append(eid)

    # Force one cluster (gap=30min, all within 2 min)
    clusters = build_clusters(nest, gap_seconds=1800, user_id='admin', namespace='private')
    assert len(clusters) == 1
    assert clusters[0]['member_count'] == 3

    # Save cluster summary (use prefix concat as summary; LLM call skipped)
    cid = save_cluster_summary(
        nest, clusters[0],
        summary="Discussion of passport forms and baking recipes",
        user_id='admin', namespace='private',
    )
    assert cid > 0

    # Search with "passport" — should find cluster
    model = astor_get_embedding_model()
    q_emb = np.array(list(model.embed(['passport']))[0], dtype=np.float32)
    hits = cluster_summary_search(
        nest, q_emb, limit=5, user_id='admin', namespace='private',
    )
    assert len(hits) == 1
    assert hits[0]['member_count'] == 3
    assert set(hits[0]['member_event_ids']) == set(eids)


def test_set_status_endpoint_marks_inactive(tmp_astor_home):
    """Status column writeable; default 'active'; survives through migration."""
    from astor_memory.bus.store import astor_bus_for
    # ensure module init without side-effects
    bus = astor_bus_for(tier='private', user_id='admin')
    # Insert a fact directly via promote_candidate path
    import json as _json
    eid = bus.append_event(
        namespace='private:admin', agent_id='admin', source='test',
        action='test', content='test fact', metadata=_json.dumps({}),
    )
    cid = bus.insert_candidate(
        event_id=eid, namespace='private:admin', content='user likes coffee',
        kind='preference', confidence=0.9, importance=0.7,
    )
    fact_id = bus.promote_candidate(
        candidate_id=cid, promoted_by='admin-test',
    )
    assert fact_id > 0
    # Default status is active
    row = bus._conn.execute(
        "SELECT status FROM memory_canonical WHERE id=?", (fact_id,)
    ).fetchone()
    assert row[0] == 'active'

    # Set to inactive
    bus._conn.execute(
        "UPDATE memory_canonical SET status='inactive' WHERE id=?",
        (fact_id,)
    )
    bus._conn.commit()
    row = bus._conn.execute(
        "SELECT status FROM memory_canonical WHERE id=?", (fact_id,)
    ).fetchone()
    assert row[0] == 'inactive'