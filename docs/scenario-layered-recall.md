# Scenario-Layered Recall (Layer 2) — Astor-Memory

> Companion to `fact-lifecycle.md`. This doc explains **Layer 2 (Scenario Clustering)** in detail — how it works, how to use it, how it fits into the 4-layer architecture.

## What is Layer 2?

Layer 2 sits between Layer 1 (atomic facts) and Layer 3 (user profile). It **groups facts into scenarios** — clusters of related facts that share a topic, project, or recurring pattern.

Without L2, recall returns a flat list of facts. With L2, recall returns:
- **Top-N scenarios** (relevance × decay × importance × access_count)
- **Per-scenario fact drill-down** (the actual facts within each scenario)

This is what `recall_3store.py` calls the `<active_scenarios>` block.

## Why L2 matters

Without L2, you have N=730 facts. Recall returns 5-50 of them as a flat list. The user has to mentally reconstruct the patterns.

With L2, you have 30-100 scenarios. Recall returns 3 scenarios with their facts already grouped. The user sees:

```
[Active scenarios, ranked by relevance + decay + importance + access_count]

1. 🔥 weixin token renewal cycle (scenario_id=sc_a1b2c3, score=0.92, 12 facts)
   - fact mem-145: "Tianshu API v18 bind state expires after 30 days"
   - fact mem-289: "Bot binding refresh requires NSSM restart"
   - fact mem-512: "Weixin iLink sendmessage fails when access_token stale"
   - ... (12 total)

2. 📊 portfolio rebalance Q2 (scenario_id=sc_e4f5g6, score=0.78, 8 facts)
   - fact mem-156: "TFSA account over-allocated in NVDA"
   - ...

3. 💤 historical context (scenario_id=sc_h7i8j9, score=0.31, 22 facts)
   - facts > 90 days old, demoted
```

**Pattern visibility** — the user/agent immediately sees "weixin token" is a recurring theme, not 12 separate facts.

## How it works internally

### Storage (`scenarios.db`)

```sql
CREATE TABLE scenarios (
    scenario_id TEXT PRIMARY KEY,    -- md5(prefix), stable
    label TEXT NOT NULL,             -- first 60 chars of seed fact
    keywords TEXT,                   -- JSON array of top-100 tokens
    fact_ids TEXT,                   -- JSON array of bus IDs
    importance REAL DEFAULT 0.5,    -- 0-1, decay-slower when high
    created_at REAL,
    updated_at REAL,
    last_accessed REAL,
    access_count INTEGER DEFAULT 0,
    ttl_days INTEGER DEFAULT 30      -- scenario-level TTL
);

CREATE TABLE scenario_links (
    scenario_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    fact_source TEXT,                -- 'bus' or 'memu'
    added_at REAL,
    PRIMARY KEY (scenario_id, fact_id)
);
```

### Clustering algorithm (`scenario_clustering.py cluster`)

Greedy keyword-based clustering (no LLM cost):

1. Pull recent facts from `memory_canonical` (default 7 days, 500 fact limit)
2. For each fact, build keyword set from `summary + content + tags` (top 50 unique tokens)
3. Score against existing scenarios using:
   - `kw_overlap` = |fact_kws ∩ scenario_kws| / |scenario_kws| (60% weight)
   - `jaccard` = |A ∩ B| / |A ∪ B| (40% weight)
4. If `score >= 0.12` (threshold), assign to existing scenario
5. Else create new scenario with `md5(text[:200])` as ID

**Why greedy + cheap**: 500 facts × 30 scenarios = 15,000 comparisons per cluster run. Runs in <2 seconds on a laptop. No LLM cost.

### Recall flow (`scenario_clustering.py hydrate`)

Given a query string, return top-N scenarios:

1. Load all scenarios (limit 100, ordered by updated_at DESC)
2. Score each scenario against query:
   - `kw_overlap` × 0.7 + `jaccard` × 0.3 = relevance
3. Apply decay: `decay = max(0.1, 1.0 - days_old / 30.0)`
4. Composite score: `relevance × decay × (0.5 + 0.5 × importance) × (1 + 0.1 × access_count)`
5. Mark top-N as accessed (updates `last_accessed`, increments `access_count`)
6. Drill down: fetch fact details from `memory_canonical` for each scenario

### CLI usage

