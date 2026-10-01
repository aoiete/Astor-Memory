"""v1.16.30: distiller regression test (5 locked cases)."""
import sys
sys.path.insert(0, r'D:\AI\astor-memory')
from astor_memory.nest.distiller import distill

CASES = [
    # (description, input, expected_contains, expected_NOT_contains,
    #  expected_dropped_count_min, expected_method_keywords_present)
    ('case 1: clean method content — no drops',
     '微信抓取 method curl+UA 抓 mp.weixin.qq.com/s/abc 文章, 验证后先 query 再 hot-link',
     ['method'],      # has 'method' keyword
     ['mp.weixin', '13800'],  # URL replaced
     0,              # no drops (no PII / fp / emotion)
     True),          # method keywords present

    ('case 2: PII inline-redacted within sentence',
     '今天用 13800138000 测试, 微信抓取 method curl+UA 抓文章',
     ['method'],
     ['13800138000'],
     0,              # PII inline-redact — sentence not removed
     True),

    ('case 3: first-person sentences dropped',
     '我今天心情不好, 微信抓取 method curl+UA 抓文章先 query 再 hot-link',
     ['method', 'query', 'hot-link'],
     ['我今天'],        # "我" sentence dropped
     1,
     True),

    ('case 4: emotion-bearing sentences dropped',
     '我发现抑郁倾向, 但 method curl+UA 抓文章很好用',
     ['method', 'curl'],
     ['抑郁'],
     1,
     True),

    ('case 5: pure personal content — distilled to empty',
     '我今天心情不好, 我累了, 我想自杀',
     [],              # nothing survives
     ['我', '抑郁', '自杀'],
     3,              # all 3 sentences dropped
     False),         # no method keywords remain -> empty result
]


def run_case(desc, text, exp_contains, exp_not, exp_dropped_min, exp_method):
    cleaned, removed, report = distill(text)
    for needle in exp_contains:
        if needle.lower() not in cleaned.lower():
            return False, f'expected "{needle}" in cleaned: {cleaned!r}'
    for needle in exp_not:
        if needle in cleaned:
            return False, f'unexpected "{needle}" in cleaned: {cleaned!r}'
    if len(removed) < exp_dropped_min:
        return False, f'expected >= {exp_dropped_min} drops, got {len(removed)}: {removed}'
    if report['method_keywords_present'] != exp_method:
        return False, f'method_keywords_present expected = {exp_method}, got {report}'
    return True, ''


def main():
    print('=' * 70)
    print('Distiller regression (v1.16.30, 5 locked cases)')
    print('=' * 70)
    passed = 0
    failed = []
    for desc, text, exp_contains, exp_not, exp_drop_min, exp_method in CASES:
        ok, msg = run_case(desc, text, exp_contains, exp_not, exp_drop_min, exp_method)
        if ok:
            passed += 1
            print(f'  PASS  {desc}')
        else:
            failed.append(desc)
            print(f'  FAIL  {desc}: {msg}')
    print()
    print(f'{passed}/{len(CASES)} passed')
    if failed:
        print('FAILED cases:')
        for f in failed:
            print(f'  - {f}')
        sys.exit(1)
    print('GATE PASS: hit_rate=1.0')


if __name__ == '__main__':
    main()