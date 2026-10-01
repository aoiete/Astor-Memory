"""v1.16.34: recall-aware decay regression test (3 cases)."""
import sys, os, json
sys.path.insert(0, r'D:\AI\astor-memory')

test_dir = __import__('tempfile').mkdtemp(prefix='astor_test_')
os.environ['ASTOR_DIR'] = test_dir

from astor_memory._internal.acl import astor_init_acl
from astor_memory.bus.store import astor_bus

astor_init_acl(actor='admin:admin', role='admin', tier='private', user_id='admin')

bus = astor_bus(tier='private', user_id='admin')


def make_fact(content, entities=None, kind='fact', importance=0.5):
    event_id = bus.append_event(
        namespace='test', agent_id='test',
        source='test.decay', action='write', content=content,
    )
    cand = bus.insert_candidate(
        event_id=event_id, namespace='test',
        content=content, kind=kind, confidence=0.8, importance=importance,
        tags=['test'], entities=entities or [],
    )
    fid = bus.promote_candidate(
        cand, promoted_by='test',
        user_id='admin', tier='private', scope_type='long_term',
    )
    return fid


def get_imp(fact_id):
    row = bus.conn.execute(
        "SELECT importance FROM memory_canonical WHERE id = ?",
        (fact_id,),
    ).fetchone()
    return float(row[0]) if row else None


def main():
    print('=' * 70)
    print('Recall-aware decay regression (v1.16.34, 3 cases)')
    print('=' * 70)

    passed = 0
    failed = []

    # Case 1: HIT fact importance boosted by 1.05x
    f1 = make_fact('hello world')
    imp_before = get_imp(f1)
    r = bus.apply_recall_aware_decay(
        hit_fact_ids=[f1],
        decay_factor_unhit=0.95, boost_factor_hit=1.05,
        days_unhit_threshold=90,
    )
    imp_after = get_imp(f1)
    expected = min(1.0, round(imp_before * 1.05, 4))
    if abs(imp_after - expected) < 0.001 and r['boosted_count'] == 1:
        passed += 1
        print(f'  PASS  case 1: HIT fact boosted {imp_before} -> {imp_after}')
    else:
        failed.append('case 1')
        print(f'  FAIL  case 1: imp_before={imp_before}, after={imp_after}, r={r}')

    # Case 2: UNHIT fact with stale last_confirmed_at -> decayed by 0.95x
    f2 = make_fact('stale fact test')
    # Backdate last_confirmed_at to >90 days ago
    bus.conn.execute(
        "UPDATE memory_canonical SET last_confirmed_at = '2020-01-01T00:00:00Z' WHERE id = ?",
        (f2,),
    )
    imp_before = get_imp(f2)
    r = bus.apply_recall_aware_decay(
        hit_fact_ids=[],
        decay_factor_unhit=0.95, boost_factor_hit=1.05,
        days_unhit_threshold=90,
    )
    imp_after = get_imp(f2)
    expected = round(imp_before * 0.95, 4)
    if abs(imp_after - expected) < 0.001 and r['decayed_count'] >= 1:
        passed += 1
        print(f'  PASS  case 2: stale UNHIT decayed {imp_before} -> {imp_after}')
    else:
        failed.append('case 2')
        print(f'  FAIL  case 2: imp_before={imp_before}, after={imp_after}, r={r}')

    # Case 3: Mix — some HIT, some stale UNHIT. Fresh facts to avoid
    # case 1/2 residue.
    f_hit = make_fact('mix3-hit fact')
    f_unhit = make_fact('mix3-unhit fact')
    bus.conn.execute(
        "UPDATE memory_canonical SET last_confirmed_at = '2020-01-01T00:00:00Z' WHERE id = ?",
        (f_unhit,),
    )
    # Reset HIT fact to baseline so we test 'boosted from 0.5'
    bus.conn.execute(
        "UPDATE memory_canonical SET importance = 0.5, last_confirmed_at = NULL WHERE id = ?",
        (f_hit,),
    )
    # Reset UNHIT fact to baseline 0.5
    bus.conn.execute(
        "UPDATE memory_canonical SET importance = 0.5 WHERE id = ?",
        (f_unhit,),
    )
    r = bus.apply_recall_aware_decay(
        hit_fact_ids=[f_hit],
        decay_factor_unhit=0.95, boost_factor_hit=1.05,
        days_unhit_threshold=90,
    )
    imp_hit_after = get_imp(f_hit)
    imp_unhit_after = get_imp(f_unhit)
    if (imp_hit_after > 0.5  # boosted from baseline
        and imp_unhit_after < 0.5  # decayed from baseline
        and r['boosted_count'] >= 1
        and r['decayed_count'] >= 1):
        passed += 1
        print(f'  PASS  case 3: HIT boosted 0.5->{imp_hit_after}, '
              f'UNHIT decayed 0.5->{imp_unhit_after}')
    else:
        failed.append('case 3')
        print(f'  FAIL  case 3: hit {imp_hit_after}, unhit {imp_unhit_after}, r={r}')

    print(f'\n{passed}/{passed+len(failed)} passed')
    if failed:
        sys.exit(1)
    print('GATE PASS: hit_rate=1.0')


if __name__ == '__main__':
    main()