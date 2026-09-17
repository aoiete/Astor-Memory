# 0004. L1/L2 multi-granularity recall

- Status: accepted
- Date: 2026-09-16
- Deciders: admin (first_admin), astor-memory maintainers
- Source: MemU ADR 0007 (2026-07-23) L1/L2 invert, RippleMem v1.14.21
  multi-hop eval verdict, internal observation that similar facts in
  the same session were competing for the same recall slot.

## Context and Problem Statement

astor-memory's `/v1/read` is single-granularity: each fact has its own
embedding, and recall is a single cosine similarity lookup. Empirically
this leads to a problem:

- A user asks "what did we decide about X?" → recall returns 3 facts
  from session S1, 2 from session S2, 1 from S3.
- All 6 are about X, but only 2 of them are the *actual decision* — the
  other 4 are context-setting facts from the same sessions.
- The decision fact loses the recall slot to its context, because they
  have similar embeddings.

MemU's ADR 0007 (2026-07-23) inverted L1/L2: **L1 = coarse doc/cluster,
L2 = item slices**. The cluster-level summary vector first narrows the
search space; the fact-level vector then ranks within. We adopt this
direction.

## Considered Options

- **A — Keep single-granularity (fact-level only)**
- **B — L1/L2 multi-granularity** (cluster summary → fact ranking)
- **C — Hierarchical clustering** (multi-level L1, deeper fact L3)

## Decision Outcome

Chosen option: **B (L1/L2 multi-granularity)**.

### Why B

- **Solves the same-session competition problem** directly — L1 picks
  the session, L2 picks the fact. No more context-vs-decision collisions.
- **Aligns with MemU ADR 0007** — both frameworks converge on the same
  architecture choice from different starting points.
- **Cheap to implement** — 1 new table (`cluster_embeddings`), 1 rebuild
  method, 1 search method. ~150 LOC. No new external dependency.
- **Reversible** — `ASTOR_MULTIGR_ENABLED=0` falls back to plain fact
  search. Default-on because the corpus is dense single-session.

### Why not C (hierarchical)

- Adds operational complexity (cluster of cluster maintenance).
- We don't have multi-level semantic structure to leverage (no docs /
  chapters / paragraphs).
- Can be added later as L1' → L1 → L2 if needed.

### Implementation

1. **Schema** (v3 → v4): new table `cluster_embeddings`
   ```
   cluster_key TEXT, model_name TEXT,
   embedding BLOB, member_count INT,
   PRIMARY KEY (cluster_key, model_name)
   ```

2. **`rebuild_clusters(cluster_dim='session_id')`** — joins
   `embeddings` to `memory_canonical` via fact_id, groups by
   `cluster_dim` (default: session_id), mean-pools member embeddings,
   writes one row per group. Idempotent. Skips singleton clusters
   (< 2 members).

3. **`search_l1_l2(query_emb, l1_limit=3, l2_limit=10)`** — three steps:
   - L1: cosine against `cluster_embeddings` → top-3 clusters.
   - L2: get winning clusters' fact_ids; cosine against
     `embeddings` restricted to those fact_ids → top-10 facts.
   - Return `[(cluster_key, fact_id, sim)]`.

4. **Server wiring** (deferred to next ship): call `rebuild_clusters()`
   on a weekly cron (or on every N writes) so `cluster_embeddings`
   stays current. Until then, the `/v1/read` endpoint falls back to
   plain search() when `cluster_embeddings` is empty.

## Consequences

### Good

- **Recall precision up** — decisions beat context in their own session.
- **Same hybrid retrieval** — L1/L2 layers *on top of* the existing
  embedding + BM25 path. No conflict with ADR-0002.
- **Cheap** — one new table, no new model.
- **Reversible** — env var kills the path.

### Bad

- **Cluster staleness** — `cluster_embeddings` goes stale as new facts
  arrive. Mitigated by weekly cron + (optional) on-write refresh.
- **Cluster_dim coupling** — currently hardcoded to `session_id`. If
  most queries are not session-scoped, the L1 shortlist misfires.
  Mitigation: ship multiple cluster_dims (session_id + topic) in a
  follow-up.
- **Singleton clusters skipped** — single-fact sessions don't get
  represented. Acceptable: they only have one fact anyway, so plain
  search finds it.

### Risks

- **Mean pooling dilutes signal** — clusters with 50+ facts may have
  a mean vector that doesn't represent any one fact well. Mitigation:
  cap cluster size at top-50 most-recent facts (S-candidate).
- **session_id quality** — depends on callers writing session_id
  consistently. Today it's mostly empty for public-tier facts
  (R-class observation: 0% public facts have session_id). Mitigation:
  group by `topic` instead for public tier, or backfill session_id
  from origin_session_id.

## Related

- ADR-0002 — hybrid retrieval is the L2 path; L1/L2 sits on top.
- `astor_memory/nest/vector_store.py` —
  - `AstorNest.rebuild_clusters()` — compute cluster summary vectors.
  - `AstorNest.search_l1_l2()` — L1 → L2 recall path.
- `astor_memory/nest/schema.py` — schema v3 → v4 adds `cluster_embeddings`.
- `tests/test_l1_l2_recall.py` — 5 tests cover the path.
- `docs/competitive-sheet.md` § 2 "Multi-granularity" row — MemU
  L1/L2 is the source citation.
- v1.14.42 CHANGELOG entry.
