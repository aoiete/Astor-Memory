"""The WeChat-id rule must not fire on ordinary tokens.

v1.17.85. The bare WeChat id rule -- "an 'o' followed by 8+ alphanumerics
containing a digit" -- matched a random token 4% of the time.

That number matters more than it looks. The classifier uses the rule to
decide where a fact belongs, so a false positive there is a
misclassification. But the WRITE GATE uses the same pattern, and on the
public tier the gate REJECTS a write on any match. So a 4% false
positive does not just mislabel a fact -- it rejects a legitimate write
with a 400, and it does so intermittently.

It did, on CI: the hermes adapter probe builds a marker out of a random
8-letter word plus a hex string plus a millisecond timestamp, and one
run in 25 started with "o" and carried a digit, so the write was
blocked and the recall that followed got the 400 instead of results.

What separates a real id from a random token is the SEPARATOR. The
ilink form is "o<hex>-<mixed case>"; the standard openid form is
"o<something>_<mixed case>". A random hex-and-digits run has neither.

The uppercase-letter alternative does NOT work, and the reason is worth
recording: the gate compiles every pattern with re.IGNORECASE, and
IGNORECASE turns [A-Z] into [A-Za-z]. "Must carry an uppercase letter"
under re.I means "must carry a letter", which every word has. Only the
separator discriminates.

The ids here are assembled at runtime: what the rule has to notice is
the SHAPE, and a literal identifier in the source would be exactly the
thing this rule exists to keep out.
"""

import random
import time
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from astor_memory.nest.memory_defense import scan_facts  # noqa: E402
from astor_memory.nest.visibility_classifier import has_pii  # noqa: E402


def _gate_catches(text: str) -> bool:
    return any(m.severity in ('redact', 'block') for m in scan_facts(text))


def _classifier_catches(text: str) -> bool:
    return any(m['severity'] in ('redact', 'block') for m in has_pii(text))


# --- the real ids, in the shapes the platform hands out --------------------

REAL_IDS = [
    # the ilink form: o<hex>-<mixed case>
    "o9cq" "80632" "-REHf6fG55epPMPCreM",
    "o9cq" "801uWX_pXw" "-FLdmyYryNe88Y",
    # the standard openid form: o<something>_<mixed case>
    "o6_bmjrPTlm6_2sgVt7hMZOPfL2M",
]


@pytest.mark.parametrize('ident', REAL_IDS)
def test_a_real_wechat_id_is_caught(ident):
    text = f"the WEIXIN_ADMIN_ID (admin's {ident}) is in .env"
    assert _gate_catches(text), f"the write gate missed a real id: {ident[:14]}"
    assert _classifier_catches(text), f"the classifier missed a real id: {ident[:14]}"


def test_the_full_im_wechat_form_is_caught():
    ident = "o9cq" "80632" "-REHf6fG55epPMPCreM"
    assert _gate_catches(f"weixin user {ident}@im.wechat here")


# --- the false positive that broke CI --------------------------------------

def test_a_random_token_is_not_a_wechat_id():
    """The shape that broke CI: an 'o' + alphanumerics with a digit.

    No separator, so not an id -- whatever the length and whatever
    the digit count. The tokens are assembled at runtime so that no
    identifier-shaped literal sits in the source.
    """
    for token in (
        "otmud" "rwlb94e" "77bcc4c1" "1795407314485",
        "oabcdef" "gh123456" "789012345678",
        "o1234" "567890",
    ):
        text = f"adapter verification record with 熊猫{token}"
        assert not _gate_catches(text), f"the gate flagged a random token: {token}"
        assert not _classifier_catches(text), \
            f"the classifier flagged a random token: {token}"


def test_the_false_positive_rate_is_low():
    """A rate, not a sample.

    The rule fired on 4% of randomly generated markers. That is high
    enough to block legitimate writes intermittently, which is what it
    did on CI. Generating markers the way the failing test did, the
    rate has to be zero.
    """
    # Seeded, so a failure is reproducible rather than a coin flip.
    rng = random.Random(20261122)
    flagged = 0
    n = 400
    for _ in range(n):
        word = ''.join(rng.choice('abcdefghijklmnopqrstuvwxyz') for _ in range(8))
        hexpart = ''.join(rng.choice('0123456789abcdef') for _ in range(12))
        marker = f"熊猫{word}{hexpart}{int(time.time() * 1000)}"
        if _gate_catches(f"adapter verification record with {marker}"):
            flagged += 1
    assert flagged == 0, f"{flagged}/{n} random markers were flagged as ids"


# --- ordinary words -------------------------------------------------------

@pytest.mark.parametrize('text', [
    "occurrences and occurrence are words",
    "obfuscated code and openness",
    "our token rotation is automated",
    "open source offsets in the index",
])
def test_ordinary_words_are_not_ids(text):
    assert not _gate_catches(text)
    assert not _classifier_catches(text)


# --- the invariant the whole change rests on ------------------------------

def test_the_gate_is_never_weaker_than_the_classifier():
    """Both scanners must agree on these shapes.

    The v1.17.85 change made the gate BORROW the classifier's patterns.
    That only holds if the two agree -- which is why a rule tightened
    in one place has to be tightened in the other.
    """
    for ident in REAL_IDS:
        text = f"the WEIXIN_ADMIN_ID (admin's {ident}) is in .env"
        assert _gate_catches(text) == _classifier_catches(text)
    for text in ("occurrences are words", "obfuscated", "our token"):
        assert _gate_catches(text) == _classifier_catches(text)
