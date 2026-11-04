# Options C and D in detail, measured

Follow-up to `first-person-and-the-shared-tier.md`. That document asked
whether the shared tier should keep blocking on first-person pronouns.
This one defines the two candidate rules precisely, measures them
against the real corpus, and states what each one would actually let
through.

All numbers below come from the runtime databases under
`$ASTOR_DIR` and from running the current classifier over them.

## The baseline

`_FPII_RE` in `astor_memory/nest/visibility_classifier.py`:

```python
r'(?:'
r'我(?!们|自己)|我的|我们|我自己|'
r'\bmy\b|\bour\b|\bmine\b|\bus\b|\bme\b|'
r'家里|family|home|home address'
r')'
```

`has_first_person` returns true on any match, and `classify_visibility`
treats a first-person match as one of four equal blockers:

```python
signals = {'pii': ..., 'first_person': ..., 'emotion': ..., 'geographic': ...}
has_any_block = any(bool(v) for v in signals.values())
```

So one pronoun in a technical note is worth exactly as much as a phone
number. Measured on the runtime:

| branch | facts blocked alone by this branch | share |
|---|---|---|
| 我 (singular) | 93 | 66.9% |
| `home` (bare substring) | 25 | 18.0% |
| 我自己 | 13 | 9.4% |
| 我们 | 12 | 8.6% |
| 我的 | 10 | 7.2% |
| our / my / family / mine / us / me / 家里 | 20 | 13.7% |
| **total blocked by first_person alone** | **139** | |

Against the 1,488 facts whose kind is shareable, first-person is the
sole blocker for 139 of them — 65% of all blocks. The `home` branch is
a plain bug: `home` with no word boundary fires on `<home>` placeholders
and on "the home directory".

## Option B — drop the plural pronouns

Remove `我们`, `\bour\b`, `\bus\b` from the hard block; keep the
singular forms.

Measured:

| | blocked | released | release rate |
|---|---|---|---|
| current | 139 | 1,349 | 90.7% |
| B | 103 | 1,385 | 93.1% |

B releases 36 facts and nothing else changes. But the plural branches
were only 20 of the 139 blocks. B does not touch the 93 single-person
`我` blocks or the 25 `home` blocks, which is where the mass is.

On a hand-built negative set of 11 private sentences, B lets one
through (`our family`) because dropping the plural also drops the
plural-with-marker case.

**B is the weakest of the three.** It addresses a symptom that accounts
for 7% of the problem.

## Option C — plural blocks only near a personal marker

The rule becomes: a plural pronoun blocks the write only when a personal
marker sits in the same text. A marker is a possessive form or a private
noun, in either language:

```
我的|我自己|我们的|\bmy\b|\bmine\b|\bme\b
家里|家人|family|home address
账户|余额|存款|工资|收入|资产|信用卡|贷款|投资组合|持仓
老婆|老公|女朋友|男朋友|父母|父亲|母亲|爸爸|妈妈|孩子|女儿|儿子
住址|地址|手机号|微信号|邮箱|身份证|银行卡|社保|户口
用户主张|用户希望|用户偏好|用户要求|用户规定|用户不满|用户批评|
用户明确|用户强调|用户声明|用户拒绝|用户纠正|用户说
\baddress\b|\bpassword\b|\baccount\b|\bsalary\b|\bincome\b|
\bwife\b|\bhusband\b|\bfamily\b|\bdaughter\b|\bson\b
```

The singular forms keep blocking on their own, and `home` as a bare
word stops blocking (it only blocks in `my home` / `home address`).

Measured on the real corpus:

| | negatives leaked | negatives correctly blocked |
|---|---|---|
| current | 0 | 11 |
| B | 1 | 10 |
| **C** | **0** | **11** |

C is the only candidate that keeps the negative set fully blocked while
releasing more of the corpus than B. On the shareable-kind corpus C
releases 36, identical to B, but it releases them for the right reason —
the plural pronoun is present and no personal marker is near it.

## Option D — let the caller declare the tier

Currently `visibility_hint='commons'` is blocked by any of the four
signals:

```python
if hint == 'commons':
    if has_any_block:
        return {'visibility': 'personal', 'reason': 'hint_commons_blocked_by_signals'}
```

D changes it so that an explicit `commons` hint clears the
first-person signal, while PII, emotion and geographic keep blocking.

