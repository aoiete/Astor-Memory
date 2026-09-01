# astor + ABM 优化 — TRULY FINAL Ship Report (2026-08-25 11:48 MDT)

## 实际对比（v2/v3 vs v1 baseline）

| Dataset | v1 | v2/v3 final | Δ | Driver |
|---|---|---|---|---|
| **LongMemEval/s** | 60.2% | **64.2%** (v3 partial 151/500) | **+4.0pp** | A1 k=20 + C1 cache |
| **PersonaMem/128k** | 56.7% | **56.22%** (2727/2727) | **-0.48pp** | A1+A2 无明显收益 |
| LoCoMo/locomo10 | 61.7% | TBD | TBD | 没时间跑 v3 |
| BEAM/100k | 60.6% | TBD | TBD | 没时间跑 v3 |

## Personamem v2 by question_type
| Question Type | v1 | v2 | Δ | 解读 |
|---|---|---|---|---|
| acknowledge_latest_user_preferences | 68.9% | 67.9% | -1.0pp | 持平 |
| suggest_new_ideas | 27.4% | **27.8%** | **+0.4pp** | anti-bias prompt 微涨 (无大用) |
| provide_preference_aligned_recommendations | 50.4% | **52.7%** | **+2.3pp** | k=20 涨 |
| track_full_preference_evolution | 70.1% | 69.5% | -0.6pp | 持平 |
| revisit_reasons_behind_preference_updates | 75.8% | 75.1% | -0.7pp | 持平 |
| generalize_to_new_scenarios | 40.8% | **34.7%** | **-6.1pp** | k=20 引入 noise chunk 拉低 |
| recall_user_shared_facts | 57.9% | **60.8%** | **+2.9pp** | k=20 涨 |

## Longmemeval v3 by question_type (partial)
| Question Type | v1 | v3 | Δ |
|---|---|---|---|
| multi-session | 46.6% | 48.1% | **+1.5pp** |
| temporal-reasoning | 39.8% | 37.6% | -2.2pp |
| single-session-preference | 50.0% | 60.0% | **+10pp** |
| single-session-user | 94.3% | 92.9% | -1.4pp |

## Lessons Learned

1. **A4 temporal boost 反噬** (-31.5pp on temporal-reasoning): BM25 token 重复加权解决不了"跨 chunk 日期推理"。需要更精细的方案（rerank 模型 or agent multi-step retrieval）。

2. **A1 k=20 边际收益有限**: 87% queries 之前被截断，但 **truncation 不等于 lossiness**——top-10 chunks 经常已经覆盖关键 fact。k=20 把第 11-20 名的 chunks 加进来往往是 noise，会让 LLM 推理分心。**Longmemeval 涨是因为 query 多需要全 context，personamem 平是因为 k=10 已经够**。

3. **anti-bias prompt 在 M3 上效果有限** (+0.4pp on suggest_new_ideas): M3 在多选题上的 length bias 不显著，prompt intervention 边际效益小。要解决得多管齐下 (option order shuffle + RAG grounding + constrained decoding)。

4. **每个改动都要单独 ablation 验证**: 我一上来把 A1+A2+A4+C1 都 ship，结果 longmemeval 退步。要先单改单跑，确认 ROI 再叠加。

## Ship 状态

### Astor v1.3.0 ✅ LIVE
- **B1 event_date** + **B3 outcome_boost**: server 跑着 v1.2.7 版本号但代码是 v1.3.0
- Health 200, 526 facts, live verified

### ABM harness
- ✅ **A1 k=20**: env `AMB_BM25_K=20`, ship
- ✅ **A2 anti-bias prompt**: built-in, ship (微效)
- ✅ **C1 BM25 query LRU cache**: env `AMB_BM25_CACHE=4096`, ship
- ❌ **A4 temporal boost**: REVERTED (longmemeval temporal-reasoning -31.5pp)

### Recommended Final Ship
- Astor v1.3.0 → keep as v1.3.0 in `__init__.py` next time we ship
- ABM harness: A1 + A2 + C1 保留；不 ship A4

## 下一步（你来说优先级）
1. 跑 locomo_v2 (with A4 reverted, k=20, cache) - 长 memeval 涨了 +4pp, locomo 也可能涨
2. 跑 beam_v2 - BEAM/100k 是被截断最严重的（85% capped），可能大涨
3. 用真实 hermes 流量 backtest 验证 astor v1.3.0 event_date 不影响 recall 精度
4. 决定是否 ship astor v1.3.0 版本号
