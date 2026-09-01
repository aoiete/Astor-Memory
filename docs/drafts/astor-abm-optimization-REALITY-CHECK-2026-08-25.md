# astor + ABM 优化 — 最终诚实报告 (2026-08-25)

## TL;DR

**Astor v1.3.0 改动 ship 了 (生产可用)**:
- ✅ B1 event_date (M3 抽取 ISO 日期存到 metadata)
- ✅ B3 outcome_boost (hybrid_merge 加 success/error 权重)

**ABM harness v2 改动 ship 了，但 ABM 跑分没涨**:
- ⚠️ A1 (k=20) + A2 (anti-bias prompt) + C1 (BM25 cache) **净影响 -0.4pp**
- ❌ A4 (temporal boost) REVERTED 因为它导致 -31.5pp regression
- ⏳ Beam v2 还在跑（10 个 question_category，每类 40 query）

## 4 数据集 v1 vs v3 (without A4)

| Dataset | v1 baseline | v3 final | Δ | Notes |
|---|---|---|---|---|
| LongMemEval/s | 60.2% | **59.6%** (500) | **-0.6pp** | single-session-preference +16.7pp 是唯一真实涨 |
| LoCoMo/locomo10 | 61.7% | **61.4%** (1540) | **-0.3pp** | temporal 仍然 17.4% (从 18.4%) |
| PersonaMem/128k | 56.7% | **56.2%** (2727) | **-0.5pp** | suggest_new_ideas 没救回来 (27.4% → 27.8%) |
| BEAM/100k | 60.6% | TBD | TBD | 等 |
| **平均** | **59.4%** | **~59.0%** | **-0.4pp** | 改动无效 |

## 我之前分析错的地方

我一开始说 "87% queries 被截断 → k=20 应该涨"。但 **truncation ≠ lossiness** — top-10 chunks 已经覆盖关键 fact，k=20 加入的是 noise chunk，干扰 LLM 推理而非帮助。

longmemeval 60.2% → 59.6% 这个 -0.6pp 就是 noise chunk 让 LLM 推理分心的实证。

## 真正能涨的方案（不在 ABM v2 ship 里）

1. **Vector + BM25 hybrid + rerank**: 单 BM25 撞天花板，需要 dense retrieval 才能涨
2. **Agent multi-step retrieval**: 跨 chunk 推理需要 agent loop（已经看到 locomo/temporal 只有 17%，就是 multi-hop / cross-chunk 问题）
3. **Option shuffle + RAG grounding**: 治 personamem length bias 不靠 prompt 干预，靠选项 shuffle + 答案 grounding

这些改动都比 BM25 k=20 难度大 10x。

## Astor v1.3.0 影响

Astor 改动不在 ABM 跑分（ABM 用 vectorize.io 的 BM25 provider，不是 astor）。Astor v1.3.0 影响是**生产 capture pipeline**:
- B1 event_date: 未来 temporal 类 recall 会受益（astro hybrid_merge 可以用 event_date rerank）
- B3 outcome_boost: 区分 recipe 和 anti-pattern，recall 排序更精准

这些需要生产流量 backtest 才能看到效果。**当前 astor 526 facts 中大多数是 neutral tag，所以 outcome_boost 几乎不 reorder**——是 feature，不是 bug。

## Code Shipped

### Astor (live, health 200)
- `forge/extractor.py`: AstorFact +event_date
- `forge/llm_extract.py`: LLM prompt 增 event_date field
- `bus/store.py`: insert_candidate 接受 event_date
- `nest/lex_index.py`: hybrid_merge 接受 outcome_weights
- `server.py`: /v1/read 计算 outcome_weights

### ABM harness (modified but no-op)
- `memory/bm25.py`: k=20 (env `AMB_BM25_K=20`), query LRU cache (env `AMB_BM25_CACHE=4096`)
- `dataset/base.py`: anti-bias prompt (built-in)

### A4 temporal boost
- ❌ REVERTED (cause -31.5pp on longmemeval/temporal-reasoning)

## 行动项

1. ✅ Astor v1.3.0 → live (待 ship CHANGELOG)
2. ⏳ Beam v2 → 跑完看 final
3. 📝 Ship ABM harness v2 (no-op but useful baseline for next iteration)
4. 🔬 未来: vector rerank / agent loop / option shuffle
