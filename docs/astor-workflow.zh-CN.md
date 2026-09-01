# Astor 工作流 — 写/读/分析/排序/突出

> **已 ship**：2026-08-19 (v1.2.7)
> **状态**：`astor-extract-facts` 接受 `why` + `outcome` 参数
> **读者**：agent 作者 + 运维

---

## 5 阶段工作流

astor **不只是** 读/写。它是一条管道：捕获意图、分类结果、审计调用、排序结果、突出关键事实。

```
┌──────────────────────────────────────────────────────────────┐
│ 阶段 1：WRITE（捕获）                                        │
│   astor_extract_facts(text, mode, why, outcome)            │
│   - regex OR llm                                            │
│   - capture_intent 检测（提升 confidence）                   │
│   - success/error 捕获 + 审计日志                           │
└──────────────────────────────────────────────────────────────┘
        ↓
┌──────────────────────────────────────────────────────────────┐
│ 阶段 2：READ（检索）                                          │
│   /v1/read (hybrid: vector + BM25 + jaccard)                │
│   nest.search() → bus.memory_canonical 查询                  │
│   importance 过滤 (0.65+)                                   │
│   + clean_recall.py（test marker 过滤）                       │
└──────────────────────────────────────────────────────────────┘
        ↓
┌──────────────────────────────────────────────────────────────┐
│ 阶段 3：ANALYZE（判断）                                       │
│   llm_call_log: success/error/latency 审计                    │
│   extraction_cache: 按 content hash 去重                      │
│   schema_version: 迁移历史                                   │
│   audit db: first_admin 操作                                  │
└──────────────────────────────────────────────────────────────┘
        ↓
┌──────────────────────────────────────────────────────────────┐
│ 阶段 4：RANK（优先级）                                        │
│   hybrid_merge: vector (0.6) + BM25 (0.4) + jaccard          │
│   importance 排序                                             │
│   outcome boost: success > neutral > error                    │
└──────────────────────────────────────────────────────────────┘
        ↓
┌──────────────────────────────────────────────────────────────┐
│ 阶段 5：HIGHLIGHT（聚焦）                                     │
│   importance 字段 (0-1)                                       │
│   kind 字段 (user_preference > decision > ...)                │
│   confidence 字段 (LLM 或 capture_intent boost)                │
│   outcome tag (success/error) 用于排序                         │
└──────────────────────────────────────────────────────────────┘
```

---

## 阶段 1 — Write（捕获）

### `astor_extract_facts` 签名 (v1.2.7+)

```python
astor_extract_facts(
    text: str,
    mode: Literal['auto', 'none', 'regex', 'llm'] = 'auto',
    *,
    tier: str = 'public',
    user_id: str | None = None,
    actor: str = 'system',
    why: str | None = None,              # v1.2.7 新增
    outcome: Literal['success', 'error', 'neutral'] = 'neutral',  # 新增
) -> list[AstorFact]
```

### `why` 参数 — 区分 "do X" 和 "avoid X"

- `why='success_recipe_for_X'` — 写某个**有效**做法的 lesson
- `why='error_pattern_documented_Y'` — 写某个**失败**做法的 lesson
- 自由字符串，记录在 `context` 字段 + llm_call_log 的 `reason` 字段

**规则**：写可能被 recall 的 facts 时总是设 `why`。没有 `why`，recall 无法区分 "do X" 和 "avoid X" — 见 bus-mem-1042 (id=1373)。

### `outcome` 参数 — 驱动 recall 排序

- `'success'` — 做 X。Recipe、最佳实践、有效方法。Tag：`outcome:success`
- `'error'` — 避免 X。反模式、错误、不要再犯。Tag：`outcome:error`
- `'neutral'` — 事实上下文，既不是 recipe 也不是警告。Tag：省略（默认）

**下游效果**：用户问 "how do I..." 时，recall 可以提升 `outcome:success` facts；问 "should I do X" 时降低 `outcome:error` facts。

### 示例

```python
# 成功 recipe
facts = astor_extract_facts(
    'Always use --why flag when logging lessons to astor',
    mode='regex',
    why='success_recipe_for_bus_mem_1042',
    outcome='success'
)

# 错误模式
facts = astor_extract_facts(
    'When --why missing, recall ranking degrades',
    mode='regex',
    why='error_pattern_documented_2026-08-19',
    outcome='error'
)
```

结果：

- `tags`：`['fact', 'auto_extracted', 'outcome:success']`
- `context`：`[why] success_recipe_for_bus_mem_1042\n<original text>`
- llm_call_log `reason`：`success_recipe_for_bus_mem_1042`

---

## 阶段 2 — Read（检索）

### Hybrid recall

```python
# 默认配置：
#   bm25_weight=0.4, vec_weight=0.6
#   hybrid=True（合并打分）

# 纯 vector（旧）
results = nest.search(query_emb, limit=top_k)

# Hybrid（推荐）
merged = hybrid_merge(
    bm25_hits=lex.bm25_search(query),
    vector_hits=nest.search(query_emb),
    bm25_weight=0.4,
    vec_weight=0.6,
    keyword_hits=...,
    query_keywords=...,
)
```

### 过滤管道（client-side）

```python
from clean_recall import clean_recall
result = clean_recall("user coffee preference", top_k=5)
# 返回 dict：
#   - count: int（过滤后）
#   - total_before_filter: int
#   - filtered: int
#   - results: list 干净 facts（无 test markers）
```

应用的过滤：

