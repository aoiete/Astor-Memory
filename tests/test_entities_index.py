"""v1.16.70 Ship D #1 test — entities.jsonl index builder."""
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


def test_build_entities_index_groups_by_type_value(tmp_astor_home):
    """Entities with same (type, value) collapse to one index entry."""
    from astor_memory.bus.store import astor_bus_for
    from astor_memory.nest.markdown_export import build_entities_index
    bus = astor_bus_for(tier='private', user_id='admin')
    # Promote 2 candidates, both with same entity
    eid1 = bus.append_event(
        namespace='private:admin', agent_id='admin', source='user-msg',
        action='test', content='a', metadata='', explicit_user=True,
    )
    cid1 = bus.insert_candidate(
        event_id=eid1, namespace='private:admin', content='user likes coffee',
        kind='preference', entities=[{'type': 'food', 'value': 'coffee'}],
    )
    fid1 = bus.promote_candidate(
        candidate_id=cid1, promoted_by='test',
        user_id='admin', tier='private',
    )
    eid2 = bus.append_event(
        namespace='private:admin', agent_id='admin', source='user-msg',
        action='test', content='b', metadata='', explicit_user=True,
    )
    cid2 = bus.insert_candidate(
        event_id=eid2, namespace='private:admin',
        content='user drinks espresso',
        kind='preference', entities=[{'type': 'food', 'value': 'coffee'}],
    )
    fid2 = bus.promote_candidate(
        candidate_id=cid2, promoted_by='test',
        user_id='admin', tier='private',
    )

    out_dir = tmp_astor_home / 'export_out'
    result = build_entities_index(
        bus, tier='private', user_id='admin', out_dir=out_dir,
    )
    assert result['entity_count'] == 1
    assert result['total_references'] == 2
    idx_path = out_dir / 'entities.jsonl'
    assert idx_path.exists()
    lines = idx_path.read_text(encoding='utf-8').strip().split('\n')
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec['type'] == 'food'
    assert rec['value'] == 'coffee'
    assert sorted(rec['fact_ids']) == sorted([fid1, fid2])
    assert rec['fact_count'] == 2


def test_build_entities_index_separates_types(tmp_astor_home):
    """Different types or values get separate index entries."""
    from astor_memory.bus.store import astor_bus_for
    from astor_memory.nest.markdown_export import build_entities_index
    bus = astor_bus_for(tier='private', user_id='admin')

    def _add_fact(content, ents):
        eid = bus.append_event(
            namespace='private:admin', agent_id='admin', source='user-msg',
            action='test', content=content[:10], metadata='',
            explicit_user=True,
        )
        cid = bus.insert_candidate(
            event_id=eid, namespace='private:admin',
            content=content, kind='fact', entities=ents,
        )
        bus.promote_candidate(
            candidate_id=cid, promoted_by='test',
            user_id='admin', tier='private',
        )

    _add_fact('user drinks coffee', [{'type': 'food', 'value': 'coffee'}])
    _add_fact('user drinks tea', [{'type': 'food', 'value': 'tea'}])
    _add_fact(
        'user lives in seoul',
        [{'type': 'location', 'value': 'seoul'}],
    )

    out_dir = tmp_astor_home / 'export_out2'
    result = build_entities_index(
        bus, tier='private', user_id='admin', out_dir=out_dir,
    )
    assert result['entity_count'] == 3
    assert result['total_references'] == 3