On its own, D is not safe. Measured on 14 private sentences, D alone
lets 8 through:

```
我家的地址是某某路 12 号        personal → commons
我的账户余额是 120000           personal → commons
our family                     personal → commons
我老婆叫小美                    personal → commons
我的工资收入是 50000            personal → commons
my salary is high               personal → commons
my password is hunter2          personal → commons
我的家人生日在 3 月              personal → commons
```

None of these trip the PII, emotion or geographic detectors. They are
caught only by the first-person signal, which D just cleared.

Gating D on the Layer 2 soft detector (`detect_personal_content`) does
not fix it. Layer 2's `name_chinese` rule is
`[一-鿿]{2,4}(?:家|说|做|去|来|在|到|跟|和|把|给|叫|想|觉得|发生|去世|结婚)`,
which matches ordinary technical prose — "日常就**在**" is enough. Using
it as a gate both leaks real private facts and blocks shareable ones.

### D2 — gate D on the marker list instead

D2 is D with the marker list from C as the gate: an explicit
`commons` hint clears the first-person signal only when no personal
marker is present.

Measured on the real personal corpus — 585 facts that are personal today
and are blocked by first-person alone:

| rule | personal facts that would become commons |
|---|---|
| current | 0 |
| C | 77 |
| D (unconditional) | 585 |
| D gated on Layer 2 | 294 |
| **D2 (gated on the marker list)** | **364** |

D2 still releases 364. On a hand-built set of 17 private sentences, D2
and C both leak exactly one — `张伟去了北京`, which neither the
first-person signal nor the marker list covers, and which the current
rule already lets through.

So D2 is not better than C on the leak axis. It releases more facts and
blocks the same negatives.

## The honest comparison

| | C | D2 |
|---|---|---|
| negatives leaked (hand-built, 17) | 1 | 1 |
| real personal facts released | 77 | 364 |
| shareable-kind facts released | 36 | 36 |
| needs a caller change | no | yes — `visibility_hint` must be plumbed through the write path |
| depends on a list | marker list | marker list |

C is strictly better on the leak axis and identical on the positive
axis. D2's extra releases are all facts that the caller explicitly
declared as shareable — which means the caller, not the detector, is
making the judgement. That is the right place for that judgement, and
it is also the reason it is dangerous: the same mechanism that lets a
method note through lets a mistake through.

## What the released facts actually look like

The 77 facts C releases, categorised by hand:

| category | count |
|---|---|
| method or technical path | 21 |
| contains an identity or channel name | 25 |
| other (mostly method notes the crude classifier missed) | 30 |
| a user's stated position or preference | 1 |

The identity group contains host paths and channel boundaries:

```
Phase A MCP ship 9/15: <home>\.evox\agent\mcp-servers\astor-memory-rest-mcp\server.py
R-class: cron deliver="origin" resolves the home channel at the moment of trigger
```

Both are handled downstream. `scrub_local_identifiers` in
`astor_memory/nest/public_scrub.py` runs on every public-tier write and
rewrites host-local paths and identifiers, and the PII detector
catches the rest. The shared tier's own comment says it: "keeps the
method and loses the machine".

## Recommendation

**C, on its own. Not D, and not D2.**

C fixes the detector. D and D2 move the decision to the caller, which
means the quality of the shared tier depends on every caller passing
the right hint — including the ones that get it wrong.

C does not need a caller change, keeps every private sentence in the
negative set blocked, and releases the same 36 shareable facts that B
does but for the right reason.

If D is wanted later, it should land as an additional channel with the
marker list as its gate, on top of C, and only after the write path can
carry a hint end to end. `visibility_hint` is read in `server.py` at
the write endpoint but is not exposed by the CLI, so D2 today would
only be reachable by an HTTP caller.

Two things neither C nor D touches:

* The `home` substring is a bug in its own right and should be fixed
  regardless of which option is chosen: `home` without a word boundary
  fires on `<home>` placeholders and "the home directory". 25 facts.
* `US.SPY` and other ticker symbols trip `pii_keyword_context`, a
  different detector with real privacy weight. That is a separate
  decision.

## The measured artifact

`tools/_eval_first_person_options.py`, `tools/_eval_option_d.py`,
`tools/_eval_against_real_private.py`, `tools/_eval_c_and_d_gated.py`
and `tools/_eval_c_with_stance.py` reproduce every number above
against the live runtime. They are measurement scripts, not part of the
package.
