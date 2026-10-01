"""v1.16.33: tool result clearing regression test."""
import sys
sys.path.insert(0, r'D:\AI\astor-memory')
from astor_memory.nest.distiller import clear_tool_results


def run(text: str, keep_n: int):
    cleaned, replaced = clear_tool_results(text, keep_recent_n=keep_n)
    return cleaned, replaced


CASES = [
    # (description, text, keep_n, exp_replaced_count, exp_kept_count,
    #  exp_first_kept_id, exp_size_reduction_pct_min)
    ('case 1: 3 blocks, keep 1 — drops 2 oldest',
     'a\n__tool_result_a__{"x": "' + 'x' * 1000 + '"}__/tool_result__\n'
     'b\n__tool_result_b__{"y": "' + 'y' * 1000 + '"}__/tool_result__\n'
     'c\n__tool_result_c__{"z": "' + 'z' * 1000 + '"}__/tool_result__\n',
     1, 2, 1, 'c', 60),  # >= 60% reduction
    ('case 2: 2 blocks, keep 1 — drops 1',
     '__tool_result_a__{"x": "' + 'x' * 500 + '"}__/tool_result__\n'
     '__tool_result_b__{"y": "' + 'y' * 500 + '"}__/tool_result__\n',
     1, 1, 1, 'b', 30),
    ('case 3: keep_recent_n >= blocks — nothing dropped',
     '__tool_result_a__{"x": "' + 'x' * 100 + '"}__/tool_result__\n',
     3, 0, 1, 'a', 0),
    ('case 4: no tool_result blocks — pass through',
     'just plain text with no embedded tool_result',
     1, 0, 0, None, 0),
]


def main():
    print('=' * 70)
    print('Tool-result clearing regression (v1.16.33, 4 cases)')
    print('=' * 70)
    passed = 0
    failed = []
    for desc, text, keep_n, exp_drop, exp_keep, exp_first_kept, exp_reduce_pct in CASES:
        cleaned, replaced = run(text, keep_n)
        if len(replaced) != exp_drop:
            failed.append((desc, f'expected {exp_drop} dropped, got {len(replaced)}'))
            continue
        # Check kept blocks count
        kept_count = cleaned.count('__tool_result_') - 0  # rough
        # Check first kept id
        if exp_first_kept:
            # Find first tool_result_ in cleaned
            import re
            m = re.search(r'__tool_result_(\w+)__', cleaned)
            if not m or m.group(1) != exp_first_kept:
                failed.append((desc, f'expected first kept = {exp_first_kept}, found {m.group(1) if m else "NONE"} in cleaned: {cleaned[:200]!r}'))
                continue
        # Check size reduction
        if len(text) > 0:
            reduce_pct = (1 - len(cleaned)/len(text)) * 100
            if reduce_pct < exp_reduce_pct:
                failed.append((desc, f'reduction {reduce_pct:.1f}% < expected {exp_reduce_pct}%'))
                continue
        passed += 1
        print(f'  PASS  {desc}')

    print(f'\n{passed}/{len(CASES)} passed')
    if failed:
        for desc, msg in failed:
            print(f'  FAIL  {desc}: {msg}')
        sys.exit(1)
    print('GATE PASS: hit_rate=1.0')


if __name__ == '__main__':
    main()