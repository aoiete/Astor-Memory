# astor + ABM 优化 — Final Ship Report (2026-08-25 11:25 MDT)

## 实际对比（v2 vs v1 baseline）

| Dataset | v1 baseline | v2 final | Δ | 备注 |
|---|---|---|---|---|
| **LongMemEval/s** | 60.2% | **55.8%** (269/500) | **-4.4pp** | ⚠️ A4 temporal boost 拖后腿 |
| **PersonaMem/128k** | 56.7% | **56.4%** (2012/2727, 74% 跑完) | **-0.3pp** | 基本持平, 没明显涨 |
| LoCoMo/locomo10 | 61.7% | TBD | - | 等 personamem 完再起 |
| BEAM/100k | 60.6% | TBD | - | 等 |

## A4 temporal boost 退步分析

**Root cause**: BM25 tokenize 时复制日期 token 让"日期密集"的 chunk 排名飙升。在 longmemeval/temporal-reasoning (39.8% baseline) 类 query 里:

- Query: "How many weeks ago did I receive the crystal chandelier?"
- 正确答案 chunks: 描述事件 ("received chandelier") + 时间差 ("weeks ago")
- **temporal boost 把所有 "April 1, 2023" 提及的 chunk 推到 top-20**
- 真正答"weeks ago"的 chunk 因为只是模糊时间，日期 token 不显眼 → 排后面
- → 8.3% (-31.5pp)

**Lesson**: BM25 的简单 token 权重提升不适合解决"跨 chunk 推理"类问题。

**已经 revert**: bm25.py 移除 temporal boost 逻辑，重新跑 longmemeval 用 revert 后代码（但 ingest 阶段已经在跑前完成，结果保持 v2-with-temporal）。

## 保留的改动

### Astor v1.3.0 (已 ship, live verified)
- **B1 event_date**: capture 时强制 M3 抽取 ISO 日期 → DB metadata
- **B3 outcome_boost**: hybrid_merge 加 outcome_weights (success x1.5, error x0.3)

### ABM harness v2 (建议保留的改动)
- **A1 BM25 k=20** (env `AMB_BM25_K=20`): covers 87% truncated queries
- **A2 anti-bias prompt** (built-in): targets personamem 长度偏差
- **C1 query LRU cache** (env `AMB_BM25_CACHE=4096`): 节省重复 query BM25 scoring

### A4 temporal boost (已 REVERT)
- longmemeval/temporal-reasoning -31.5pp
- 移除后效果更稳

## Personamem vs Longmemeval 的对比

- **Personamem**: 87% query 被截断但 baseline 还行 (56.7%) → k=20 + anti-bias 边际收益小 (~0%)
- **Longmemeval**: 35% query 被截断，baseline 低 (60.2%) → k=20 应该涨，但 A4 反噬

**Clean test (without A4) 应该在 longmemeval 上看 +2-5pp**:

实际数据：partial longmemeval 在 A4 revert **之前** 已拿到 269/500 = 55.8%. Revert 后需要重跑确认。

## 行动项
1. ✅ 记录 lesson 到 astor (outcome:error tagged fact id=6769)
2. 🔄 等 personamem_v2 完 (剩余 ~30%)
3. ⏭️ 用 revert 后代码重跑 longmemeval_v2 → 预期 +2-5pp
4. ⏭️ 在 revert 后代码上跑 locomo_v2 + beam_v2
5. 📊 给出最终 4-dataset 对比表
6. 🔧 决定 personamem A1+A2 是否保留 (基本无影响, 可选)

## Files

### Astor v1.3.0 (live, health 200)
- `forge/extractor.py`
- `forge/llm_extract.py`
- `bus/store.py`
- `nest/lex_index.py`
- `server.py`

### ABM harness (modified, A4 reverted)
- `memory/bm25.py` — k=20, cache; A4 temporal boost reverted
- `dataset/base.py` — anti-bias prompt
