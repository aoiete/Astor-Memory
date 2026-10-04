"""v1.16.33 + v1.16.34: conflict resolver regression test (7 cases)."""
import sys, os, json
import os
sys.path.insert(0, os.environ.get('ASTOR_TEST_SRC', os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Use temp db for the resolver test
import tempfile
test_dir = tempfile.mkdtemp(prefix='astor_test_')
os.environ['ASTOR_DIR'] = test_dir

from astor_memory._internal.acl import astor_init_acl
from astor_memory.bus.store import astor_bus

astor_init_acl(actor='admin:admin', role='admin', tier='private', user_id='admin')

bus = astor_bus(tier='private', user_id='admin')


def make_fact(content, entities=None, kind='fact', importance=0.5):
    """Helper to insert + promote a fact."""
    event_id = bus.append_event(
        namespace='test',
        agent_id='test',
        source='test.conflict',
        action='write',
        content=content,
    )
    cand = bus.insert_candidate(
        event_id=event_id,
        namespace='test',
        content=content,
        kind=kind,
        confidence=0.8,
        importance=importance,
        tags=['test'],
        entities=entities or [],
    )
    fid = bus.promote_candidate(
        cand, promoted_by='test',
        user_id='admin', tier='private', scope_type='long_term',
    )
    return fid


def main():
    print('=' * 70)
    print('Conflict resolver regression (v1.16.34, 7 cases)')
    print('=' * 70)

    passed = 0
    failed = []

    # Case 1: new user_preference with same entities -> gate old
    old_id = make_fact(
        'user prefers Python',
        entities=[{'type': 'topic', 'value': 'language'}],
        kind='user_preference',
    )
    new_id = make_fact(
        'user prefers Rust',
        entities=[{'type': 'topic', 'value': 'language'}],
        kind='user_preference',
    )
    r = bus.resolve_conflicts(new_id, 'user_preference', 'admin',
                              json.dumps([{'type': 'topic', 'value': 'language'}]),
                              'user prefers Rust')
    if (len(r) == 1
        and r[0]['fact_id'] == old_id
        and r[0]['mode'] == 'gated'
        and r[0]['new_importance'] < r[0]['old_importance']):
        passed += 1
        print('  PASS  case 1: user_preference gates old (default soft_decay)')
    else:
        failed.append('case 1')
        print(f'  FAIL  case 1: expected gated mode, got {r}')

    # Case 2: same kind but no shared entities -> no gating
    new_id2 = make_fact(
        'user prefers Vue',
        entities=[{'type': 'topic', 'value': 'frontend'}],
        kind='user_preference',
    )
    r2 = bus.resolve_conflicts(
        new_id2, 'user_preference', 'admin',
        json.dumps([{'type': 'topic', 'value': 'frontend'}]),
        'user prefers Vue',
    )
    if len(r2) == 0:
        passed += 1
        print('  PASS  case 2: no shared entities -> no gating')
    else:
        failed.append('case 2')
        print(f'  FAIL  case 2: expected [], got {r2}')

    # Case 3: kind=fact (no kind-driven trigger) -> no gating
    new_id3 = make_fact(
        'today is sunny',
        entities=[{'type': 'topic', 'value': 'weather'}],
        kind='fact',
    )
    r3 = bus.resolve_conflicts(
        new_id3, 'fact', 'admin',
        json.dumps([{'type': 'topic', 'value': 'weather'}]),
        'today is sunny',
    )
    if len(r3) == 0:
        passed += 1
        print('  PASS  case 3: kind=fact does not trigger')
    else:
        failed.append('case 3')
        print(f'  FAIL  case 3: expected [], got {r3}')

    # Case 4: user_correction kind -> default soft gate (v1.16.34)
    old_id4 = make_fact(
        'rate is 3%',
        entities=[{'type': 'topic', 'value': 'rate'}],
        kind='user_correction',
    )
    new_id4 = make_fact(
        'rate is 5%',
        entities=[{'type': 'topic', 'value': 'rate'}],
        kind='user_correction',
    )
    r4 = bus.resolve_conflicts(
        new_id4, 'user_correction', 'admin',
        json.dumps([{'type': 'topic', 'value': 'rate'}]),
        'rate is 5%',
    )
    # default soft_decay=True -> gated, not tombstoned
    _check4 = bus.conn.execute(
        "SELECT importance, tombstoned, valid_until FROM memory_canonical WHERE id = ?",
        (old_id4,),
    ).fetchone()
    if (len(r4) == 1
        and r4[0]['mode'] == 'gated'
        and _check4[1] == 0  # not tombstoned
        and _check4[2] is None):  # not invalidated
        passed += 1
        print('  PASS  case 4: user_correction soft-gates old (RPMem-style)')
    else:
        failed.append('case 4')
        print(f'  FAIL  case 4: r4={r4} check={_check4}')

    # Case 5: custom decay_factor=0.25 -> importance drops to 0.25x
    _old5 = make_fact(
        'preference: Python6',
        entities=[{'type': 'topic', 'value': 'lang6'}],
        kind='user_preference',
    )
    new5 = make_fact(
        'preference: Rust6',
        entities=[{'type': 'topic', 'value': 'lang6'}],
        kind='user_preference',
    )
    bus.resolve_conflicts(
        new5, 'user_preference', 'admin',
        json.dumps([{'type': 'topic', 'value': 'lang6'}]),
        'preference: Rust6',
        soft_decay=True, decay_factor=0.25,
    )
    _check5 = bus.conn.execute(
        "SELECT importance FROM memory_canonical WHERE id = ?", (_old5,),
    ).fetchone()
    if _check5 is not None and float(_check5[0]) < 0.2:
        passed += 1
        print('  PASS  case 5: decay_factor=0.25 reduces importance to 0.25x')
    else:
        failed.append('case 5')
        print(f'  FAIL  case 5: imp={_check5[0] if _check5 else None}')

    # Case 6: soft_decay=False -> legacy hard-invalidate
    _old6 = make_fact(
        'preference: Python7',
        entities=[{'type': 'topic', 'value': 'lang7'}],
        kind='user_preference',
    )
    new6 = make_fact(
        'preference: Rust7',
        entities=[{'type': 'topic', 'value': 'lang7'}],
        kind='user_preference',
    )
    r6 = bus.resolve_conflicts(
        new6, 'user_preference', 'admin',
        json.dumps([{'type': 'topic', 'value': 'lang7'}]),
        'preference: Rust7',
        soft_decay=False,
    )
    _check6 = bus.conn.execute(
        "SELECT importance, tombstoned, valid_until FROM memory_canonical WHERE id = ?",
        (_old6,),
    ).fetchone()
    if (len(r6) == 1
        and r6[0]['mode'] == 'invalidated'
        and _check6[1] == 0
        and _check6[2] is not None):
        passed += 1
        print('  PASS  case 6: soft_decay=False preserves legacy hard-invalidate')
    else:
        failed.append('case 6')
        print(f'  FAIL  case 6: r6={r6} check={_check6}')

    # Case 7: metadata.gated_by + gate_at stored in soft_decay mode
    _old7 = make_fact(
        'preference: Python8',
        entities=[{'type': 'topic', 'value': 'lang8'}],
        kind='user_preference',
    )
    new7 = make_fact(
        'preference: Rust8',
        entities=[{'type': 'topic', 'value': 'lang8'}],
        kind='user_preference',
    )
    bus.resolve_conflicts(
        new7, 'user_preference', 'admin',
        json.dumps([{'type': 'topic', 'value': 'lang8'}]),
        'preference: Rust8',
    )
    _check7 = bus.conn.execute(
        "SELECT metadata FROM memory_canonical WHERE id = ?", (_old7,),
    ).fetchone()
    _meta7 = _check7[0] if _check7 and _check7[0] else '{}'
    try:
        _meta_dict = json.loads(_meta7) if _meta7 else {}
    except Exception:
        _meta_dict = {}
    if (isinstance(_meta_dict, dict)
        and 'gated_by' in _meta_dict
        and _meta_dict.get('gated_by') == new7
        and 'gate_at' in _meta_dict):
        passed += 1
        print('  PASS  case 7: gate metadata stored (gated_by + gate_at)')
    else:
        failed.append('case 7')
        print(f'  FAIL  case 7: meta={_meta_dict}')

    print(f'\n{passed}/{passed+len(failed)} passed')
    if failed:
        sys.exit(1)
    print('GATE PASS: hit_rate=1.0')


if __name__ == '__main__':
    main()