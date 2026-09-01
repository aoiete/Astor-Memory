# 成功模式自动识别 + 自动升级 (v1.13.0)

> 配套 [`fact-lifecycle.md`](./fact-lifecycle.md)。本文档描述 astor-memory
> 如何自动识别用户陈述的成功事件（"搞定了"、"this works"、"shipped"）
> 并把反复出现的模式升级到 `tier='public'`，供跨 session recall。

## 它做什么

`astor_memory.forge.pattern_detector` 提供了 4 个原语 (v1.13.0):

| 函数 | 用途 |
|---|---|
| `astor_detect_success_pattern(text)` | 如果 `text` 描述了成功 recipe / 行为模式则返回 `True`。启发式 regex（中+英）。无 LLM 成本。 |
| `astor_score_success_strength(text)` | 返回 0.0-1.0 强度分数。0.5 = 一个短语，0.75 = 两个，1.0 = ≥3 个不同短语。 |
| `astor_count_similar_success_facts(content, tier)` | 统计给定 tier 内已有 fact 中内容 Jaccard 相似且打 `outcome:success` 标签的数量。 |
| `astor_promote_recurring_success(fact_id, tier='public', threshold=3)` | 如果相似 success fact 出现 ≥ `threshold` 次，则把该 fact 升级到 `tier='public'`。审计日志记录此次升级。 |

新增 CLI 子命令 `am learn <text>` 一行调用所有这些。

## 为什么需要这个

没有自动识别，每个成功的 recipe 都得手动打 `outcome='success'` 标签 +
手动升级到 `tier='public'`。时间一长，反复出现的 recipe 埋在 private
tier 里，agent 没法 recall "这个模式以前 work 过 4 次 — 试试同样思路"。

有了自动识别 + 自动升级：

1. 用户说"搞定了 cron 配置，今天跑通了"
2. `am learn` 把这条 fact 写进去，打 `outcome='success'` 标签
3. 第 3 次出现相似成功陈述后，fact 自动升级到 `tier='public'`
4. 未来 agent session 通过标准 recall 流程自动拿到这个 recipe

## 短语模式（中+英）

### 中文（18 个）

| 模式 | 例子 |
|---|---|
| 成功了 | "配置成功了" |
| 搞定了 | "搞定了 cron 配置" |
| 这个方法可以 | "这个方法可以，下次还用" |
| 这招可以 | "这招可以" |
| 记住了 | "记住了，先 grep 再 patch" |
| work 了 | "work 了" |
| 跑通了 | "跑通了" |
| 通了 | "终于通了" |
| ok 了 | "ok 了" |
| 成了 | "成了" |
| 通过了 | "测试通过了" |
| 就这么干 | "以后就这么干" |
| 这样就好了 | "这样就好了" |
| 能用了 | "现在能用了" |
| 可以了 | "现在可以了" |

### 英文（11 个）

| 模式 | 例子 |
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

## CLI 用法

```bash
# 1. 一行命令：write + 自动识别 + 自动升级
am learn "搞定了 cron 配置，今天跑通了"

# 2. 跳过自动升级（只写带 outcome=success 标签）
am learn "搞定了 cron 配置" --no-promote

# 3. 自定义 threshold
am learn "搞定了 cron 配置" --threshold 5

# 4. 直接写到 public tier（不需要升级）
am learn "搞定了 cron 配置" --tier public
```

输出示例：

```
[astor] learn: outcome=success strength=0.50
   tier=private fact_ids=[42]
   (similar success facts below threshold 3; not promoted)
```

3 次相似成功陈述后：

```
[astor] learn: outcome=success strength=0.50
   tier=private fact_ids=[51]
   PROMOTED to tier=public (1/1 facts crossed threshold)
```

## 自动升级逻辑

```
1. 用户运行 `am learn "<text>"`
2. astor_detect_success_pattern(text) → bool
3. 如果是 success：
     a. 通过 bus 写 fact，打 `outcome:success` 标签
     b. 通过 astor_count_similar_success_facts 统计当前 tier 内
        相似 success-tagged fact（Jaccard threshold 0.3）
     c. 如果 count >= threshold：
          - UPDATE memory_canonical SET tier='public' WHERE id=?
          - INSERT audit_log row 标记 'promote_recurring_success'
4. 否则：写带 outcome='neutral'，不升级
```

Jaccard 相似度阈值：`0.3`（可通过参数配置）。

反复出现阈值：`3`（可通过 `--threshold` 配置）。

## 调参

| 旋钮 | 默认 | 效果 |
|---|---|---|
| `jaccard_threshold` | `0.3` | 调低 → 更多 fact 被认为是"相似"；调高 → 更严格 |
| `threshold`（`astor_promote_recurring_success` 参数） | `3` | 触发升级所需的最小反复出现次数 |
| 成功短语 regex 列表 | 18 中 + 11 英 | 通过扩展 `_SUCCESS_PATTERNS_ZH` / `_SUCCESS_PATTERNS_EN` 加更多短语 |

加新成功短语（比如 "yay"）：

```python
_SUCCESS_PATTERNS_EN = [
    ...existing...
    r'\byay\b',  # new
]
```

## 这个不做什么

- **不做 LLM 分类。** 只用 regex。成本为 0；覆盖率受短语列表限制。
- **不跨用户升级。** private_<user> 的 success 模式在**自己 scope** 内升级；
  不会污染其他用户的 private tier。（升级到 public 是用户自己的数据。）
- **不自动降级。** 一旦升级到 public，除非通过 `am cascade` / 直接 SQL
  手动降级，否则留在那里。
- **不对反复出现次数做时间衰减。** "3 次"是 lifetime，不是"最近一周 3 次"。
  未来版本可能加衰减。

## 参考

- 代码：`astor_memory/forge/pattern_detector.py`（~290 LOC）
- 测试：`tests/test_pattern_detector.py`（36 测试，全绿）
- CLI：`astor_memory/cli/main.py::cmd_learn`
- 短语常量：`_SUCCESS_PATTERNS_ZH` / `_SUCCESS_PATTERNS_EN`

## 变更历史

- 2026-09-02：初始 ship（v1.13.0）。36 个测试全部通过。