"""v1.17.89: the recency boost must age a fact by when it was last
confirmed, not by when its embedding row was last rewritten.

Why this test exists
--------------------
The recency boost in AstorNest._topk aged every candidate by
embeddings.updated_at, which only moves when the fact is rewritten. A
rule that the user restates weekly but that is never rewritten looks
ancient: the paraphrase query "有一条铁律是不许把没验证过的事说成真的"
had its answer fact (fid 1533) at cosine 0.9205 -- the best match in the
whole corpus -- yet it ranked outside the top 10 because its recency had
decayed to 0.17, losing to a newer, less relevant fact at 0.90.

Switching the clock to memory_canonical.last_confirmed_at ("still in
force / still in use") moved the answer from rank 32 to rank 3 on the
hand-labelled paraphrase set, with the recency weight left at 0.2.

The four cases below pin that behaviour:
  1. a confirmed-but-old fact keeps a fresh age
  2. a fact that was never confirmed falls back to its updated_at age
  3. _topk ranks the confirmed fact above the merely-rewritten one
  4. a future confirmation timestamp does not overflow the decay
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.environ.get('ASTOR_TEST_SRC', os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from astor_memory._internal.acl import astor_init_acl
from astor_memory.bus.store import astor_bus

OLD_ISO = '2020-01-01T00:00:00Z'
FRESH_ISO = '2999-01-01T00:00:00Z'


@pytest.fixture
def bus(tmp_path, monkeypatch):
    """A private-tier bus + nest in a temp ASTOR_DIR.

    ASTOR_DIR is set through the fixture rather than at import time: the
    package resolves its paths from the environment when the handles are
    created, and a module-level os.environ write leaks into every test
    that imports after it.
    """
    monkeypatch.setenv('ASTOR_DIR', str(tmp_path / 'astor'))
    astor_init_acl(actor='admin:admin', role='admin', tier='private', user_id='admin')
    from astor_memory.nest.vector_store import astor_nest
    yield astor_bus(tier='private', user_id='admin'), astor_nest(tier='private', user_id='admin')


def _write_fact(bus, content, kind='fact'):
    event_id = bus.append_event(
        namespace='test', agent_id='test',
        source='test.recency', action='write', content=content,
    )
    cand = bus.insert_candidate(
        event_id=event_id, namespace='test',
        content=content, kind=kind, confidence=0.8, importance=0.9,
        tags=['test'], entities=[],
    )
    return bus.promote_candidate(
        cand, promoted_by='test',
        user_id='admin', tier='private', scope_type='long_term',
    )


def _set_confirmed(bus, fid, iso):
    bus.conn.execute(
        "UPDATE memory_canonical SET last_confirmed_at = ? WHERE id = ?", (iso, fid))


def _embed_row(nest, fid, dim=8):
    vec = np.ones(dim, dtype=np.float32)
    vec = vec / np.linalg.norm(vec)
    n = nest
    n.conn.execute(
        "INSERT OR REPLACE INTO embeddings "
        "(fact_id, embedding, model_name, dim, updated_at, user_id, tier) "
        "VALUES (?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ','now'), ?, ?)",
        (fid, vec.tobytes(), 'test-model', dim, n.user_id, n.tier),
    )
    return vec


def test_confirmed_fact_keeps_a_fresh_age(bus):
    _b, nest = bus
    fid = _write_fact(_b, '用户设铁律:不能报假信息')
    _set_confirmed(_b, fid, FRESH_ISO)
    age_map = nest._confirmed_age_map([fid])
    assert fid in age_map
    assert age_map[fid] < 1.0


def test_never_confirmed_fact_is_absent_from_the_confirmed_map(bus):
    _b, nest = bus
    fid = _write_fact(_b, '一条从没被确认过的事实')
    _b.conn.execute(
        "UPDATE memory_canonical SET last_confirmed_at = NULL WHERE id = ?", (fid,))
    age_map = nest._confirmed_age_map([fid])
    assert fid not in age_map


def test_confirmed_fact_outranks_the_merely_rewritten_one(bus):
    _b, n = bus
    f_conf = _write_fact(_b, '用户设铁律:不能报假信息')
    f_none = _write_fact(_b, '一条从没被确认过的事实')
    _set_confirmed(_b, f_conf, FRESH_ISO)
    vec = _embed_row(n, f_conf)
    _embed_row(n, f_none)
    # the confirmed fact's embedding row was written long ago
    n.conn.execute(
        "UPDATE embeddings SET updated_at = ? WHERE fact_id = ?", (OLD_ISO, f_conf))
    n.conn.execute(
        "UPDATE embeddings SET updated_at = ? WHERE fact_id = ?", (FRESH_ISO, f_none))
    n._search_cache = {}

    # both facts have the same cosine, so only the recency clock decides
    emb_matrix = np.vstack([vec, vec])
    fact_ids = np.array([f_conf, f_none], dtype=np.int64)
    e_norms = np.array([1.0, 1.0], dtype=np.float32)
    q = np.ones(8, dtype=np.float32)
    out = n._topk(q, emb_matrix, fact_ids, e_norms, limit=2)
    assert [fid for fid, _ in out][0] == f_conf


def test_future_confirmation_timestamp_does_not_overflow(bus):
    _b, n = bus
    f_future = _write_fact(_b, '一条确认时间在未来的事实')
    _set_confirmed(_b, f_future, FRESH_ISO)
    vec = _embed_row(n, f_future)
    q = np.ones(8, dtype=np.float32)
    # 0.5 ** negative raises OverflowError; a clock-drifted or imported
    # timestamp in the future used to take the whole read path down.
    out = n._topk(q, vec.reshape(1, -1), np.array([f_future], dtype=np.int64),
                  np.array([1.0], dtype=np.float32), limit=1)
    assert len(out) == 1

