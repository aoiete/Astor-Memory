"""v1.16.30 + v1.16.31 + v1.16.33: distiller regression test (12 locked cases)."""
import sys
import os
sys.path.insert(0, os.environ.get('ASTOR_TEST_SRC', os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from astor_memory.nest.distiller import distill

CASES = [
    # (description, input, exp_contains, exp_NOT_contains,
    #  expected_dropped_count_min, expected_method_keywords_present)
    ('case 1: clean method content — no drops',
     '微信抓取 method curl+UA 抓 mp.weixin.qq.com/s/abc 文章, 验证后先 query 再 hot-link',
     ['method'],
     ['mp.weixin.qq.com'],
     0,
     True),

    ('case 2: PII inline-redacted within sentence',
     '今天用 2026-01-15 测试, 微信抓取 method curl+UA 抓文章',
     ['method'],
     ['2026-01-15'],
     0,
     True),

    ('case 3: first-person sentences dropped',
     '我今天心情不好, 微信抓取 method curl+UA 抓文章先 query 再 hot-link',
     ['method', 'query', 'hot-link'],
     ['我今天'],
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
     [],
     ['我', '抑郁', '自杀'],
     3,
     False),

    # v1.16.31: 3 new tests covering <user_b> scenario (PII + first-person + method)
    ('case 6: <user_b> PII + first-person + method (inline-redact PII, drop first-person)',
     '我今天用 2026-01-15 测试, 微信抓取 method curl+UA 抓文章, 验证后先 query 再 hot-link 比较稳',
     ['method', 'curl', 'query', 'hot-link'],
     ['2026-01-15', '我'],
     1,
     True),
    ('case 7: emotion + first-person sentences dropped, method kept',
     '我想哭, 我很焦虑, 今天用 method curl+UA 抓文章',
     ['method', 'curl'],
     ['我想哭', '焦虑'],
     2,
     True),
    ('case 8: <user_b> snippet (period separator), first-person dropped, method kept',
     '我今天做的。method curl+UA 抓文章',
     ['method', 'curl'],
     ['我今天做的'],
     1,
     True),

    # v1.16.33 state-const protection (article 红线一)
    # Note: tokens in [STATE-CONST] block use full-width dot '．' (instead of '.')
    # so the second-pass sentence split doesn't fragment them. Caller can
    # normalize back to ASCII '.' via .replace('．', '.') on read.
    ('case 9: file paths preserved in [STATE-CONST] block',
         'method curl+UA 抓文章, file: src/api/v2/handler.py:142 验证通过',
         ['method', 'curl', '[STATE-CONST]', 'src/api/v2/handler．py:142'],
         [],
         0,
         True),
    ('case 10: version number preserved',
     '用 method curl, v2.1.3 修复 ok',
     ['method', 'curl', 'v2.1.3'],
     [],
     0,
     True),
    ('case 11: function call preserved',
     'method: astor_recall() returns top_k facts',
     ['method', 'astor_recall()'],
     [],
     0,
     True),
    ('case 12: state-const preserved even when sentence is dropped',
     '我今天做了 abc.py:10, 但 method 是 curl+UA',
     ['method', 'curl', 'abc．py:10'],
     [],
     1,  # first-person "我今天做了" dropped, but state preserved
     True),
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
    print('Distiller regression (v1.16.33, 12 locked cases)')
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