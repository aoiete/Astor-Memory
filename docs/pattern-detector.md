# Success-Pattern Auto-Detect & Auto-Promote (v1.13.0)

> Companion to [`fact-lifecycle.md`](./fact-lifecycle.md). This doc describes
> how astor-memory automatically identifies user-stated successes ("搞定了",
> "this works", "shipped") and promotes recurring patterns to
> `tier='public'` for cross-session recall.

## What it does

`astor_memory.forge.pattern_detector` ships three primitives (v1.13.0):

| Function | Purpose |
|---|---|
| `astor_detect_success_pattern(text)` | Returns `True` if `text` describes a successful recipe / behavior loop. Heuristic regex (zh + en). No LLM cost. |
| `astor_score_success_strength(text)` | Returns a 0.0-1.0 strength score. 0.5 = single phrase, 0.75 = two phrases, 1.0 = ≥3 distinct phrases. |
| `astor_count_similar_success_facts(content, tier)` | Counts existing facts with Jaccard-similar content tagged `outcome:success` in the given tier. |
| `astor_promote_recurring_success(fact_id, tier='public', threshold=3)` | Promotes a fact to `tier='public'` if similar success-tagged facts recur ≥ `threshold` times. Audits the promotion. |

A new CLI subcommand `am learn <text>` wraps these for one-line usage.

## Why this matters

Without auto-detection, every successful recipe has to be manually tagged
with `outcome='success'` and promoted to `tier='public'` by hand. Over time,
recurring recipes stay buried in private tiers and the agent has no way
to recall "this pattern worked 4 times before — try the same approach."

With auto-detection + auto-promote:

1. User says "搞定了 cron 配置，今天跑通了"
2. `am learn` writes the fact tagged `outcome='success'`
3. After the 3rd similar success statement, the fact auto-promotes to
   `tier='public'`
4. Future agent sessions recall the recipe automatically via the standard
   recall pipeline

## Phrase patterns (zh + en)

### Chinese (18 patterns)

| Pattern | Example |
|---|---|
| 成功了 | "配置成功了" |
| 搞定了 | "搞定了 cron 配置" |
| 这个方法可以 | "这个方法可以，下次还用" |
| 这招可以 | "这招可以" |
| 记住了 | "记住了，先 grep 再 patch" |
| work了 | "work 了" |
| 跑通了 | "跑通了" |
| 通了 | "终于通了" |
| ok了 | "ok 了" |
| 成了 | "成了" |
| 通过了 | "测试通过了" |
| 就这么干 | "以后就这么干" |
| 这样就好了 | "这样就好了" |
| 能用了 | "现在能用了" |
| 可以了 | "现在可以了" |

### English (11 patterns)

| Pattern | Example |
|---|---|
| this works | "this works perfectly" |
| shipped | "shipped it yesterday" |
| nailed it | "nailed it" |
| figured out | "I figured out the bug" |
| got it working | "got it working" |
| that's the trick/way | "that's the trick" |
| works for me | "works for me" |
| that worked | "that worked" |
| this is the way | "this is the way" |
| done | "done" |

## CLI usage

```bash
# 1. Write + auto-detect + auto-promote in one command
am learn "搞定了 cron 配置，今天跑通了"

# 2. Skip auto-promotion (just write with outcome=success)
am learn "搞定了 cron 配置" --no-promote

# 3. Custom threshold
am learn "搞定了 cron 配置" --threshold 5

# 4. Write to public tier directly (no promotion needed)
am learn "搞定了 cron 配置" --tier public
```

Sample output:

```
[astor] learn: outcome=success strength=0.50
   tier=private fact_ids=[42]
   (similar success facts below threshold 3; not promoted)
```

After 3 similar success statements:

```
[astor] learn: outcome=success strength=0.50
   tier=private fact_ids=[51]
   PROMOTED to tier=public (1/1 facts crossed threshold)
```

## How the auto-promote logic works

```
1. User runs `am learn "<text>"`
2. astor_detect_success_pattern(text) → bool
3. If success:
     a. Write fact via bus with tag `outcome:success`
     b. Count similar success-tagged facts in current tier via
        astor_count_similar_success_facts (Jaccard threshold 0.3)
     c. If count >= threshold:
          - UPDATE memory_canonical SET tier='public' WHERE id=?
          - INSERT audit_log row tagged 'promote_recurring_success'
4. Otherwise: write with outcome='neutral', no promotion
```

Jaccard similarity threshold: `0.3` (configurable via parameter).

Recurrence threshold: `3` (configurable via `--threshold`).

## Tuning

| Knob | Default | Effect |
|---|---|---|
| `jaccard_threshold` | `0.3` | Lower → more facts considered "similar"; higher → stricter |
| `threshold` (in `astor_promote_recurring_success`) | `3` | Min recurrence count to trigger promotion |
| Success-phrase regex list | 18 zh + 11 en | Add more phrases by extending `_SUCCESS_PATTERNS_ZH` / `_SUCCESS_PATTERNS_EN` |

To add a new success phrase (e.g. "yay"), append to
`_SUCCESS_PATTERNS_EN` in `astor_memory/forge/pattern_detector.py`:

```python
_SUCCESS_PATTERNS_EN = [
    ...existing...
    r'\byay\b',  # new
]
```

## What this does NOT do

- **No LLM-based classification.** Uses regex only. Cost is zero; coverage
  is bounded by the phrase list.
- **No cross-user promotion.** A private_<user> success pattern promotes
  within its own scope; it does NOT cross-pollinate other users' private
  tiers. (Public promotion is intentional and the user's own data.)
- **No auto-demotion.** Once promoted to public, a fact stays there
  unless manually demoted via `am cascade` / direct SQL.
- **No time decay on recurrence count.** "3 occurrences" means lifetime,
  not "3 in the last week". A future version may add decay.

## Reference

- Code: `astor_memory/forge/pattern_detector.py` (~290 LOC)
- Tests: `tests/test_pattern_detector.py` (36 tests, all green)
- CLI: `astor_memory/cli/main.py::cmd_learn`
- Phrase constants: `_SUCCESS_PATTERNS_ZH` / `_SUCCESS_PATTERNS_EN`

## Change history

- 2026-09-02: initial ship (v1.13.0). 36 tests passing.