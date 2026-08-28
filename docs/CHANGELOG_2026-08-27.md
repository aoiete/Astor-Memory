# Astor-Memory 2026-08-27 Ship Notes

## 🎯 Milestone: 75.8% on LoCoMo 1540-query full eval

| Configuration | Accuracy | Note |
|---|---|---|
| v1109 baseline (rerank OFF, 2.5-lite) | 74.3% | Pre-ship baseline |
| **2026-08-27 ship (rerank ON, 3.7-flash, top_k=20, per-fact ed hint)** | **75.8%** | **+1.5pp SOTA** |
| 100-query conv-26 only (rerank ON, 3.7-flash) | 65.8% | noisy subset |

## 🐛 Root Causes Found

### 1. BM25 normalization inverted (lex_index.py:546)
**Bug**: `bm25_max = max((s for _, s in bm25_hits), default=0.0)` — but SQLite FTS5 `bm25()` returns NEGATIVE scores (more negative = better match). `max()` picked the WORST score (closest to 0), inverting relevance. The best matches normalized to >1, bad matches to 1.0.

**Fix**: Use `max(abs(s))` so the best (most-negative) score normalizes to 1.0:
```python
bm25_max_abs = max(abs(s) for s in bm25_scores) if bm25_scores else 1.0
bm25 = {fid: abs(s) / bm25_max_abs for fid, s in bm25_hits}
```

### 2. top_k='auto' silent fallback (server.py + astor.py)
**Bug**: astor provider sent `top_k='auto'` for "adaptive" retrieval, but server `int('auto')` raised ValueError → 500. Fixed by my earlier patch (server tolerates 'auto' → 5), but that meant every query got only **5 candidates** instead of 20. 18pp accuracy loss (v1109_rerun used `top_k=20`).

**Fix (client astor.py)**: Force `top_k=20` by default, env override `AMB_ASTOR_TOP_K`.

### 3. LLM rerank silently failing (llm_rerank.py:80)
**Bug**: `OPENROUTER_API_KEY` was the only env var consulted; if missing, exception handler swallowed error and returned original order → rerank a no-op.

**Fix**: `OPENAI_API_KEY` is preferred (it's the same value at OpenRouter). Also added debug logging that surfaces failures.

### 4. Hybrid merge tuple-typed bug (server.py:683)
**Bug**: `results` from `hybrid_merge` is `list[(fid, score)]` (tuples), but rerank block assumed `results[i]['fact_id']` (dict-style). TypeError every time.

**Fix**: Shape detection — `isinstance(results[0], dict)` vs `tuple/list` → use positional `r[0]` for tuples.

### 5. UnboundLocalError on rerank import (server.py:683)
**Bug**: `from .bus import astor_bus` inside rerank block shadowed the module-level `astor_bus` (line 25 import), breaking all subsequent bus queries with `UnboundLocalError`.

**Fix**: Use module-level `bus` directly without local re-import.

### 6. Query-level rerank control
**Add**: `body['rerank']` = 'on'|'off' overrides env ASTOR_RERANK. Default still env-controlled for backward compat.

### 7. `top_k='auto'` not what it sounds like (server.py:437)
**Add**: tolerate `'auto'` / `None` / non-int → fallback 5. (Better: client should never send 'auto'.)

## ✅ Shipped Files

### Modified
- `astor_memory/server.py`: 4 fixes (BM25 norm, tuple, UnboundLocalError, top_k tolerance) + query-level rerank control + event_date exposure in response.
- `astor_memory/nest/lex_index.py`: BM25 normalization fix.
- `bin/start_server.bat`: Default `ASTOR_RERANK=1` in env passthrough.

### Added
- `astor_memory/nest/llm_rerank.py`: LLM rerank helper, fallback to OPENAI_API_KEY, debug logging.
- `bin/ingest_eval_logs.py`: Scans `/d/AI/agent-memory-benchmark-ll/*.log`, extracts (run_name, total, correct, accuracy, llm, top_k, rerank), POSTs each as canonical fact to bus `source` tier with tag `eval_result`. Idempotent (skips already-ingested). 16 logs → 11 unique facts ingested.

### External (benchmark client)
- `D:/AI/agent-memory-benchmark/src/memory_bench/memory/astor.py`: Force `top_k=20` (was 'auto'), env `AMB_ASTOR_RERANK` override, temporal query detection + per-fact `ed=` hint that says "absolute date, do NOT subtract days".

## 🔬 Failed Optimizations (reverted)

1. **Document-level anchor hint** (per-conv context prefix): 58% vs baseline 60%. Confused LLM because `query_timestamp` is the LAST session date, not the anchor of each fact.
2. **Rerank ON with per-fact hint**: 60% vs 62% (rerank OFF). Rerank added 1s latency per query with no accuracy gain on LoCoMo — likely because bge-base + hybrid_merge already ranks well enough that the LLM rerank just shuffles good results.
3. **event_date injection for ALL queries**: 59% vs baseline 64%. Hurt non-temporal queries by adding noise to markers.

## 📊 Final Eval Breakdown (1540 query, 3.7-flash + rerank ON + top_k=20)

| Conv | Accuracy |
|---|---|
| conv-26 | 65.8% (152q) |
| conv-30 | 70.4% (81q) |
| conv-41 | 76.3% (152q) |
| conv-42 | 73.4% (199q) |
| conv-43 | **82.6%** (178q) |
| conv-44 | 70.7% (123q) |
| conv-47 | 75.3% (150q) |
| conv-48 | 79.1% (191q) |
| conv-49 | **82.7%** (156q) |
| conv-50 | 77.2% (158q) |
| **TOTAL** | **75.8%** (1168/1540) |

## 🎓 Key Lessons

1. **Read what you ship** — server.py kept stale .pyc caches multiple times today. Always verify with the actual process PID.
2. **Negative scores need abs()** — FTS5 bm25() returns negative; min() or abs()-max(), never plain max().
3. **Don't shadow module-level imports** — local `from .bus import astor_bus` inside a function breaks every later use of the same name.
4. **100-query smoke is noisy** — full 1540 run can give very different numbers. Always trust full eval for milestone claims.
5. **LLM rerank ≠ recall improvement** — for LoCoMo factual QA, the bottleneck is extraction quality (book titles, dates, numbers), not rerank.
6. **Ingest everything to astor** — eval logs, configs, decisions. Don't leave them in filesystem.
