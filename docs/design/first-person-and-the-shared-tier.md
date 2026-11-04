# First-person pronouns and the shared tier

Status: measured, not changed. This document records what the shared
tier's visibility classifier does with first-person pronouns, because the
answer is not what the tier is for.

## What happens

`astor_memory/nest/visibility_classifier.py` decides whether a fact is
`commons` (readable by peers) or `personal` (user only). One of its
signals is a first-person match:

```python
_FPII_RE = re.compile(
    r'(?:'
    r'我(?!们|自己)|我的|我们|我自己|'
    r'\bmy\b|\bour\b|\bmine\b|\bus\b|\bme\b|'
    r'家里|family|home|home address'
    r')'
)
```

Any match makes the fact `personal`, and `server.py` (v1.17.15) then
routes it out of the requested tier into the caller's private bucket.
A caller passing `visibility_hint='commons'` does not get past it: the
result is `hint_commons_blocked_by_signals`.

Measured on a method/lesson note with no private data in it:

| text | result | reason |
|---|---|---|
| `... 我们日常就在制造大量带期限的前瞻内容 ...` | `personal` | `default_personal`, `first_person: true` |
| the same note with the pronoun removed | `commons` | `kind_lesson_auto_promote` |
| `our approach to the scanner, verified on the holdout.` | `personal` | `first_person: true` |
| `US market` | `commons` | — |
| `用 US.SPY 跟 US.TQQQ 对比点差。` | `personal` | `pii_keyword_context` (severity `review`) |

So the pronoun is the difference between a note being shared and not.
`our` and `us` are in ordinary English technical writing; `我们` is in
ordinary Chinese technical writing. The plural first person names a
group, not a person.

## Why this is worth a decision

The shared tier exists for methods, flows, models and patterns — content
with no private data in it. A note that says "we tried this and it
worked" is a method note. Today it is filed as personal, so the shared
tier loses exactly the content the tier was built for, and the only
symptom is that the tier stays thin.

The opposite failure is the expensive one: a real name, a real
location or a real credential reaching a tier every peer can read. The
classifier's job is to make that impossible, and the other signals cover
it:

* Layer 1 PII (phone, email, ID card, bank card, SSN, chat id) blocks
  the write outright.
* `_GEO_RE` catches `我/我的/my/I` followed by a place.
* `_EMOTION_RE` catches health and crisis keywords.
* `我的`, `my`, `mine`, `me`, `家里`, `family`, `home` all still fire.

Verified with a hint of `commons` and content that is both personal and
shared-intent:

```
我们住在北京, 密码是 +86 138 0000 0000  -> personal  pii=True geo=True
our phone is +86 138 0000 0000          -> personal  pii=True
```

Both are still caught, by the signals that carry actual privacy weight.

## The options

**A. Leave it.** Correct and safe, but the shared tier keeps losing
method notes. The cost is invisible: nothing breaks, the tier just
stays small.

**B. Drop the plural first person from the signal.** Remove `我们`,
`\bour\b` and `\bus\b`, keeping the singular forms. This is the change
that matches the tier's stated purpose. It moves content in the
private-to-public direction, which is the direction that must not be
taken lightly, so it belongs to whoever owns that boundary.

Effect on the locked regression suite: none of the ten locked cases in
`tests/test_visibility_classifier.py` uses a plural pronoun, so the
suite still passes unchanged. New cases would be appended, per that
file's append-only rule.

**C. Require the plural pronoun to be near a personal marker.** E.g.
`我们` only counts when the sentence also carries `我的`, `家里`, a
place or a feeling. More precise than B, and more complicated.

**D. Keep the signal and let the caller declare the tier.** The
visibility classifier already has a `visibility_hint` input, but
`hint_commons_blocked_by_signals` means it cannot currently override a
first-person match. Making the hint able to clear *this* signal would
give the caller a channel for exactly the case above — a method note
that happens to be written in the first person plural — while leaving
the personal signals untouched.

## What was not changed

Nothing. The classifier, the PII layer and the routing were all left
exactly as they are.

Two reasons:

1. This is the boundary between the shared tier and the user's own
   data, and a change that moves content toward `commons` is a change
   in the direction that has to be owned deliberately, not as a side
   effect of reading an article.
2. The `pii_keyword_context` hit on `US.SPY` is a separate false
   positive in a signal that carries real privacy weight. Touching that
   one is a different decision with a different risk profile, and it is
   not covered by this document.

## Related

`docs/design/intentions.md` covers the other idea from the same source:
a forward-looking intention with an expiry date.
