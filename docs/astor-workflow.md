# Astor Workflow — Write/Read/Analyze/Rank/Highlight

> **Shipped**: 2026-08-19 (v1.2.7)
> **Status**: astor-extract-facts accepts `why` + `outcome` params
> **Audience**: agent authors + ops

---

## The 5-stage workflow

astor is **not** just read/write. It's a pipeline that captures intent, classifies outcome, audits calls, ranks results, and highlights key facts.

```
┌──────────────────────────────────────────────────────────────┐
│ STAGE 1: WRITE (capture)                                    │
│   astor_extract_facts(text, mode, why, outcome)            │
│   - regex OR llm                                           │
│   - capture_intent detection (boost confidence)            │
│   - success/error capture + audit log                      │
└──────────────────────────────────────────────────────────────┘
        ↓
┌──────────────────────────────────────────────────────────────┐
│ STAGE 2: READ (retrieve)                                     │
│   /v1/read (hybrid: vector + BM25 + jaccard)                │
│   nest.search() → bus.memory_canonical lookup              │
│   importance filter (0.65+)                                │
│   + clean_recall.py (test marker filter)                    │
└──────────────────────────────────────────────────────────────┘
        ↓
┌──────────────────────────────────────────────────────────────┐
│ STAGE 3: ANALYZE (judge)                                     │
│   llm_call_log: success/error/latency audit                  │
│   extraction_cache: dedup by content hash                    │
│   schema_version: migration history                          │
│   audit db: first_admin actions                              │
└──────────────────────────────────────────────────────────────┘
        ↓
┌──────────────────────────────────────────────────────────────┐
│ STAGE 4: RANK (priority)                                     │
│   hybrid_merge: vector (0.6) + BM25 (0.4) + jaccard         │
│   importance sorting                                         │
│   outcome boost: success > neutral > error                  │
└──────────────────────────────────────────────────────────────┘
        ↓
┌──────────────────────────────────────────────────────────────┐
│ STAGE 5: HIGHLIGHT (focus)                                    │
│   importance field (0-1)                                     │
│   kind field (user_preference > decision > ...)             │
│   confidence field (LLM or capture_intent boost)             │
│   outcome tag (success/error) for ranking                    │
└──────────────────────────────────────────────────────────────┘
```

---

## STAGE 1 — Write (capture)

### astor_extract_facts signature (v1.2.7+)

```python
astor_extract_facts(
    text: str,
    mode: Literal['auto', 'none', 'regex', 'llm'] = 'auto',
    *,
    tier: str = 'public',
    user_id: str | None = None,
    actor: str = 'system',
    why: str | None = None,              # NEW in v1.2.7
    outcome: Literal['success', 'error', 'neutral'] = 'neutral',  # NEW
) -> list[AstorFact]
```

### why parameter — distinguishes "do X" from "avoid X"

- `why='success_recipe_for_X'` — write a lesson about something that worked
- `why='error_pattern_documented_Y'` — write a lesson about something that didn't work
- Free-form string, recorded in `context` field + `reason` field of llm_call_log

**Rule**: Always set `why` when writing facts that may be recalled later. Without `why`, recall cannot distinguish "do X" from "avoid X" — see bus-mem-1042 (id=1373).

### outcome parameter — drives recall ranking

- `'success'` — do X. Recipe, best practice, working approach. Tag: `outcome:success`
- `'error'` — avoid X. Anti-pattern, mistake, don't repeat. Tag: `outcome:error`
- `'neutral'` — factual context, neither recipe nor warning. Tag: omitted (default)

**Downstream effect**: recall can boost `outcome:success` facts when user asks "how do I..." and suppress `outcome:error` facts when user asks similar question.

### Example

```python
# Success recipe
facts = astor_extract_facts(
    'Always use --why flag when logging lessons to astor',
    mode='regex',
    why='success_recipe_for_bus_mem_1042',
    outcome='success'
)

# Error pattern
facts = astor_extract_facts(
    'When --why missing, recall ranking degrades',
    mode='regex',
    why='error_pattern_documented_2026-08-19',
    outcome='error'
)
```

Result:
- `tags`: `['fact', 'auto_extracted', 'outcome:success']`
- `context`: `[why] success_recipe_for_bus_mem_1042\n<original text>`
- llm_call_log `reason`: `success_recipe_for_bus_mem_1042`

---

## STAGE 2 — Read (retrieve)

### Hybrid recall

```python
# Default config:
#   bm25_weight=0.4, vec_weight=0.6
#   hybrid=True (combined scoring)

# Pure vector (legacy)
results = nest.search(query_emb, limit=top_k)

# Hybrid (recommended)
merged = hybrid_merge(
    bm25_hits=lex.bm25_search(query),
    vector_hits=nest.search(query_emb),
    bm25_weight=0.4,
    vec_weight=0.6,
    keyword_hits=...,
    query_keywords=...,
)
```

