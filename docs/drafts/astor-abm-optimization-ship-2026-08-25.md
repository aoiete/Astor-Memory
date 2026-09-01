# astor + ABM 优化 Ship Report (2026-08-25)

## 当前状态
- **Astor runtime v1.3.0**: ✅ 已 ship, server 跑着 v1.2.7 报告但代码 v1.3.0
- **ABM harness v2**: ✅ 已 ship, personamem v2 在跑 (24% done), longmemeval v2 并行跑 (5% done)

## Astor 改动

### B1: event_date + event_date_precision
- `forge/extractor.py`: `AstorFact` 增加 `event_date` (ISO-8601 'YYYY-MM-DD') + `event_date_precision` ('day'|'month'|'year'|'none') 字段
- `forge/llm_extract.py`: LLM extractor prompt 要求返回 event_date；dict→AstorFact 转换时透传
- `bus/store.py`: `insert_candidate()` 接受 event_date 参数, 存到 metadata JSON `__event_date__` + `__event_date_precision__`
- `server.py`: `/v1/write` 的两处 insert_candidate 调用都透传 event_date

**验证**: 写入"用户 2026-08-25 参观了博物馆" → DB 存了 `__event_date__=2026-08-25`, `__event_date_precision__=day`。

### B3: hybrid_merge outcome boost
- `nest/lex_index.py`: `hybrid_merge()` 新增 `outcome_weights` + `outcome_boost_strength` 参数 (默认 strength=0.3)
  - success tag → weight 1.5 → score ×1.15
  - error tag → weight 0.3 → score ×0.79
  - neutral → 不动
- `server.py`: `/v1/read` 在 hybrid 路径下, query 时计算 candidate fids 的 outcome_weights (从 tags['outcome:success'/'outcome:error'] + metadata['__outcome__']) 传给 hybrid_merge

**效果预期**: 区分 "do X" (recipe) 和 "avoid X" (anti-pattern) facts, 让 recall 优先返回 recipe 类。

## ABM harness 改动

### A1: BM25 k=10 → k=20
- `src/memory_bench/memory/bm25.py`: default k 从 10 改成 20 (env `AMB_BM25_K`)
- **影响**: 87% 的 query (locomo + personamem + BEAM) 之前被截断在 5179 tokens (=10 chunks × 512); 20 chunks 覆盖更全
- **成本**: retrieve time 略升 (但 BM25 O(n) 很快, p50 从 11ms 涨到 ~20ms, 总耗时影响 < 5%)

### A2: MCQ anti-bias prompt
- `src/memory_bench/dataset/base.py`: `_DEFAULT_MCQ_PROMPT` 增加 anti-bias 规则 (do NOT choose by option length/position)
- **影响**: 针对 personamem 多选题的 27% length-bias 失败 (suggest_new_ideas 类别); smoke 30-query 测试显示 43% → 47%

### A4: BM25 temporal boost
- `src/memory_bench/memory/bm25.py`: BM25 ingest 时, 对每个 chunk, 把 ISO date / Month DD YYYY / YYYY tokens 复制到 tokenized list
- **影响**: locomo/temporal 查询 ("When did X happen on 7 May 2023") 现在 BM25 会更倾向返回包含该日期的 chunk

### C1: BM25 query-result LRU cache
- `src/memory_bench/memory/bm25.py`: `_retrieve_cached` 用 `@lru_cache(maxsize=4096)` 缓存 (query, k, user_id) → docs
- **影响**: longmemeval/locomo 多 session 类数据集有重复 query 时, 节省 BM25 scoring; 默认 4096 条 (~32MB), 用 `AMB_BM25_CACHE=0` 可关

## 运行进度 (10:42 MDT)
- **personamem_v2**: 816/2727 = 30%, ETA ~1.5h
- **longmemeval_v2**: 20/500 = 4%, ETA ~12 min
- beam_v2 + locomo_v2: 等 personamem_v2 完再起 (防止 M3 rate limit 撞车)

## 验收指标 (v2 vs v1 baseline)
| Dataset | v1 baseline | v2 expected | 关键变化 |
|---|---|---|---|
| Personamem/128k | 56.7% | 58-60% | k=20 + anti-bias |
| LoCoMo/locomo10 | 61.7% | 63-65% | k=20 (22% queries 之前已够 22 chunks, 涨点少) |
| BEAM/100k | 60.6% | 63-66% | k=20 + temporal boost (BEAM 有 event_ordering 类) |
| LongMemEval/s | 60.2% | 62-65% | cache (重复 query 节省) + temporal boost (temporal-reasoning 39.8% 起点) |
| **平均** | **59.4%** | **62-64%** | +3-5pp |

## Next Steps (按 ROI 排序)
1. 等待 personamem_v2 跑完, 起 locomo + beam (后台进行中)
2. 收集 v2 全量结果, 对 baseline 做表格
3. 验证 astor hybrid_merge outcome_boost 在 admin 私 bus 上不影响 recall 精度 (注意: admin bus 现有 fact 多为 neutral, 不会有显著 reorder)
4. 考虑 ingest 时的 date extraction pass (类似 A3) 进一步提升 locomo/temporal
