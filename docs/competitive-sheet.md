# Astor vs Memory Frameworks — Comparative Sheet (T-sheet)

**Purpose:** Side-by-side comparison of astor-memory against the leading
agent memory frameworks (MemU, MemPalace, Mem0, Zep, Letta). Use this sheet
whenever the user asks "what's X updating / what can we learn / how do we
differ" — answers should pull from this single source of truth.

**Last reviewed:** 2026-09-16
**Sources:**
- MemU GitHub: <https://github.com/NevaMind-AI/memU> (latest: v2.0.0-beta.0, 2026-07-23)
- MemPalace GitHub: <https://github.com/MemPalace/mempalace> (latest: v3.4.0, 2026-06-06)
- Mem0: <https://github.com/mem0ai/mem0>
- Zep / Graphiti: <https://github.com/getzep/graphiti>
- Letta: <https://github.com/letta-ai/letta>

---

## 1. Core Architecture Decisions

| Dimension | astor-memory (v1.14.x) | MemU (v2.0.0-beta.0) | MemPalace (v3.4.0) | Mem0 (v1.x) | Zep Graphiti |
|---|---|---|---|---|---|
| **Storage** | 9-DB SQLite (3-tier × 3-store) | SQLite + optional vector backend | Pluggable (ChromaDB default; Qdrant, pgvector, sqlite_exact) | Vector DB + metadata DB | Neo4j (graph) + optional vector |
| **Retrieval** | Hybrid (embedding + BM25, R201 lock) | Hybrid (embedding + BM25, ADR 0007) | Living graph + Hebbian/Ebbinghaus | Vector + keyword + LLM rerank | Graph traversal + BM25 |
| **Graph layer** | Not default (R201) | Dropped (ADR 0007) | Built-in (hallways + tunnels) | Optional | First-class |
| **Tier model** | public / source / private (× per-user DBs) | single tier | wings / rooms / halls / tunnels / closets / drawers | user-level | entity-relationship triples |
| **Multi-user ACL** | Built-in (per-user dirs, role gate) | Single user | Single user (palace) | Single user | Single user |
| **Multi-host adapters** | ✅ hermes (ship 2026-08-15, v1.5.0 era — before MemU's #536) | Claude Code, Cursor, OpenClaw, Hermes, WorkBuddy, Codex | Claude Code + Claude.ai + ChatGPT | Mem0 API | Zep SDK |
| **Self-hosted** | Yes, single Python dep tree | Yes | Yes | Yes | Yes |
| **License** | MIT | Apache 2.0 | MIT | Apache 2.0 | Apache 2.0 |

**Key takeaway:** astor's differentiator is **multi-tenant + per-user ACL + 9-DB layout** — neither MemU nor MemPalace ship this. The "drop graph" decision (MemU ADR 0007) **validates astor's R201 lock**: hybrid retrieval beats graph at scale.

---

## 2. Recall / Search Mechanisms

| Dimension | astor | MemU | MemPalace | Mem0 |
|---|---|---|---|---|
| **Default retriever** | embedding (multilingual-e5-large) + BM25 rerank | embedding + BM25 hybrid | semantic (cosine) + graph navigation | vector + LLM rerank |
| **LLM rerank** | Optional (ASTOR_RERANK=1 default) | Optional | Built-in (Haiku rerank → 100% LongMemEval R@5) | Optional (gpt-4o-mini) |
| **Multi-granularity** | fact-level + ✅ L1/L2 cluster (Ship A v1.14.42, ADR-0004) | L1 (coarse doc) + L2 (item slices, ADR 0007 inverted) | drawers + closets + hallways | memory-level |
| **Multilingual cosine** | e5-large default (verified) | e5-large family | embeddinggemma (0.35 → 0.88 cross-lingual) | multilingual-e5 |
| **LongMemEval R@5** | Not yet benchmarked | Not published | **96.6% baseline / 100% with Haiku rerank** | ~85% (LoCoMo) |
| **Time decay** | ✅ enabled (v1.14.39, default) — 30d halve, 90d tombstone | Not explicit | Living-memory dynamics (Hebbian + Ebbinghaus) | Access count only |

**Key takeaway:** MemPalace's living-memory dynamics **directly validate astor's v1.14.39 default-on decay sweep**. MemU's L1/L2 inversion is a future hint for astor's multi-granularity recall.

---

## 3. Ingest & Document Processing

| Dimension | astor | MemU | MemPalace |
|---|---|---|---|
| **Text** | ✅ via forge/extractor | ✅ | ✅ (mine <dir>) |
| **PDF/Office/HTML** | ❌ (text only) | ✅ MarkItDown (v2.0.0) | ✅ `--mode extract` (v3.3.6) |
| **Conversation transcript split** | per-fact write | per-fact write | ✅ `mempalace split` |
| **Office-document mining** | ❌ | ✅ (v2.0.0) | ✅ (v3.3.6) |
| **API-tool call routing** | unified bus | unified bus | ✅ separate `wing_api` |

**Key takeaway:** Both competitors added Office/PDF ingest (2026 H1). Astor has not — low ROI for current admin corpus (text-only), keep as S-candidate.

---

## 4. Multi-Tenancy & ACL

| Dimension | astor | MemU | MemPalace | Mem0 |
|---|---|---|---|---|
| **Single user** | ✅ | ✅ | ✅ | ✅ |
| **Multi-user, same instance** | ✅ **9-db per-user layout** | ❌ (1 instance = 1 user) | ❌ | ❌ |
| **Per-tier ACL** | ✅ public/source/private | ❌ | ❌ | ❌ |
| **Actor binding** | ✅ X-Actor header + body fallback (v1.14.37) | none | none | API key only |
| **Free-tier quota** | ✅ via role gate | ❌ | ❌ | ✅ (cloud tiers) |

**Key takeaway:** **astor is the only multi-tenant system in this comparison.** No other framework ships per-user DB isolation + per-tier ACL at the same time. This is the moat.

---

## 5. Observability / Operations

| Dimension | astor | MemU | MemPalace |
|---|---|---|---|
| **Health endpoint** | ✅ `/v1/health` + `/v1/health/diagnose` (Ship C v1.14.43, ADR-0005: +proxy_hijack_check +db_corruption_check +embedding_version_check) | `memU doctor` CLI | implicit |
| **Recall log** | ✅ `recall_log.jsonl` + `latency_ms` (v1.14.29) | none published | none published |
| **Usage stats** | ✅ weekly aggregator (v1.14.28) | none | none |
| **Decay sweep** | ✅ default-on (v1.14.39) | n/a | living-memory dynamics |
| **Audit log** | ✅ astor_audit.db | none | none |
| **Crash-safe reload** | ✅ vector_store auto-reopen | ✅ (v1.14.3+) | n/a |

**Key takeaway:** Astor ships observability out-of-box. MemU's `doctor` proxy-hijack detector is a future hint for astor diagnostics (e.g. detect local DB corruption).

---

## 6. Public Network / Federation (Future)

| Dimension | astor (designed) | MemU | MemPalace | Mem0 |
|---|---|---|---|---|
| **Peer gossip** | ✅ designed v1.15.x (fact 12611) | ❌ | ❌ | ❌ (cloud-only) |
| **Quarantine + trust** | ✅ designed (3-layer defense, fact 12610) | ❌ | ❌ | ❌ |
| **Content filter for spam** | ✅ designed (PII + CVE + keywords) | ❌ | ❌ | ❌ |
| **Dashboard peer view** | ✅ designed | n/a | n/a | n/a |

**Key takeaway:** Astor's planned peer network is **unique in this comparison**. No competitor ships BT-like gossip with 3-layer spam defense. Will be the first framework to ship opt-in public federation.

---

## Lessons Learned — ADR-linked

Every lesson with a `Locked into` clause points to an Architecture
Decision Record under `docs/adr/`. Use ADR-NNNN short form when
referring in chat or commit messages.

| From | Lesson | ADR / Impact |
|---|---|---|
| MemU ADR 0007 | Drop graph, hybrid retrieval wins | ADR-0002 (R201 layer-selection lock) |
| MemPalace v3.3.6 | Living-memory dynamics validate access_count decay | ADR-0003 (v1.14.39 default-on) |
| astor multi-tenant | 9-DB layout gives physical tier isolation | ADR-0001 |
| MemU v2.0.0 | MarkItDown ingest for rich docs | Future S-candidate (low ROI today) |
| MemU v2.0.0 | ADR-driven architecture decisions | ✅ SHIPPED — `docs/adr/` directory (2026-09-16) |
| MemPalace v3.4.0 | Pluggable vector backend | Future S-candidate (only when scale demands) |
| MemPalace v3.4.0 | drawer_id hash collision silently lost data | Astor immune (INTEGER PK, no hash) |
| astor internal | Same-session facts compete for recall slot | ADR-0004 (L1/L2 multi-granularity) |
| MemPalace v3.3.6 | `wing_api` separates tool-call from human-conversation traffic | Future S-candidate: kind-based routing |
| MemU v2.0.0 | Multi-host adapters (Claude Code, Codex, Cursor) | ✅ SHIPPED (hermes_adapter.py, 2026-08-15, predates MemU's #536 by ~12 months) |
| MemU v2.0.0 | `memU doctor` CLI for proxy hijack | ✅ SHIPPED (Ship C v1.14.43, ADR-0005) — diagnose endpoint adds proxy_hijack_check + db_corruption_check + embedding_version_check |

---

## 8. Astor's Unique Defensible Surface

1. **Multi-tenant + 9-DB layout + per-user ACL** — no competitor ships this.
2. **Tier model (public/source/private) + actor binding** — server.py's `_astor_bind_request_acl` is the canonical resolver.
3. **Recall log + usage stats** — out-of-box observability that competitors don't ship.
4. **Hybrid retrieval by default, graph optional** — R201 lock.
5. **Planned peer network** — first opt-in BT-like federation with 3-layer spam defense.

---

## 9. Update cadence

This sheet is reviewed every quarter (or whenever a major framework ships a new version). Trigger phrases to re-run this review:
- "有什么更新 / what's updating / check memu / check mempalace / check mem0 / check zep"
- "看看 X 学到什么 / learn from X"
- "X 比 Y 强吗 / how do we compare to X"

When reviewing:
1. Pull latest release notes from each framework's GitHub releases page.
2. Compare new features against this sheet's 9 dimensions.
3. Update the "Lessons Learned" table with anything that should change astor's direction.
4. Update "Update cadence" timestamp at top.

---

**Maintainer:** Astor-Memory Maintainers
**Status:** Living document. Update as the field evolves.
