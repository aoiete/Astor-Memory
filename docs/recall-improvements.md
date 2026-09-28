# Recall Improvements — K/L/M/O Ships (2026-09-28)

**Purpose:** Single source of truth for the four recall-quality ships landed
on 2026-09-28 in response to operator feedback on astor's lifestyle / fortune
mrr weakness (eval baseline 0.898, lifestyle 0.556).

**Eval set:** `tests/eval_set.jsonl` (110 queries, cold-cache, mixed-script).
**Harness:** `tests/eval_runner.py --all` (5 production variants + 4 MMR
ablations + 2 HyDE ablations). Aggregated into `astor/metrics/eval_history.jsonl`.

**Trigger:** WeChat article "HelloAgents 记忆+RAG" walkthrough surfaced
4 concepts we hadn't yet shipped: (1) MQE-derived variants beyond the
synonym dict, (2) MMR diversity rerank to fix rank-collapse on near-dups,
(3) HyDE for short abstract queries, (4) per-fact TTL decay. The first
three had measurable eval impact; #4 was already shipped (ADR-0003,
v1.14.39). See "What we did NOT ship" at the bottom.

---

## Ship K v1.15.31 — Multi-language query expansion

**Problem:** `synonym_expander.py` (v1.10.9) was English-only. Chinese
short queries like `fit 健身 计划`, `八字 壬水身强`, `今日日柱` fanned
out to a single recall route — no synonym substitution, no bi-gram
anchors. The operator's domain (RAG / 八字 / poker / trading) is heavily
CJK, so the English-only expander was effectively dead weight on
Chinese traffic.

**Fix:** Add a 30-entry Chinese synonym dict, CJK bi-gram fallback for
synonym-barren short queries, optional LLM fallback gated by
`ASTOR_LLM_EXPAND` env var. Self-substitution guard (`syn != trigger`,
`syn not in query`) prevents trivial variants like `RAG 知识库 →
RAG RAG bge-reranker`.

**Files:**
- `astor_memory/nest/synonym_expander.py` — added `_CN_SYNONYM_GROUPS`,
  `_match_cn_groups()`, `_cn_bigrams()`, mixed-script dispatch.
- `tests/test_synonym_expander_chinese.py` — 30 unit tests covering CJK
  trigger substitution, bi-gram fallback, mixed-script, English
  backward-compat, self-substitution guard, LLM gate, empty query.

**Measured impact (110-query eval, cold-cache):**
- `hit_rate@10`: unchanged at 0.972 (already saturated).
- `mrr`: unchanged at 0.898 (rank issue isolated to Ship L).
- **Qualitative win:** `RAG 知识库 bge-reranker` now surfaces `fid=9580`
  first hit ("用户对 RAG 文章「RAG 知识库搭建」有兴趣学习...") — was
  pure tier-access miss before Ship K (fact lives in private tier,
  but Ship K's Chinese-aware variant generation pulls it in at the
  public tier too via cross-tier BM25 hits).
- **Why no quantitative win:** lifestyle queries were already matching
  at ranks 1-7; Ship K expands the candidate pool without improving
  rank. That's Ship L's job.

---

## Ship L v1.15.32 — MMR diversity rerank (Carbonell-Goldstein λ=0.7)

**Problem:** Lifestyle eval baseline showed 8 queries matching at
ranks 1-7 instead of rank 1. Root cause: hybrid_merge sorted by score
desc, but the top-scoring facts were near-duplicates ("用户喜欢德州"
/ "用户偏好德州" / "用户常玩德州") of each other. The canonical answer
got bumped to 4-7 because the near-dups outscored it on raw
similarity+BM25.

**Fix:** Carbonell-Goldstein MMR applied after `hybrid_merge`. Greedy
selection trades λ× score for (1-λ)× max-Jaccard-to-selected. Token
Jaccard on fact content (each CJK char = own token, matches
`lex_index._tokenize`) for diversity — no embedding cost, no LLM
cost. λ=0.7 default (Carbonell-Goldstein recommended range).

**Files:**
- `astor_memory/nest/mmr_reranker.py` — new module: `mmr_rerank()`,
  `_tokens()`, `_jaccard()`. Pure-Python, O(k × |pool|).
- `astor_memory/server.py` — wired after `hybrid_merge`, before LLM
  rerank. Triggered only when `len(merged) > top_k` (preserves
  hybrid order on small candidate pools). Gated by `ASTOR_MMR=0`
  env var.
- `tests/test_mmr_reranker.py` — 24 unit tests: empty input, λ=1.0
  fallback, determinism, tie-break by original index, missing
  content, score preservation, CN diversity.

**Measured impact (110-query eval, cold-cache):**

| Category | Pre-Ship (v1.15.30) | Post-Ship (v1.15.32) | Delta |
|---|---|---|---|
| overall mrr | 0.898 | **0.909** | **+0.011** |
| lifestyle mrr | 0.556 | **0.646** | **+0.090** |
| lifestyle hit | 0.80 | **0.90** | **+0.10** |
| fortune mrr | 0.814 | **0.833** | **+0.019** |
| misses | 5 | **4** | -1 (Q60 workout 周训练 now hits) |

---

## Ship M v1.15.33 — MMR λ knob + sweep harness

**Problem:** Ship L hardcoded λ=0.7. Carbonell-Goldstein paper says
λ∈[0.5, 0.9] are all reasonable; we had no operator knob to A/B.

**Fix:** Body field `mmr_lambda` (float, clamped to [0.0, 1.0])
overrides env `ASTOR_MMR_LAMBDA` which overrides
`mmr_rerank.DEFAULT_LAMBDA`. 1.0 = pure relevance (no MMR). Added
`mmr_lambda_05/09/off` variants in eval_runner for sweeps.

