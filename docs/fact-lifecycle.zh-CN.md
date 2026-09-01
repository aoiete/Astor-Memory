# 事实生命周期规则 — Astor-Memory

> 给引擎用。配套 `architecture.md`（描述「是什么」 — schema、store、layer）。
> 本文档描述「怎么动」 — 事实如何从 write → store → recall → verify →
> expire → destroy。

本文档是权威。如果其他文档与本文档冲突，**以本文档为准**。

## 4 层架构（对齐 TencentDB）

Astor-Memory 采用腾讯的 4 层记忆模型（参考 `D:/AI/wiki/entities/01-tencentdb-agent-memory.md`）：

| 层 | 名称 | 存储位置 | 内容 |
|---|---|---|---|
| L0 | Raw | `bus_discrete` 每事件日志 | 每条消息 + 工具调用 + 观察 |
| L1 | Fact | `memory_canonical` (SQLite) | 抽取出的结构化事实 |
| **L2** | **Scenario** | **`scenarios.db` (SQLite)** | **按主题/项目聚类的事实 — 这层就是腾讯DB说的 "scenario clustering"** |
| L3 | Profile | `memu` (memU SDK) | 长期用户人设 + 稳定偏好 |

**L2 是 2026-07-31 通过 `scripts/scenario_clustering.py` 补上的空缺**。
没有 L2，recall 是按事实粒度的（噪声大）。有 L2，recall 先查 top-N scenarios，
再下钻到这些 scenarios 里的事实。

### 冷启动 recall 流程（L2 优先）

```
query "weixin token renewal"
  ↓
bus (L1) → top-50 facts via BM25 + vector + rerank
  ↓
scenarios.db (L2) → 把这 50 个事实按关键词聚成 3 个 scenario
  ↓
top-3 scenarios 返回（relevance × decay × importance × access_count）
  ↓
下钻:每个 scenario 的 fact_ids → bus 详情
  ↓
memu (L3) profile 加在最上面（如果 user-specific）
```

这就是 `recall_3store.py` session_start routine 调用 `<active_scenarios>` 块。

## 为什么有本文档

Astor-Memory 既有的 `Fact` schema（`architecture.md`）带有 `kind`、
`confidence`、`references`。但**生命周期** — 事实何时从「待定」晋升到
「可信」，何时应该衰减，何时一个被反驳的事实属于「明确错误」vs
「过时」— 散落在 session 笔记和 bus hard rules 里。本文档把它们集中起来。

## Schema 扩展（向后兼容的增量）

`bus` 中每个事实携带以下字段（`architecture.md` 里的既有 `Fact` schema
不变；新字段可选）：

| 字段 | 类型 | 默认值 | 备注 |
|---|---|---|---|
| `kind` | enum | `fact` | `fact` / `user_preference` / `profile` / `risk_rule` / `lesson` |
| `success` | enum \| null | `None` | `True`（已验证正确）/ `False`（已验证错误）/ `None`（未验证） |
| `confidence` | float 0-1 | `0.5` | 置信度；见 `## Confidence model` |
| `verified_at` | timestamp \| null | `None` | 上次确认事实的时间 |
| `verified_by` | enum \| null | `None` | `user` / `auto` / `paper` / `test` / `cron` |
| `expires_at` | timestamp \| null | `None` | 事实过期时间；自动衰减 |
| `source` | string | `''` | `user` / `bus` / `forge` / `mempalace` / `paper:<id>` / `paper:<title>` |
| `references` | list[memory_id] | `[]` | 跨 store 反链（已有字段） |

运行时表 `bus` / `nest` / `lex` 可能还没所有列 — API 层在读取时填默认值。

## Confidence 模型

Confidence ∈ [0.0, 1.0]。默认写入 0.5。通过验证事件增加，被反驳减少。

### 初始 confidence（写入时）

| 来源 | 默认 confidence |
|---|---|
| `user` 显式陈述 | 0.7 |
| `user` 直接指令（"always do X"） | 0.85 |
| `llm` 从文本抽取 | 0.4 |
| `paper`（peer-reviewed, arxiv id） | 0.7 |
| `paper`（预印本、博客、新闻） | 0.5 |
| `cron` 动作已验证结果 | 0.85 |
| `backtest` 已证明 | 0.9 |
| `test` 已写且通过 | 0.95 |

### Confidence 增量（每次验证）

| 事件 | Δ |
|---|---|
| 同一事实再次出现，无反驳 | +0.05 |
| 用户明确确认 | +0.1 |
| 反复验证（3+ 次） | +0.05（封顶 0.95） |
| 被用户反驳 | -0.3 |
| 被 paper 反驳 | -0.2 |
| 被 backtest 反驳 | -0.4 |
| 测试失败 | -0.5（或设 `success=False`） |

## `success` 字段规则

`success` 是**真相裁定**：

| 值 | 含义 | 何时设置 |
|---|---|---|
| `True` | 已验证正确（被用户/测试/backtest 证明） | `verified_at` 事件且结果为正之后 |
| `False` | 已验证错误（被证明不正确） | `verified_at` 事件且结果为负之后 |
| `None` | 未验证（默认） | 所有写入从这里开始 |

