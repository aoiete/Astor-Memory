"""
test_filter_hook.py — v1.16.60 pre-promote filter unit tests.

Standalone stdlib tests (no pytest required). Documents and verifies
the three noise gates applied in /v1/write before facts hit
memory_canonical: text < 5 chars, confidence < 0.3, importance < 0.3
AND kind == 'fact'. These numbers must not drift accidentally.

Usage:
    python tests/test_filter_hook.py
"""


def _gate(text, kind, confidence, importance):
    """Re-implementation of the v1.16.60 filter for direct unit testing.

    Returns None when the fact survives, otherwise the drop reason.
    """
    if len((text or '').strip()) < 5:
        return 'text_too_short'
    if float(confidence or 0.0) < 0.3:
        return 'low_confidence'
    if float(importance or 0.0) < 0.3 and kind == 'fact':
        return 'low_importance_fact'
    return None


def _run(name, fn):
    try:
        fn()
        print(f'PASS  {name}')
        return 0
    except AssertionError as e:
        print(f'FAIL  {name}: {e}')
        return 1


def test_survives_meaningful_fact():
    assert _gate('This is a meaningful fact', 'fact', 0.8, 0.7) is None


def test_survives_rule_low_importance():
    # rule at low importance still survives — rules have value even if not recalled often
    assert _gate('Always use HTTPS in production', 'rule', 0.9, 0.2) is None


def test_survives_lesson_low_importance():
    assert _gate('Lesson learned: cache invalidation is hard', 'lesson', 0.7, 0.1) is None


def test_survives_decision():
    assert _gate('Decision: switch to Postgres', 'decision', 0.95, 0.5) is None


def test_drop_text_too_short_hi():
    assert _gate('hi', 'fact', 0.8, 0.7) == 'text_too_short'


def test_drop_text_empty():
    assert _gate('', 'fact', 0.8, 0.7) == 'text_too_short'


def test_drop_text_whitespace_only():
    assert _gate('    ', 'fact', 0.8, 0.7) == 'text_too_short'


def test_drop_low_confidence():
    assert _gate('A confident statement', 'fact', 0.1, 0.7) == 'low_confidence'


def test_drop_zero_confidence():
    assert _gate('A confident statement', 'fact', 0.0, 0.7) == 'low_confidence'


def test_drop_low_importance_fact():
    # ONLY kind='fact' gets dropped on low importance
    assert _gate('A fact', 'fact', 0.8, 0.1) == 'low_importance_fact'


def test_drop_zero_importance_fact():
    assert _gate('A fact', 'fact', 0.8, 0.0) == 'low_importance_fact'


def test_text_length_boundary_at_5():
    # 5 chars exactly → survives (gate is < 5, not <= 5)
    assert _gate('abcde', 'fact', 0.8, 0.7) is None
    assert _gate('abcd', 'fact', 0.8, 0.7) == 'text_too_short'


def test_confidence_boundary_at_0_3():
    # Exactly 0.3 survives
    assert _gate('A real fact', 'fact', 0.3, 0.7) is None
    # 0.299 drops
    assert _gate('A real fact', 'fact', 0.299, 0.7) == 'low_confidence'


def test_importance_boundary_at_0_3():
    # rule/lesson/decision at 0.3 still survive (kind matters)
    assert _gate('a rule', 'rule', 0.8, 0.3) is None
    assert _gate('a lesson', 'lesson', 0.8, 0.3) is None
    # fact at 0.299 drops
    assert _gate('a fact', 'fact', 0.8, 0.299) == 'low_importance_fact'


def test_kind_exact_match_not_fact_string():
    # kind='fact' (exact) drops; kind='fact' (whitespace) or 'Fact' (case) — implementation
    # uses 'kind == "fact"' so only literal 'fact' drops. This is intentional: we
    # want the 'fact' baseline to be quiet while higher-quality kinds survive.
    assert _gate('longer text here', 'Fact', 0.8, 0.1) is None  # case-sensitive: not 'fact' → no drop


if __name__ == '__main__':
    import inspect
    import sys
    failed = 0
    passed = 0
    for name, fn in inspect.getmembers(sys.modules[__name__], inspect.isfunction):
        if name.startswith('test_'):
            r = _run(name, fn)
            failed += r
            if r == 0:
                passed += 1
    print(f'\n{passed} passed, {failed} failed')
    sys.exit(0 if failed == 0 else 1)
