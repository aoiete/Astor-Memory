# Scenario 分层 Recall（L2 层）— Astor-Memory

> 配套 `fact-lifecycle.md`。本文档详细解释 **L2 层（Scenario Clustering）**
> — 它怎么工作、怎么用、怎么融入 4 层架构。

## L2 是什么

L2 位于 L1（原子事实）和 L3（用户 profile）之间。它**把事实分组到 scenario 里** —
共享同一主题、项目、或反复出现模式的相关事实集群。

没有 L2，recall 返回事实的扁平列表。有 L2，recall 返回：

- **Top-N scenarios**（relevance × decay × importance × access_count）
- **每个 scenario 的事实下钻**（该 scenario 内的事实本身）

这就是 `recall_3store.py` 调用的 `<active_scenarios>` 块。

## 为什么 L2 重要

没有 L2，你有 N=730 个事实。Recall 返回 5-50 个作为扁平列表。
用户得自己在脑子里重建模式。

有 L2，你有 30-100 个 scenarios。Recall 返回 3 个 scenario，事实已经分组。
用户看到：

```
[Active scenarios, ranked by relevance + decay + importance + access_count]

1. 🔥 weixin token renewal cycle (scenario_id=sc_a1b2c3, score=0.92, 12 facts)
   - fact mem-145: "Tianshu API v18 bind state expires after 30 days"
   - fact mem-289: "Bot binding refresh requires NSSM restart"
   - fact mem-512: "Weixin iLink sendmessage fails when access_token stale"
   - ... (共 12 条)

2. 📊 portfolio rebalance Q2 (scenario_id=sc_e4f5g6, score=0.78, 8 facts)
   - fact mem-156: "TFSA account over-allocated in NVDA"
   - ...

3. 💤 historical context (scenario_id=sc_h7i8j9, score=0.31, 22 facts)
   - facts > 90 days old, demoted
```

**模式可见性** — 用户/agent 立刻看到"weixin token"是个反复出现的主题，
而不是 12 条独立事实。

## 内部如何工作

### 存储（`scenarios.db`）

```sql
CREATE TABLE scenarios (
    scenario_id TEXT PRIMARY KEY,    -- md5(prefix), stable
    label TEXT NOT NULL,             -- seed 事实的前 60 字符
    keywords TEXT,                   -- JSON 数组，top-100 tokens
    fact_ids TEXT,                   -- JSON 数组，bus IDs
    importance REAL DEFAULT 0.5,    -- 0-1，值高衰减慢
    created_at REAL,
    updated_at REAL,
    last_accessed REAL,
    access_count INTEGER DEFAULT 0,
    ttl_days INTEGER DEFAULT 30      -- scenario 级 TTL
);

CREATE TABLE scenario_links (
    scenario_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    fact_source TEXT,                -- 'bus' 或 'forge' 或 'public'
    added_at REAL,
    PRIMARY KEY (scenario_id, fact_id)
);
```

### 聚类算法（`scenario_clustering.py cluster`）

贪心 + 基于关键词的聚类（无 LLM 成本）：

1. 从 `memory_canonical` 拉近期事实（默认 7 天，500 条上限）
2. 对每条事实，从 `summary + content + tags` 构建关键词集（top 50 唯一 token）
3. 与既有 scenarios 比对打分：
   - `kw_overlap` = |事实 kws ∩ scenario kws| / |scenario kws|（权重 60%）
   - `jaccard` = |A ∩ B| / |A ∪ B|（权重 40%）
4. 如果 `score >= 0.12`（阈值），分到已有 scenario
5. 否则用 `md5(text[:200])` 作 ID 新建 scenario

**为什么贪心 + 廉价**：500 事实 × 30 scenarios = 每次 cluster 跑 15,000 次比对。
笔记本上 <2 秒跑完。无 LLM 成本。

### Recall 流程（`scenario_clustering.py hydrate`）

给定 query 字符串，返回 top-N scenarios：

1. 加载所有 scenarios（限 100，按 updated_at DESC）
2. 每个 scenario 与 query 算分：
   - `kw_overlap` × 0.7 + `jaccard` × 0.3 = relevance
3. 应用衰减：`decay = max(0.1, 1.0 - days_old / 30.0)`
4. 综合分：`relevance × decay × (0.5 + 0.5 × importance) × (1 + 0.1 × access_count)`
5. 把 top-N 标记为已访问（更新 `last_accessed`，`access_count++`）
6. 下钻：从 `memory_canonical` 抓每个 scenario 的事实详情

### CLI 用法