**规则**：

- `success=None` → recall 时正常展示
- `success=True` 且 `confidence ≥ 0.7` → recall 时打"trusted"标记
- `success=False` 且 `confidence ≥ 0.7` → recall 时打"known wrong"警告
- `success=False` 不会自动删除 — 它留着当护栏（"别再这样做"）

## 写入规则（引擎）

每次写入（`am write` / `am broadcast` / `am ingest`）必须：

1. **至少打上 `kind`、`confidence`、`source`**。其他字段默认。
2. **生成 `memory_id`**（UUID v7 推荐 — 时间前缀便于排序）。
3. **插入前走 dedup 表**：
   - 如果同一 `kind` + 规范化文本在 7 天内已存在 → UPDATE（不要 INSERT 重复）
   - 如果同一 `kind` + 文本在 `mempalace`（cold）已存在 → 跨链，不重复
4. **追加到 `audit_log`**，字段：op、memory_id、source、confidence、who wrote it。
5. **跳过 `__pycache__`、`.git`、`*.bak`** — 永远不要把这些当事实 ingest。
6. **`kind=forgettable`** 事实 24 小时自动清理。

## 读取规则（recall）

Recall 组合顺序（最相关优先）：

1. **bus**（近期、hot、快） — 主 recall
2. **nest**（关联） — 链到相关事实
3. **lex**（向量余弦 ≥ 0.35） — 语义邻居
4. **mempalace**（cold） — 长尾 recall，`>90 天` AND `success=None` 时降权

Recall 注入策略：

- `success=True`，confidence ≥ 0.7 → **注入**（trusted）
- `success=None`，confidence ≥ 0.5 → **注入不打标**
- `success=False`，confidence ≥ 0.7 → **注入带警告**（"guard rail: known wrong"）
- `success=None`，confidence < 0.5 → **注入带保留**（"tentative — verify before acting"）
- `success` 任意，confidence < 0.3 → **隐藏**（太噪）
- **已过期**（timestamp 超过 `expires_at`） → **注入带 stale 标记**（"stale, verify before acting"）

每次 recall 永远取 **3 nearest + 1 conflict**（主动消歧）。禁止单事实 recall。

## 衰减与清理

- 设了 `expires_at` 的事实 — 过期时自动降到 `mempalace`（不删）
- `success=False` 且 `confidence < 0.1` 且 `last_seen > 180 天` — 可安全删除
- `kind=forgettable` — 24h 后自动删除
- `kind=lesson` — 永远不自动删除（用户硬规则 R33："数据质性判断必须 fact-check sample"）

## 跨 store 同步

任何 store 更新时，广播到其他 2 个 store（bus → nest → lex/mempalace）。
广播失败时回退到"pending sync"队列。启动时重放队列。

规则（locked 2026-07-15）：**同步是强制的** — 永远不要跳过跨 store 广播。

## 冲突解决

两个事实矛盾时：

1. 较老的事实（`verified_at < newer.verified_at`）默认输
2. 高 confidence 赢
3. `success=True` 胜 `success=None`
4. `user` 验证 胜 `auto` 验证 胜 `paper` 验证
5. 如果仍平局，两者都作为 "conflicting" 保留 — 都带 ⚠ 标签 recall

永远不要在冲突时自动删除事实。决定权在用户。

## Risk 规则（继承）

这些是来自 5-store hard rule 集的用户编码规则；放这里为完整起见：

- **R33**：数据质量判断必须 fact-check sample，绝不能只基于数量/比例自动删除。
  （来源：2026-07-09 用户抓到我差点删 meihua_案例/ziwei_四化 古典命理语料。）
- **R34**：GPU 训练/评估前必须跑 `PRE_V10_CHECK.md` 9 项验证。
  （来源：2026-07-09 用户强调"准备工作做扎实 = 稳定"。）
- **R35**：Astor 命理模型 = 仅用户私有，绝不发布、绝不外部托管、绝不开放权重。
  （来源："天机不可泄露"用户指令。）

## CRON 角色强制

cron job 存事实时必须设：

- `source=cron`
- `verified_by=cron`（如果结果已被观察）
- `confidence=0.85`（cron 是实时观察的）
- `expires_at` = `next_run_at + 1h`（cron 事实时效性强）

如果 cron 动作失败，设 `success=False` 并把 `verified_at` 打成失败时间戳。

## 风格参考

Python 实现细节见 `contributing.md` § 6（"Style reference"） — 它解释了
LLM-style normalization、dedup 算法、以及本文用到的 bus schema。

## PR / 变更流程

1. **唯一真源**：`~/.astor-memory (source)\docs\fact-lifecycle.md`（本文件）
2. **运行时副本**：`$ASTOR_DIR (runtime)\docs\fact-lifecycle.md`（通过 `astor_dev_watch.py` 同步）
3. **变更流程**：编辑源 → file watcher 自动同步 + restart → 上线
4. **向后兼容**：本文档任何变更必须是增量的 — 永远不要破坏现有字段

## 变更历史

- 2026-08-19: 初稿（按用户指令锁定 — 规则是指导不是硬编码；可扩展）