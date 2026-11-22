"""The write gate and the tier classifier must agree on what a chat id is.

v1.17.84. Two scanners answered the same question -- "does this text
name a conversation?" -- with two separate regex sets:

  * memory_defense.scan_facts     -- the public WRITE gate
  * visibility_classifier.has_pii -- the tier classifier

When they drifted apart, the gate became the weaker one. It knew
"platform name, maybe chat_id/user_id, then digits" -- but not the
ROLE word, which is what the operator actually writes:

    telegram DM <chat_id> 只负责 crypto 信号
    DC admin DM 通道(<discord_id>) 只负责 crypto 信号

The classifier had been hardened against real text; the gate had not.
So a public-tier write carrying a chat id passed the gate as ordinary
prose, and three facts in the public tier on this host carried a real
Discord id that way.

The gate now borrows the classifier's patterns instead of restating
them, so the two cannot drift apart again -- which is precisely how
this hole opened. These tests pin that the two agree, on both the
shapes that must be caught and the neighbours that must not.

The ids are assembled at runtime: what the scanners have to notice is
the SHAPE, and a literal identifier in the source would be exactly the
thing this whole change is about.
"""

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from astor_memory.nest.memory_defense import scan_facts  # noqa: E402
from astor_memory.nest.visibility_classifier import has_pii  # noqa: E402

_CHAT_ID = "448" "000" "241"
_DISCORD_ID = "151" "448084" "479266" "412"
_WECHAT_ID = "o9cq" "80632"


def _gate_catches(text: str) -> bool:
    return any(m.severity in ('redact', 'block') for m in scan_facts(text))


def _classifier_catches(text: str) -> bool:
    return any(m['severity'] in ('redact', 'block') for m in has_pii(text))


# --- the shapes that must be caught by BOTH -----------------------------

MUST_CATCH = [
    # The shape that got through the gate: platform, role, digits.
    f"telegram DM {_CHAT_ID} 只负责 crypto 信号",
    f"discord DM {_DISCORD_ID} 只负责 crypto 信号",
    # The abbreviation forms.
    f"TG DM {_CHAT_ID} 只负责 crypto 信号",
    f"DC DM {_DISCORD_ID} 只负责 crypto 信号",
    f"DC 通道 {_DISCORD_ID} 只负责 crypto 信号",
    # admin between the abbreviation and the role, and the id in
    # parentheses -- both as the real facts were written.
    f"DC admin DM 通道({_DISCORD_ID})只负责 crypto 信号",
    # The full platform name with a chat_id keyword.
    f"telegram chat_id {_CHAT_ID} carries the signal",
    # The bare WeChat id, without the "@im.wechat" suffix.
    f"read the WEIXIN_ADMIN_ID (admin's {_WECHAT_ID}-...) from .env",
]


@pytest.mark.parametrize('text', MUST_CATCH)
def test_the_gate_catches_a_chat_id(text):
    assert _gate_catches(text), f"the write gate missed a chat id: {text[:60]}"


@pytest.mark.parametrize('text', MUST_CATCH)
def test_the_classifier_catches_the_same_text(text):
    assert _classifier_catches(text), f"the classifier missed a chat id: {text[:60]}"


# --- the neighbours that must NOT be caught -----------------------------

MUST_NOT_CATCH = [
    # A Hermes session id starts with a date and has more after it.
    "telegram session 20261009",
    "telegram DM 20261104_122221_61c9ac3a has RESUMED",
    "the discord DM 20261029_200344_95e192ab is idle",
    # Abbreviations that are ordinary words.
    "DC power supply model 5000W",
    "TG the joint probability distribution",
    "the DC group of companies reported",
    "TGIF revenue grew 12%",
    # A role word with no digits.
    "DC admin panel is broken",
    "tg admin dashboard needs a refresh",
    "DC channel 5000 words a day",
    # A filename, not an identifier.
    "wechat.html is a filename",
    # Ordinary words.
    "occurrences and occurrence are words",
]


@pytest.mark.parametrize('text', MUST_NOT_CATCH)
def test_the_gate_leaves_harmless_text_alone(text):
    assert not _gate_catches(text), f"the write gate flagged harmless text: {text[:60]}"


@pytest.mark.parametrize('text', MUST_NOT_CATCH)
def test_the_classifier_leaves_the_same_text_alone(text):
    assert not _classifier_catches(text), f"the classifier flagged harmless text: {text[:60]}"


# --- the two scanners must not drift apart -------------------------------

def test_the_gate_reuses_the_classifier_patterns():
    """One source of truth, not two copies of the same rule.

    If this test fails, somebody re-implemented the rule in
    memory_defense instead of borrowing it -- and that is exactly how
    the two drifted apart in the first place.
    """
    from astor_memory.nest import memory_defense as md

    assert md._VIS_CHAT_ID_SOURCE, 'the gate lost the classifier chat-id patterns'
    assert md._VIS_WECHAT_BARE_SOURCE, 'the gate lost the bare WeChat id pattern'
    # The gate's own extra rule (the hyphen form) stays.
    assert 'telegram_chat_id' in [n for n, _rx, _s in md._PATTERNS]


def test_the_two_scanners_agree_on_a_sample():
    """The gate must never be WEAKER than the classifier.

    The invariant is one-directional on purpose. The gate is the
    security boundary -- it may catch MORE than the classifier (it has
    extra rules the classifier has no reason to hold, like the
    hyphenated id form), but it must never catch LESS. The hole this
    change closes was exactly a case of "less": the classifier knew
    the role word, the gate did not, so a chat id walked past the
    gate while the classifier would have flagged it.
    """
    samples = MUST_CATCH + MUST_NOT_CATCH + [
        f"telegram:dm:{_CHAT_ID} carries last_prompt_tokens",
        f"deliver=origin->telegram {_CHAT_ID}",
        f"-{_CHAT_ID}",
        "chat id 55500011",
        f"wechat {_WECHAT_ID}",
    ]
    weaker = []
    for text in samples:
        gate = _gate_catches(text)
        cls = _classifier_catches(text)
        if cls and not gate:
            weaker.append((text[:50], cls, gate))
    assert not weaker, (
        'the write gate is weaker than the classifier on: '
        + '; '.join(f'{t} (classifier={c}, gate={g})' for t, c, g in weaker)
    )


def test_the_gate_catches_everything_the_classifier_catches():
    """The same invariant, stated as a direct comparison."""
    samples = [
        f"telegram DM {_CHAT_ID}",
        f"discord DM {_DISCORD_ID}",
        f"TG DM {_CHAT_ID}",
        f"DC 通道 {_DISCORD_ID}",
        f"telegram chat_id {_CHAT_ID}",
        f"telegram:dm:{_CHAT_ID}",
        f"admin's {_WECHAT_ID}-...",
        f"deliver=origin->telegram {_CHAT_ID}",
    ]
    for text in samples:
        assert _classifier_catches(text), f"the classifier missed: {text[:50]}"
        assert _gate_catches(text), f"the gate missed what the classifier caught: {text[:50]}"