```bash
# Cluster recent facts (last 7 days)
python scenario_clustering.py cluster --since 7d

# Hydrate scenarios for a query
python scenario_clustering.py hydrate --query "weixin token" --top 3

# Check status
python scenario_clustering.py status
```

## Integration with `recall_3store.py`

The session_start routine in `recall_3store.py` calls:

```python
from scenario_clustering import hydrate, status

# Status: 198 scenarios, 1247 fact links
scen = status()
print(f"[L2] {scen['total_scenarios']} scenarios, {scen['total_fact_links']} fact links")

# For each query this session, pre-stage active scenarios
active_scenarios = hydrate(query=session_query, top=3)
```

Output rendered as `<active_scenarios>` block in the system prompt.

## When to use L2 vs L1

| Recency of facts | Use |
|---|---|
| Recent (0-7 days) | L1 (bus) is enough — facts are still fresh in context |
| Older (7-30 days) | **L2 (scenarios)** — patterns emerge, helps recall |
| Ancient (>30 days) | L3 (memu) profile — long-term user pattern |

**Layer 2 shines when**: you have similar topics winding back across multiple sessions, and you want to see the running history rather than just today's facts.

## TTL and decay

| Scenario | ttl_days | What happens at expiry |
|---|---|---|
| Default (importance=0.5) | 30 | Demote to `mempalace` archive |
| High importance (≥0.7) | 90 | Demote to mempalace archive |
| High access_count (≥10) | Extended | `ttl_days *= 1 + log10(access_count)` |

**No auto-delete** — scenarios only get demoted to cold storage. The user can manually re-promote.

## Tuning

You can adjust `scenario_clustering.py`:

| Parameter | Default | What to change |
|---|---|---|
| `jaccard_threshold` | 0.12 | Lower = more facts merge into fewer scenarios; higher = more isolated scenarios |
| `max_scenarios` | 30 | Cap on scenarios per cluster run |
| `since_days` | 7 | How far back to pull facts |
| `ttl_days` | 30 | How long before scenario gets demoted |

## Performance

**Cluster run** (500 facts, 30 scenarios):
- Time: ~1.5s on laptop
- DB writes: 500 INSERT OR IGNORE (scenario_links) + 30 UPDATE (scenarios)
- No LLM call

**Hydrate** (query, top 3):
- Time: ~200ms (SQLite SELECT + Python scoring)
- DB writes: 3 UPDATE (last_accessed, access_count)

Fits in cron job budget.

## Where it lives

- **Source**: `~/.astor-memory (source)\scripts\scenario_clustering.py` (12.8 KB)
- **Runtime**: `$ASTOR_DIR (runtime)\scripts\scenario_clustering.py` (synced via `astor_dev_watch.py`)
- **Storage**: `$MEMORY_BUS_DIR (legacy, pre-2026-07-14)\scenarios.db` (was created 2026-07-31, persists across sessions)
- **Caller**: `recall_3store.py` session_start routine
- **Wiki reference**: `D:/AI/wiki/entities/01-tencentdb-agent-memory.md`

## Limitations & known issues

1. **Greedy clustering** — non-deterministic for ambiguous facts (a fact touching 2 topics gets assigned to whichever scores higher first)
2. **English-only keywords** — Chinese / mixed-language facts may cluster poorly
3. **No LLM on cluster** — purely keyword — nuanced topics (e.g., "weixin" vs "wechat") may not merge
4. **TTL is hardcoded** — not configurable per scenario type

If these become problems, options:
- Layer LLM-based clustering on top (slow, expensive)
- Add per-scenario type metadata
- Periodic re-clustering with LLM as cleanup

## PR / change workflow

1. **Source of truth**: `~/.astor-memory (source)\docs\scenario-layered-recall.md` (this file) + `scripts/scenario_clustering.py`
2. **Runtime copy**: `$ASTOR_DIR (runtime)\docs\` + `scripts\` (synced via `astor_dev_watch.py`)
3. **Change process**: edit source → file watcher auto-syncs → restart server (only if you edited the script)
4. **Backwards compat**: scenario_clustering.py arg API is stable since 2026-07-31

## Change history

- 2026-07-31: initial ship (`scenario_clustering.py`)
- 2026-08-19: moved to canonical source path, synced to runtime, documented here
