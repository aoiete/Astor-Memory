"""v1.16.29: visibility_classifier regression test (10 locked cases).

These tests are PERMANENT — do not edit existing cases. Append-only.
"""
import sys
sys.path.insert(0, r'D:\AI\astor-memory')
from astor_memory.nest.visibility_classifier import (
    classify_visibility, has_pii, has_first_person, has_emotion, has_geographic,
    AUTO_COMMONS_KINDS,
)

CASES = [
    # (description, text, kind, hint, admin_allow, expected_visibility, expected_has_pii)
    ('case 1: clean method auto-promotes',
     '微信抓取: curl + UA 伪装 method to fetch article',
     'method', None, True, 'commons', False),

    ('case 2: first-person emotion auto-personal',
     '我今天心情不好想自己静静',
     'personal', None, True, 'personal', False),

    ('case 3: observation default personal (no kind match)',
     '明天下雨可能要带伞',
     'observation', None, True, 'personal', False),

    ('case 4: kind=method + PII auto-blocked',
     'method to call 13800138000 for verification',
     'method', None, True, 'personal', True),

    ('case 5: first-person method blocked',
     '我发明了 X 方法来解决这个问题',
     'method', None, True, 'personal', False),

    ('case 6: admin global toggle forces personal',
     '微信抓取 method to share with everyone',
     'method', None, False, 'personal', False),

    ('case 7: hint=commons + PII BLOCKED',
     'my phone is 13800138000 please share this method',
     'method', 'commons', True, 'personal', True),

    ('case 8: hint=commons + clean content OVERRIDE',
     'curl + UA method to fetch wechat articles',
     'method', 'commons', True, 'commons', False),

    ('case 9: hint=personal always personal',
     'public method to share with everyone',
     'method', 'personal', True, 'personal', False),

    ('case 10: lesson kind auto-promotes',
     '微信文章反爬: 用 curl+UA 替代浏览器',
     'lesson', None, True, 'commons', False),
]


def run_case(desc, text, kind, hint, admin_allow, exp_vis, exp_has_pii):
    try:
        result = classify_visibility(
            text=text, kind=kind, hint=hint, admin_allow_commons=admin_allow,
        )
    except Exception as e:
        return False, f'EXCEPTION: {e}'
    actual_vis = result['visibility']
    actual_pii = bool(result['signals']['pii'])
    if actual_vis != exp_vis:
        return False, f'visibility: got {actual_vis!r}, want {exp_vis!r} (reason={result["reason"]})'
    if actual_pii != exp_has_pii:
        return False, f'has_pii: got {actual_pii}, want {exp_has_pii} (signals={result["signals"]})'
    return True, ''


def main():
    print('=' * 70)
    print('Visibility classifier regression (v1.16.29, 10 locked cases)')
    print('=' * 70)
    passed = 0
    failed = []
    for desc, text, kind, hint, admin_allow, exp_vis, exp_has_pii in CASES:
        ok, msg = run_case(desc, text, kind, hint, admin_allow, exp_vis, exp_has_pii)
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