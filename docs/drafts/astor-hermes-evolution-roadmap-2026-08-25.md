# Astor + Hermes 进化路线图 — 基于 OpenViking / OpenClaw / AdaMEM 思想

**源材料**: 微信 ClawBot 2026-08-25 8:27 (OpenViking 调研) + 14:40 (Self-Evolving Agents 对比)

**核心 takeaway**: 这些方案不是要让我们 import 它们的代码——是给我们**架构模式**,让我们在 astor + hermes 现有 5-stage pipeline 上做最划算的扩展。

---

## 1. 立即 ship (1-2 天, 改动小, ROI 高)

### 1.1 L0/L1/L2 渐进式加载 (来自 OpenViking)
**现状**: astor prefetch 一次返回 top-5 全内容(每条 ~200 chars)。每条都吃 ~200 tokens 进 system prompt。

**改造**: 给 `memory_canonical` 加 `abstract` + `overview` 列。
- L0 abstract: 一句话(≤50 tokens),从 content 自动摘要(LLM extract 时生成,存到 column)
- L1 overview: 关键参数(≤300 tokens),同样自动生成
- L2 details: 完整 content (现有)

**新增 endpoint**: `/v1/prefetch?level=L0&query=xxx` → 返回 5 个 L0 (~250 tokens 总),agent 看完后决定哪些要 drill down 到 L1/L2。

**预期收益**: system prompt token 从 ~1000 降到 ~250 → **per-turn cost 减少 75%**。

### 1.2 Adaptive top_k (来自 AdaMEM)
**现状**: astor /v1/read 用固定 top_k=10。

**改造**: 加 query complexity estimator:
- query 字数 < 20 + 单 token 高频: top_k=5 (简单)
- query 字数 > 100 OR 多实体: top_k=20 (复杂)
- multi-hop / temporal 标记: top_k=30 (cross-chunk 推理)

**实现**: query classifier 用 M3 mini call (10ms) → 输出 tier → 调 top_k。

**预期收益**: 简单 query 不浪费 token;复杂 query 拿到完整 context。**recall +5~10pp on multi-hop queries**。

### 1.3 Experience library (来自 OpenClaw Experience)
**现状**: astor 有 `tags: ['outcome:success', 'outcome:error']` 但只有单条 fact,没有"上次为什么这样做成功了" 的轨迹记录。

**改造**: 新增 `memory_experience` 表,字段:
- `trigger_fact_ids`: 触发的 fact IDs
- `action_taken`: 用户/agent 做了什么
- `result`: success/failure/partial
- `reflection`: 为什么这样/失败原因 (LLM 总结)
- `next_step_hint`: 下次遇到类似情况怎么做

auto-capture hook 触发: 当 on_session_end 时,如果 session 有 outcome:error fact,生成 experience 行。

**预期收益**: "踩过的坑" 不会重复,跨 session 学习。具体可参考 OpenClaw Experience 字段设计。

---

## 2. 中期 ship (1 周, 改动中, 需思考设计)

### 2.1 Self-reflection on failure (来自腾讯云反思机制)
**现状**: `api_request_error` hook 只写 `kind=rule, outcome=error` 单条 fact,没反思"为什么会 401"。

**改造**: 加 `astor_reflect.py`:
1. api_request_error 触发 → LLM call (mini): "给我这个错误,可能的根因,下次怎么避免"
2. 写反思 fact (kind=reflection, importance=0.9)
3. 加 outcome:error tag → hybrid_merge 排序自然降权
4. 加 reflection_for tag → 类似错误可以聚合

**预期收益**: 同一类错误 (比如 M3 rate limit) 多次发生时,前几次的错误反思会被 recall,后续 agent 主动避开。

### 2.2 Hermes Skill library 整合 (来自 OpenClaw Skill + MemSkill)
**现状**: hermes 有 skill 目录,但 skill 都是手写的 metadata, 没有"成功 SOP 自动抽取"。

**改造**: 加 `astor_extract_skill.py`:
- 监控 on_session_end hook
- 如果 session 包含 ≥3 个 outcome:success facts + ≥1 个 outcome:neutral 用户交互
- 提取模式: "用户问 X → agent 用 Y skill → 成功" 
- 写 kind=skill fact (一个 skill = 一个 canonical row),importance=0.85
- tag: skill:<skill_name>, outcome:success

**预期收益**: skill 库从 22 个手写 → 几百个自动抽取。MemSkill 的核心思想,适配到 hermes 实际架构。

---

## 3. 长期 ship (2-4 周, 改动大, 真正进化的部分)

### 3.1 Virtual filesystem (来自 OpenViking)
**现状**: astor facts 是平铺 list,recall 是 top-K。

**改造**: 加 `directory tree` 在 memory_canonical 上:
```
/memories/
  /user-preferences/
    /food/  (abstract: 5 facts, overview: 8KB)
    /work/
  /procedures/
    /trading/
    /coding/
  /projects/
    /astor-memory/
```

每个 fact 有一个 `directory_path`。recall 时先走 `tree` 命令浏览目录(L0/L1),再 `cat` 进具体 fact (L2)。

**价值**: agent 自己决定 recall 深度,而不是由 astor 固定 top-K。这是 OpenViking 最核心的范式。

**实施成本**: 大——schema 要改 (加 directory_path + tree 索引),extract 时要分类到目录,recall 路径完全重写。但 ROI 也最高 (这是真正的 "context database" vs "vector store")。

### 3.2 Agent multi-step retrieval (来自 AdaMEM + MemSkill)
**现状**: astor_recall 一次返回 top-K。

**改造**: agent 在 recall 时:
1. 第一轮: query → top-10 L0 (≤500 tokens)
2. agent 看完判断: "够了吗?够 → exit;不够 → 第二轮"
3. 第二轮: query 改写 + top-10 L1
4. ... 直到 agent 觉得够或 N 轮

**实施**: hermes-agent 的 memory_provider 接口支持 `recall_multi_round(query, max_rounds=3)`。

---

## 4. 已 ship (本次改造基础)

- ✓ v1.3.0 event_date + outcome_boost
- ✓ v1.4.0 fastembed 依赖修复 (你给的微信"北斗" silent-fail 案例)
- ✓ v1.5.0 路径统一到 runtime single source of truth

---

## 优先级建议

**今天做 (1.1 + 1.2)**:
- L0/L1 progressive loading (最有 ROI,直接省 token)
- Adaptive top_k (改一个 endpoint,几行代码)

**明天做 (1.3 + 2.1)**:
- Experience library (新表,新 hook,但 ROI 高)
- Self-reflection on failure (已经有 hook,加反思 LLM call)

**下周做 (2.2)**:
- Skill auto-extraction

**下月做 (3.1 + 3.2)**:
- Virtual filesystem (大改造,需要 design review)
- Multi-step retrieval