### Filter pipeline (client-side)

```python
from clean_recall import clean_recall
result = clean_recall("user coffee preference", top_k=5)
# Returns dict with:
#   - count: int (after filter)
#   - total_before_filter: int
#   - filtered: int
#   - results: list of clean facts (no test markers)
```

Filters applied:
1. Skip facts with test markers (`test`, `e2e_test`, `forgettable`, `marker`, etc.)
2. Skip facts with `importance < 0.65`
3. Skip facts with `kind='system_event'` (test events)

### Server endpoint

```bash
# POST /v1/read
curl -X POST http://127.0.0.1:7803/v1/read \
  -H "Content-Type: application/json" \
  -d '{"query": "coffee preference", "top_k": 5, "tier": "public"}'
```

Returns:
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

## STAGE 3 — Analyze (judge)

### llm_call_log schema

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
    input_hash TEXT NOT NULL,       -- sha256, never reveals content
    input_length INTEGER,
    output_json TEXT,
    success INTEGER NOT NULL DEFAULT 0,
    error_msg TEXT,
    latency_ms INTEGER,
    reason TEXT                     -- populated from `why` parameter (v1.2.7)
);
```

### Audit queries

```python
# Find all errors today
astor_admin_audit(action='extract', since='2026-08-19T00:00:00Z', limit=20)

# Latency stats per provider
SELECT provider, AVG(latency_ms), COUNT(*), SUM(success)
FROM llm_call_log
WHERE ts > '2026-08-19'
GROUP BY provider
```

---

## STAGE 4 — Rank (priority)

### Default hybrid weights

- vector: 0.6 (semantic similarity)
- BM25: 0.4 (lexical exact-match)
- keyword jaccard: implicit boost (token overlap)

### Outcome boost (proposed v1.2.8)

When ranking results, apply outcome tag weighting:

```
score = (vector_score * 0.6 + bm25_score * 0.4) * outcome_weight

outcome_weight:
  outcome:success → 1.5x boost  (recall "how do I" prefers these)
  outcome:neutral  → 1.0x        (default)
  outcome:error    → 0.3x suppress (recall "should I do X" deprioritizes these)
```

This is **planned for v1.2.8** — current ranking doesn't yet use outcome.

---

## STAGE 5 — Highlight (focus)

### System prompt injection

astor plugin's `prefetch()` returns top-5 facts in this format:

```
[astor-memory recall · top 5]
- (id=3, tier=public) 用户偏好小杯黑咖啡,不糖不奶
- (id=11, tier=public) 用户偏好 aggressive investment
- ...
```

Hermes injects this into system prompt before each turn, so the agent sees relevant context.

### Future enhancement (v1.2.8)

Add outcome to recall output:

```
[astor-memory recall · top 5]
- (id=3, tier=public, outcome:success) 用户偏好小杯黑咖啡,不糖不奶
```

This makes the agent aware which facts are "do X" vs "avoid X".

---

## Operational checklist

### Daily / per-write

- [ ] All `astor_extract_facts` calls include `why` parameter (if fact may be recalled)
- [ ] All `astor_extract_facts` calls set `outcome` (success/error/neutral)
- [ ] Audit log checked: `astor_status` tool

### Weekly

- [ ] Run `backfill_embeddings.py` if new facts > 200 without embeddings
- [ ] Audit `llm_call_log` for success/error rates
- [ ] Update release notes if workflow changed

### Quarterly

- [ ] Review recall precision (top-5 accuracy on test queries)
- [ ] Apply scenario clustering v2 to grouped facts
- [ ] Verify 9-db schema integrity (`astor_verify_forge_schema`)

---

## Related docs

- `docs/fact-lifecycle.md` — 4-tier architecture + write rules
- `docs/scenario-layered-recall.md` — L2 clustering workflow
- `docs/bot-stop-semantics.md` — per-user preference injection (planned)
- `astor_memory/forge/extractor.py` — source code (v1.2.7)
- `scripts/clean_recall.py` — client-side filter
- `scripts/backfill_embeddings.py` — embeddings maintenance

---

## Ship log

- **v1.2.7 (2026-08-19)**: `why` + `outcome` params added to `astor_extract_facts`. Tags include `outcome:<success|error>`, context includes `[why] <reason>`, audit log reason populated.
- **v1.2.6 (2026-08-15)**: `llm_call_log` schema + audit logging added.
- **v1.2.0 (2026-07-23)**: 9-db schema port, hybrid merge, scenario clustering v2.

---

*Astor workflow v1.2.7 — 2026-08-19 ship crew*