1. 跳过带 test markers 的 facts（`test`, `e2e_test`, `forgettable`, `marker` 等）
2. 跳过 `importance < 0.65` 的 facts
3. 跳过 `kind='system_event'` 的 facts（测试事件）

### Server endpoint

```bash
# POST /v1/read
curl -X POST http://127.0.0.1:7803/v1/read \
  -H "Content-Type: application/json" \
  -d '{"query": "coffee preference", "top_k": 5, "tier": "public"}'
```

返回：

```json
{
  "count": 4,
  "results": [
    {
      "fact_id": 3,
      "content": "用户偏好小杯黑咖啡,不糖不奶",
      "kind": "fact",
      "similarity": 0.51,
      "importance": 0.85,
      "namespace": "...",
      "tags": "...",
      "score_kind": "hybrid"
    }
  ]
}
```

---

## 阶段 3 — Analyze（判断）

### `llm_call_log` schema

```sql
CREATE TABLE llm_call_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts DATETIME NOT NULL,
    actor TEXT NOT NULL,            -- 'first_admin' | 'admin' | 'user:<id>' | 'system'
    user_id TEXT NOT NULL,
    tier TEXT NOT NULL,             -- 'public' | 'source' | 'private' | 'repo'
    provider TEXT NOT NULL,         -- 'm3' | 'openai' | 'anthropic' | 'gemini' | 'regex_fallback'
    model TEXT,
    operation TEXT NOT NULL,        -- 'extract' | 'summarize' | 'classify' | 'embed'
    input_hash TEXT NOT NULL,       -- sha256，永不暴露内容
    input_length INTEGER,
    output_json TEXT,
    success INTEGER NOT NULL DEFAULT 0,
    error_msg TEXT,
    latency_ms INTEGER,
    reason TEXT                     -- 从 `why` 参数填入 (v1.2.7)
);
```

### 审计查询

```python
# 找出今天所有错误
astor_admin_audit(action='extract', since='2026-08-19T00:00:00Z', limit=20)

# 每个 provider 的延迟统计
SELECT provider, AVG(latency_ms), COUNT(*), SUM(success)
FROM llm_call_log
WHERE ts > '2026-08-19'
GROUP BY provider
```

---

## 阶段 4 — Rank（优先级）

### 默认 hybrid 权重

- vector：0.6（语义相似）
- BM25：0.4（lexical exact-match）
- keyword jaccard：隐式 boost（token 重叠）

### Outcome boost（计划 v1.2.8）

排序结果时应用 outcome tag 加权：

```
score = (vector_score * 0.6 + bm25_score * 0.4) * outcome_weight

outcome_weight:
  outcome:success → 1.5x boost   （recall "how do I" 偏好这些）
  outcome:neutral  → 1.0x         （默认）
  outcome:error    → 0.3x suppress（recall "should I do X" 降权这些）
```

这**计划用于 v1.2.8** — 当前排序还没用 outcome。

---

## 阶段 5 — Highlight（聚焦）

### System prompt 注入

astor plugin 的 `prefetch()` 返回 top-5 facts，格式如下：

```
[astor-memory recall · top 5]
- (id=3, tier=public) 用户偏好小杯黑咖啡,不糖不奶
- (id=11, tier=public) 用户偏好 aggressive investment
- ...
```

Hermes 在每条消息前注入到 system prompt，所以 agent 看到相关上下文。

###未来增强 (v1.2.8)

在 recall 输出里加 outcome：

```
[astor-memory recall · top 5]
- (id=3, tier=public, outcome:success) 用户偏好小杯黑咖啡,不糖不奶
```

这让 agent 知道哪些 fact 是 "do X" vs "avoid X"。

---

## 运维清单

### 每天 / 每次写

- [ ] 所有 `astor_extract_facts` 调用都带 `why` 参数（如果 fact 可能被 recall）
- [ ] 所有 `astor_extract_facts` 调用都设 `outcome`（success/error/neutral）
- [ ] 检查审计日志：`astor_status` 工具

### 每周

- [ ] 跑 `backfill_embeddings.py`（如果新 facts > 200 但没有 embedding）
- [ ] 审计 `llm_call_log` 的 success/error 率
- [ ] 如果工作流变了，更新 release notes

### 每季度

- [ ] 复查 recall precision（top-5 在测试 query 上的准确率）
- [ ] 把 scenario clustering v2 应用到分组 facts
- [ ] 验证 9-db schema 完整性（`astor_verify_forge_schema`）

---

## 相关文档

- `docs/fact-lifecycle.md` · [中文](fact-lifecycle.zh-CN.md) — 4 层架构 + 写入规则
- `docs/scenario-layered-recall.md` · [中文](scenario-layered-recall.zh-CN.md) — L2 聚类工作流
- `docs/bot-stop-semantics.md` — per-user 偏好注入（计划）
- `astor_memory/forge/extractor.py` — 源代码 (v1.2.7)
- `scripts/clean_recall.py` — client-side 过滤
- `scripts/backfill_embeddings.py` — embedding 维护

---

## Ship log

- **v1.2.7 (2026-08-19)**：`why` + `outcome` 参数加到 `astor_extract_facts`。Tags 含 `outcome:<success|error>`，context 含 `[why] <reason>`，审计日志 reason 填入。
- **v1.2.6 (2026-08-15)**：`llm_call_log` schema + 审计日志加上。
- **v1.2.0 (2026-07-23)**：9-db schema port、hybrid merge、scenario clustering v2。

---

*Astor 工作流 v1.2.7 — 2026-08-19 ship crew*