```bash
# 聚类近期事实（最近 7 天）
python scenario_clustering.py cluster --since 7d

# 为 query 加载 scenarios
python scenario_clustering.py hydrate --query "weixin token" --top 3

# 查状态
python scenario_clustering.py status
```

## 与 `recall_3store.py` 的集成

`recall_3store.py` 的 session_start routine 调用：

```python
from scenario_clustering import hydrate, status

# 状态: 198 scenarios, 1247 fact links
scen = status()
print(f"[L2] {scen['total_scenarios']} scenarios, {scen['total_fact_links']} fact links")

# 对本 session 每个 query，预先准备好 active scenarios
active_scenarios = hydrate(query=session_query, top=3)
```

输出在 system prompt 里渲染为 `<active_scenarios>` 块。

## 何时用 L2 vs L1

| 事实新旧 | 用 |
|---|---|
| 近期（0-7 天） | L1（bus）就够 — 事实还很新鲜 |
| 较旧（7-30 天） | **L2（scenarios）** — 模式已浮现，recall 受用 |
| 久远（>30 天） | L3（长期 profile 视图）— 按 recency 衰减 + importance 加权的事实 |

**L2 何时最亮眼**：你多次 session 里有相似主题绕回来，你想看运行的历史
而不是只看今天的事实。

## TTL 和衰减

| Scenario | ttl_days | 过期后做什么 |
|---|---|---|
| 默认（importance=0.5） | 30 | 降级到 `mempalace` 归档 |
| 高 importance（≥0.7） | 90 | 降级到 mempalace 归档 |
| 高 access_count（≥10） | 延长 | `ttl_days *= 1 + log10(access_count)` |

**不自动删除** — scenario 只降级到冷存储。用户可手动重新提升。

## 调参

你可以调整 `scenario_clustering.py`：

| 参数 | 默认值 | 调什么 |
|---|---|---|
| `jaccard_threshold` | 0.12 | 调低 = 更多事实合并到更少 scenarios；调高 = scenarios 更孤立 |
| `max_scenarios` | 30 | 每次 cluster 跑的上限 |
| `since_days` | 7 | 往前拉多少天的事实 |
| `ttl_days` | 30 | 多久后 scenario 降级 |

## 性能

**Cluster 跑**（500 事实，30 scenarios）：
- 时间：笔记本 ~1.5s
- DB 写：500 条 INSERT OR IGNORE（scenario_links）+ 30 条 UPDATE（scenarios）
- 无 LLM 调用

**Hydrate**（query, top 3）：
- 时间：~200ms（SQLite SELECT + Python 打分）
- DB 写：3 条 UPDATE（last_accessed, access_count）

塞得进 cron job 的预算。

## 它住在哪

- **源**：`~/.astor-memory (source)\scripts\scenario_clustering.py` (12.8 KB)
- **运行时**：`$ASTOR_DIR (runtime)\scripts\scenario_clustering.py`（通过 `astor_dev_watch.py` 同步）
- **存储**：`$MEMORY_BUS_DIR` (legacy, pre-2026-07-14)`\scenarios.db`（2026-07-31 创建，跨 session 保留）
- **调用方**：`recall_3store.py` session_start routine
- **Wiki 参考**：`<wiki>/entities/01-tencentdb-agent-memory.md`

## 局限与已知问题

1. **贪心聚类** — 对歧义事实非确定（一条事实跨 2 个主题时，分到先打分高的那个）
2. **仅英文关键词** — 中文/混合语言事实可能聚类差
3. **无 LLM 参与聚类** — 纯关键词，细微主题（如 "weixin" vs "wechat"）可能不合
4. **TTL 硬编码** — 不能按 scenario 类型配置

如果这些成为问题，可选方案：
- 在上层加 LLM 聚类（慢、贵）
- 加 per-scenario type 元数据
- 定期用 LLM 重聚类作清理

## PR / 变更流程

1. **唯一真源**：`~/.astor-memory (source)\docs\scenario-layered-recall.md`（本文件）+ `scripts\scenario_clustering.py`
2. **运行时副本**：`$ASTOR_DIR (runtime)\docs\` + `scripts\`（通过 `astor_dev_watch.py` 同步）
3. **变更流程**：编辑源 → file watcher 自动同步 → 重启服务（仅当改了脚本时）
4. **向后兼容**：`scenario_clustering.py` 的 arg API 自 2026-07-31 起稳定

## 变更历史

- 2026-07-31：初始 ship（`scenario_clustering.py`）
- 2026-08-19：迁移到 canonical 源路径，同步到运行时，记录于此