**Files:**
- `astor_memory/server.py` — read `mmr_lambda` from body, validate,
  pass to `mmr_rerank()`.
- `tests/eval_runner.py` — 4 new variants + choices.

**Measured impact:** λ∈[0.5, 0.9] is statistically indistinguishable
on the 110-query eval set. Lifestyle mrr at λ=0.7 (0.646) ≈ λ=0.5
(0.646) ≈ λ=0.9 (0.646). **Default 0.7 retained** — Carbonell-Goldstein
rec + matches ship L's measured baseline. **The MMR-on vs MMR-off
gap is the real win; λ fine-tuning is secondary.**

---

## Ship O v1.15.34 — HyDE for short abstract queries

**Problem:** Very short queries (`健身`, `今日日柱`, `用户当前时区`)
live in a different embedding region than the target facts. Pure
cosine misses. MQE gives surface-level variants; MMR rearranges
candidates; neither changes what the query embedding is.

**Fix:** HyDE (Hypothetical Document Embeddings, Gao et al 2022).
For short queries (<8 tokens), ask the LLM "what would the answer
look like?", embed the hypothetical answer, search THAT embedding,
merge into primary vector_hits at weight 0.5. Cached via lru_cache(512).
Default model: `minimax/minimax-m2` (cheap, throughput-prioritized).
Override via `ASTOR_HYDE_MODEL`.

**Files:**
- `astor_memory/nest/hyde.py` — new module: `hypothetical_answer()`,
  `merge_hyde_hits()`, `_is_short()`, `_call_hyde_llm()`. Failure
  paths return empty (never a regression).
- `astor_memory/server.py` — wired after primary `vector_hits`,
  gated by `ASTOR_HYDE=1` env var (default OFF — operator opt-in
  per environment for cost control).
- `tests/test_hyde.py` — 22 unit tests: short-query detection, gate
  logic, merge dedup + max-score, cache, weight semantics, failure
  paths.

**Measured impact:** HyDE path is opt-in by design — default off
because every short-query recall pays an LLM call. Quantitative
benchmarking deferred to a separate session where operator opts in
and we measure cold-cache vs warm-cache deltas per short-query.

---

## What we did NOT ship (and why)

### Working memory TTL (originally Ship N)

The WeChat article describes 4 memory layers: working / episodic /
semantic / perceptual. astor already ships episodic (`memory_bus`
event log) + semantic (`memory_canonical` long-term). The "working
memory" the article describes is **LLM session context** — the
rolling conversation buffer that the model sees every turn. astor
isn't in the LLM session buffer business; that's the model's
responsibility. Adding a TTL layer inside astor would duplicate state
without clear benefit. **Skipped.**

### Per-fact TTL decay (article's "艾宾浩斯")

Already shipped: `ADR-0003` v1.14.39. Default-on decay sweep
(`ASTOR_DECAY_SWEEP`): 30d no-recall → halve `access_count`, 90d
no-recall → tombstone. Reversible via `ASTOR_DECAY_SWEEP=0`.
**Skipped** — already shipped.

---

## Eval-set lineage

| Date | Eval set hash | Notes |
|---|---|---|
| 2026-09-22 | `0974aef8f62f93e9` | 110 queries, 5 categories |
| 2026-09-23 | `0974aef8f62f93e9` | Same hash — no tampering |
| 2026-09-28 | `0974aef8f62f93e9` | Same hash — K/L/M/O all use this |

Trend detection: `tests/eval_trend.py --window=10`. Snapshot file
`astor/metrics/last_good_snapshot.json` updates on median improvement
(never snaps down).

---

## Operator recipes

### "Turn MMR off (revert Ship L)"

```bash
export ASTOR_MMR=0
# then restart server (or set in start_astor.bat)
```

### "Try λ=0.5 in production"

```bash
export ASTOR_MMR_LAMBDA=0.5
# restart server, or pass per-call via /v1/read body: {"mmr_lambda": 0.5}
```

### "Opt in to HyDE for short queries"

```bash
export ASTOR_HYDE=1
export ASTOR_HYDE_MODEL=minimax/minimax-m2  # default; override if you want different
# Or per-call: /v1/read body: {"hyde": true}
```

### "Sweep MMR λ on current eval set"

```bash
cd /d/AI/astor-memory
D:/AI/PY-314-ml/Scripts/python.exe tests/eval_runner.py --all
# inspect astor/metrics/eval_history.jsonl for per-variant mrr
```

### "Sweep Chinese expander effectiveness"

```bash
# Add lifestyle + fortune queries in Chinese to tests/eval_set.jsonl
# (already mostly CN; ship K tests cover CJK variants)
D:/AI/PY-314-ml/Scripts/python.exe tests/test_synonym_expander_chinese.py
```

---

## Open follow-ups (next session)

- **P (deferred):** HyDE end-to-end eval. Requires operator opt-in
  (`ASTOR_HYDE=1` + `OPENAI_API_KEY`/`OPENROUTER_API_KEY` in
  server env). Measure cold-cache vs warm-cache deltas on the
  short-query subset of `eval_set.jsonl`.
- **R:** Per-query MMR λ — adapt λ by query intent (multi-hop
  queries might prefer λ=0.9, single-hop factual might prefer
  λ=0.5). Ship as `body['mmr_lambda']` with auto-default.
- **S:** capture_intent hook — auto-detect when user reports a
  recall miss ("完全不对" / "你自己看") and emit a `recall_miss`
  fact for next-time tuning.
- **T:** Embedding-model A/B — try `BAAI/bge-m3` (multilingual,
  8K context) as a replacement for e5-large. Could improve Chinese
  query embedding quality without LLM cost.