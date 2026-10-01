"""v1.16.33: conflict resolver regression test (4 cases)."""
import sys, os, json, tempfile
sys.path.insert(0, r'D:\AI\astor-memory')

# Use temp db for the resolver test
test_dir = tempfile.mkdtemp(prefix='astor_test_')
os.environ['ASTOR_DIR'] = test_dir

from astor_memory._internal.acl import astor_init_acl
from astor_memory.bus.store import astor_bus

astor_init_acl(actor='admin:admin', role='admin', tier='private', user_id='admin')

bus = astor_bus(tier='private', user_id='admin')


def make_fact(content, entities=None, kind='fact'):
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
        importance=0.5,
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
    print('Conflict resolver regression (v1.16.33, 4 cases)')
    print('=' * 70)

    passed = 0
    failed = []

    # Case 1: new user_preference with same entities -> invalidate old
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
    invalidated = bus.resolve_conflicts(new_id, 'user_preference', 'admin',
                                        json.dumps([{'type': 'topic', 'value': 'language'}]),
                                        'user prefers Rust')
    if len(invalidated) == 1 and invalidated[0]['invalidated_fact_id'] == old_id:
        passed += 1
        print('  PASS  case 1: user_preference invalidates old')
    else:
        failed.append('case 1')
        print(f'  FAIL  case 1: expected [{old_id}], got {invalidated}')

    # Case 2: same kind but no shared entities -> no invalidation
    new_id2 = make_fact(
        'user prefers Vue',
        entities=[{'type': 'topic', 'value': 'frontend'}],
        kind='user_preference',
    )
    invalidated2 = bus.resolve_conflicts(
        new_id2, 'user_preference', 'admin',
        json.dumps([{'type': 'topic', 'value': 'frontend'}]),
        'user prefers Vue',
    )
    if len(invalidated2) == 0:
        passed += 1
        print('  PASS  case 2: no shared entities -> no invalidation')
    else:
        failed.append('case 2')
        print(f'  FAIL  case 2: expected [], got {invalidated2}')

    # Case 3: kind=fact (no kind-driven trigger) -> no invalidation
    new_id3 = make_fact(
        'today is sunny',
        entities=[{'type': 'topic', 'value': 'weather'}],
        kind='fact',
    )
    invalidated3 = bus.resolve_conflicts(
        new_id3, 'fact', 'admin',
        json.dumps([{'type': 'topic', 'value': 'weather'}]),
        'today is sunny',
    )
    if len(invalidated3) == 0:
        passed += 1
        print('  PASS  case 3: kind=fact does not trigger')
    else:
        failed.append('case 3')
        print(f'  FAIL  case 3: expected [], got {invalidated3}')

    # Case 4: user_correction kind -> invalidate same-kind older
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
    invalidated4 = bus.resolve_conflicts(
        new_id4, 'user_correction', 'admin',
        json.dumps([{'type': 'topic', 'value': 'rate'}]),
        'rate is 5%',
    )
    if len(invalidated4) == 1 and invalidated4[0]['invalidated_fact_id'] == old_id4:
        passed += 1
        print('  PASS  case 4: user_correction invalidates old')
    else:
        failed.append('case 4')
        print(f'  FAIL  case 4: expected [{old_id4}], got {invalidated4}')

    print(f'\n{passed}/{passed+len(failed)} passed')
    if failed:
        sys.exit(1)
    print('GATE PASS: hit_rate=1.0')


if __name__ == '__main__':
    main()