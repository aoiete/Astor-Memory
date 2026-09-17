# 0002. Hybrid retrieval by default, graph optional (R201 lock)

- Status: accepted
- Date: 2026-09-15
- Deciders: admin (first_admin), astor-memory maintainers
- Source: RippleMem evidence (v1.14.21), MemU ADR 0007 (2026-07-23),
  competitive sheet comparison

## Context and Problem Statement

astor-memory's recall layer can be implemented three ways:

1. **Pure embedding** — cosine similarity over embedding vectors.
2. **Pure graph** — entity-relationship traversal (Zep Graphiti, MemPalace).
3. **Hybrid** — embedding + BM25 + optional LLM rerank.

The choice has real consequences:

- Pure embedding misses exact keyword matches (e.g. user searches for
  "AVGO 404" and the fact body says "AVGO 均价 404" — keyword match
  needed).
- Pure graph (Zep-style) wins on multi-hop queries but loses on single-
  hop dense corpora (verified empirically in RippleMem v1.14.21 M01-M10
  eval: 0% hit_rate lift for our admin corpus, because admin facts are
  dense single-session, not sparse multi-session like LoCoMo).
- Hybrid wins on both — exact keyword recall for short facts + semantic
  embedding for fuzzy matches.

## Considered Options

- **A — Pure embedding** (simpler, but keyword misses)
- **B — Pure graph** (multi-hop wins, single-hop loses, ops overhead)
- **C — Hybrid** (embedding + BM25, optional LLM rerank via `ASTOR_RERANK=1`)
- **D — Hybrid by default, graph available as opt-in via plugin**

## Decision Outcome

Chosen option: **D (hybrid default, graph opt-in)**.

### Why D over C

Hybrid retrieval handles 95% of our recall needs. Graph adds:

- Operational complexity (Neo4j / SQLite-graph hybrid storage).
- Multi-hop queries we don't have yet (admin corpus is single-session).
- Latency cost (graph traversal + embedding lookup).

We **don't ship graph unless there's a multi-hop blocker** — verified
empirically via RippleMem v1.14.21: 10 multi-hop eval queries M01-M10
all returned 0% hit-rate lift on admin corpus.

### Why D over A

Keyword misses hurt user trust. Admin has many facts with numbers /
acronyms ("AVGO 404", "QQQI 540", "BTC 110k") where keyword beats
semantic similarity. BM25 over `content` field fixes this without much
storage cost (SQLite FTS5 already there, see `lex_index.py`).

### Why D over B

Graph is the right call **for some** memory systems (Zep is great for
chatbot customer-support data with rich entity relations). For admin
single-session dense corpus, it's overhead.

### Implementation

```python
# server.py /v1/read (default)
hits = nest.search(emb, limit=20)  # embedding (multilingual-e5-large)
hits += bus.fts_search(query, limit=10)  # BM25 FTS5
hits = rerank_llm(hits, query)  # optional LLM rerank if ASTOR_RERANK=1
```

## Consequences

### Good

- **Recalls exact numbers** — admin's "AVGO 404" finds the right fact.
- **Recalls fuzzy concepts** — "investment thesis" still finds similar
  semantically.
- **No graph ops overhead** — single SQLite FTS5 + embedding index.
- **LLM rerank optional** — `ASTOR_RERANK=0` for fast path, default
  `=1` for quality path. Verified: rerank adds ~80ms but +9.4% hit_rate.

### Bad

- **Two indices to keep in sync** — FTS5 + embedding. Mitigated by
  `promote_candidate` writing both atomically.
- **Recall latency** — embedding + BM25 + LLM rerank = ~250ms p95.
  Within budget for /v1/read.

### Risks

- **If multi-hop becomes a real blocker** — admin's recall fails for
  some complex query class. Mitigation: ship graph as opt-in via
  `ASTOR_GRAPH_ENABLED=1` flag (S-candidate). Re-evaluate quarterly.
- **Embedding model drift** — e5-large today, might change to bge-large
  or similar. Migration script in `scripts/migrate_embeddings.py`.

## Related

- ADR-0003 — decay sweep uses access_count from hybrid recall hits.
- ADR-0004 (proposed) — L1/L2 multi-granularity extends hybrid recall.
- `docs/competitive-sheet.md` § 2 "Recall / Search Mechanisms" —
  validates "drop graph" via MemU ADR 0007.
- RippleMem v1.14.21 Ship F (10 multi-hop eval queries + verdict).
