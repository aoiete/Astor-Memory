"""
test_intent_classifier.py — v1.16.61 read-side intent classification tests.

Documents the priority of intent boundaries that the read-side intent
classifier uses for /v1/read. These numbers / patterns must not
drift accidentally.

Six buckets (priority order):
  1. method       — rule/pattern/architecture keywords
  2. preference   — "我偏好 X", "I like X", "my favorite X"
  3. personal     — "我 X" without preference word, "my X" without preference
  4. temporal     — date / "last N days" markers
  5. procedural   — "怎么 X" / "how to"
  6. factual      — fallback (default)

Usage: python tests/test_intent_classifier.py
"""


def classify_read_intent(query: str) -> str:
    """Re-implementation of v1.16.61 classify_read_intent for unit testing.

    Same logic as server.py — keep both in sync.
    """
    import re
    q = (query or '').strip().lower()
    if not q:
        return 'factual'
    if re.search(r'(方法|规则|模式|架构|workflow|rule|pattern|architecture|framework|原理|机制)', q):
        return 'method'
    # 我 + preference-word → preference (check before pure personal)
    if re.search(r'(?<![一鿿])我[^\s]*?(喜欢|讨厌|偏好|prefer|like|dislike|hate|favor|favorite)', q):
        return 'preference'
    # my + favorite-word → preference (English equivalent)
    if re.search(r'\bmy\s+(?:favorite|favour)', q):
        return 'preference'
    if re.search(r'(喜欢|讨厌|偏好|prefer\b|like\b|dislike|hate|favor|favorite)', q):
        return 'preference'
    # pure personal
    if re.search(r'(my\s+|i\s+(?:am|was|will|did|have)\b|mine\b|(?<![一鿿])我的(?![一鿿]))', q):
        return 'personal'
    if re.search(r'(?<![一鿿])我(?![一鿿])|自己', q):
        return 'personal'
    if re.search(r'(昨天|今天|明天|上个月|last\s+(?:week|month|day|year|time)|yesterday|today|ago)', q):
        return 'temporal'
    if re.search(r'(怎么|如何|how\s+to|how\s+(?:do|can|should)|step\s*by\s*step)', q):
        return 'procedural'
    return 'factual'


def _run(name, fn):
    try:
        fn()
        print(f'PASS  {name}')
        return 0
    except AssertionError as e:
        print(f'FAIL  {name}: {e}')
        return 1


# ---- intent-specific tests ----

def test_method_classification():
    assert classify_read_intent('方法: 怎么过滤 noise') == 'method'
    assert classify_read_intent('rules of poker') == 'method'
    assert classify_read_intent('workflow 是什么') == 'method'
    assert classify_read_intent('架构选型建议') == 'method'


def test_preference_classification_chinese():
    assert classify_read_intent('我偏好 dark mode') == 'preference'
    assert classify_read_intent('我喜欢 trading') == 'preference'
    assert classify_read_intent('我自己喜欢 dark mode') == 'preference'


def test_preference_classification_english():
    assert classify_read_intent('I like trading') == 'preference'
    assert classify_read_intent('I prefer dark mode') == 'preference'
    assert classify_read_intent('my favorite stock') == 'preference'


def test_personal_classification():
    # pure my/mine without preference word → personal
    assert classify_read_intent('my trade last week') == 'personal'
    assert classify_read_intent('I have a port issue') == 'personal'
    assert classify_read_intent('我昨天打 poker') == 'personal'
    assert classify_read_intent('mine not theirs') == 'personal'
    assert classify_read_intent('自己的选择') == 'personal'


def test_temporal_classification():
    assert classify_read_intent('last week maker_pocket 跑了多少') == 'temporal'
    assert classify_read_intent('yesterday trade') == 'temporal'


def test_procedural_classification():
    assert classify_read_intent('怎么重置密码') == 'procedural'
    assert classify_read_intent('how to install astor') == 'procedural'
    assert classify_read_intent('how should I configure opend') == 'procedural'
    assert classify_read_intent('怎么 deploy 这个 app') == 'procedural'


def test_factual_default():
    assert classify_read_intent('astor server 现在健康吗') == 'factual'
    assert classify_read_intent('version 是几') == 'factual'
    assert classify_read_intent('some random fact query') == 'factual'
    assert classify_read_intent('') == 'factual'


# ---- priority edge cases (key, hardest to ship) ----

def test_method_wins_over_procedural():
    # "方法" should match before "怎么" in "方法: 怎么过滤 noise"
    assert classify_read_intent('方法: 怎么过滤 noise') == 'method'


def test_preference_wins_over_personal():
    # "我偏好" should match before bare "我"
    assert classify_read_intent('我偏好 dark mode') == 'preference'


def test_preference_wins_over_personal_english():
    # "my favorite" should be preference (favorite is preference word)
    assert classify_read_intent('my favorite stock') == 'preference'


def test_temporal_wins_over_procedural():
    # "how" + "debo:" no — temporal wins before procedural check
    # 没 temporal 词所以还是 procedural
    assert classify_read_intent('how do I fix port') == 'procedural'


if __name__ == '__main__':
    import inspect
    import sys
    failed = 0
    passed = 0
    for name, fn in inspect.getmembers(sys.modules[__name__], inspect.isfunction):
        if name.startswith('test_'):
            r = _run(name, fn)
            if r == 0:
                passed += 1
            else:
                failed += r
    print(f'\\n{passed} passed, {failed} failed')
    sys.exit(0 if failed == 0 else 1)