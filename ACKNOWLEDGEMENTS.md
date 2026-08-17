# Acknowledgements

Astor-Memory stands on the shoulders of giants. We explicitly learned from, depended on, and (in some cases) replaced the following projects.

---

## Table of contents

1. [Open-source projects we deliberately do NOT depend on](#1-open-source-projects-we-deliberately-do-not-depend-on)
2. [Open-source projects we depend on](#2-open-source-projects-we-depend-on)
3. [Projects we learned architecture from](#3-projects-we-learned-architecture-from)
4. [Papers that shaped our design](#4-papers-that-shaped-our-design)
5. [Past Hermes projects (ship history)](#5-past-hermes-projects-ship-history)
6. [Authors](#6-authors)

---

## 1. Open-source projects we deliberately do NOT depend on

These projects inspired our design. We did NOT copy code, but we learned from their architecture and made different choices.

### chromadb (Apache-2.0)

**Inspired**: `astor_memory.nest` (vector store layer)

**What we learned**: vector storage with metadata filtering, similarity search, persistence.

**Why we don't depend on it**:
- 80 MB transitive dependencies
- Separate server process (operational complexity)
- Frequent breaking changes in minor versions

**Our replacement**: SQLite + NumPy brute-force kNN. ~12 MB. Library mode (no daemon). Fast up to 5 K docs.

### memu.ai SDK (proprietary)

**Inspired**: `astor_memory.forge` (LLM extraction layer)

**What we learned**: async LLM extraction of structured facts from raw events; revision tracking.

**Why we don't depend on it**:
- Proprietary, no source available
- Cloud-coupled (no self-host)
- API surface changed 3 times in 12 months

**Our replacement**: cloud-LLM-agnostic extractor using provider abstraction (OpenAI, Anthropic, Gemini, DeepSeek, 智谱, Ollama). Same recall output, any provider.

### transformers + torch (Apache-2.0 + BSD)

**Inspired**: nothing architectural, but informed our decision to avoid local LLM stack.

**What we learned**: powerful but heavy. Local LLM inference requires significant compute.

**Why we don't depend on it**:
- 3 GB install footprint
- CUDA dependency on GPU hosts
- Most use cases don't need local inference (cloud LLM is fast enough)

**Our replacement**: cloud LLM for `forge` (with local Ollama as opt-in escape hatch). `< 50 MB` total install.

---

## 2. Open-source projects we depend on

All under OSI-approved licenses. All maintained. No vendor lock-in (any can be swapped).

| Dependency | License | Purpose | Why this one |
|---|---|---|---|
| **flask** | BSD-3-Clause | HTTP server (optional REST API) | Simple, well-known, MIT-compatible ecosystem |
| **requests** | Apache-2.0 | HTTP client (data pulls, LLM API calls) | De facto standard, no surprises |
| **numpy** | BSD-3-Clause | Vector ops (kNN brute-force) | Industry standard for numerical computing in Python |
| **fastembed** | Apache-2.0 | Local embedding (text → vector) | ONNX-based, no torch dependency, fast cold-start |
| **pydantic** | MIT | Schemas, validation, serialization | Type-safe, ergonomic, no runtime overhead |
| **pytest** | MIT | Testing framework | Industry standard, plugin ecosystem |
| **hatchling** | MIT | Build backend | Modern, standards-compliant pyproject.toml |

These are the only runtime + dev dependencies. See `pyproject.toml` for exact version constraints.

---

## 3. Projects we learned architecture from

Concept borrow only — no code copied. Each contributed one or more design decisions.

### PowerContext / PowerMem (oceanbase/powercontext)

**RFCs absorbed**: 0011, 0014, 0020, 0028, 0050, 0051, 0080

**What we learned**:
- Search ↔ context-pack separation (RFC 0028)
- Lifecycle evolution: decay, merge, promote (RFC 0020)
- Citation-first design with `revision_id` tracking
- Experience ↔ Skill split (RFC 0051)
- External skill governance (RFC 0051)

**Our divergence**: PowerContext is a Chinese-language-first pre-1.0 research project. Astor-Memory ships an English-default, post-1.0 production-ready variant with stricter ACL.

### Letta (formerly MemGPT)

**Repo**: letta-ai/letta

**What we learned**:
- Read-only memory blocks pattern
- Archival memory + recall memory separation
- Tool-call-driven memory access

**Our divergence**: Letta's runtime is heavy (Python + FastAPI + many processes). Astor-Memory ships library mode (in-process) plus optional REST. No daemon required.

### mem0 (mem0ai/mem0)

**Repo**: mem0ai/mem0

**What we learned**:
- 4-level ACL as inspiration for our 3-tier isolation
- Scope tags (user/agent/session-level)
- Async-by-default extraction

**Our divergence**: mem0 is cloud-coupled (proprietary backend). Astor-Memory is fully self-hosted.

### LangChain (langchain-ai/langchain)

**Repo**: langchain-ai/langchain

**What we learned**:
- `BaseMemory` adapter interface (we ship our own adapter in v1.2)
- Tool-call protocol
- Agent executor pattern (referenced, not directly used)

**Our divergence**: LangChain is framework-heavy (imports many transitive deps). Astor-Memory ships minimal surface — users bring their own framework.

---

## 4. Papers that shaped our design

### CoALA: Cognitive Architectures for Language Agents

**arXiv**: 2309.02427

**What we learned**: 4-type memory taxonomy (working, episodic, semantic, procedural). Astor-Memory's `bus/forge/nest/tier` decomposition aligns with this framework.

### Mem-π: Adaptive Memory through Learning When and What to Generate

**arXiv**: (Mem-π paper, 2024)

**What we learned**:
- On-demand generation (don't just retrieve, generate guidance)
- Abstain mechanism (71% abstention rate for simple tasks)
- Cross-LLM transfer: a memory strategy trained on Qwen2.5-7B works as guidance for GPT-5-mini

**Shipped**: Insight 11 (cross-LLM adapter) in v1.0. Insights 9 (on-demand generation) and 10 (abstain) deferred to v1.1.

### Memory in the Age of AI Agents

**arXiv**: 2512.13564

**What we learned**: taxonomy of agent memory types, evaluation methodology for memory systems. Informs our future benchmark suite (deferred to v1.2+).

### Activity Frames: Deterministic Screen-Activity Compilation for Agent Memory and Replay

**arXiv**: 2608.05784 (2026-08-06)

**What we learned**:
- Routine Overhead Ratio R = 60-343× — empirical evidence for memory's ROI
- LLM summary accuracy baseline = 66-80%; structured extraction = 98.4%
- Deterministic pipelines beat LLM-based summarization for stable, auditable memory
- Two-tier architecture (raw capture + compiled frames) is a viable alternative to LLM extraction

**Shipped**: Insight 13 (zero-model event classification) deferred to v1.2+ as a performance optimization layer. The empirical numbers (60-343×, 86×, 98.4% vs 66-80%) are cited in our README "How we compare" section as authoritative validation of revision tracking.

**Why we don't depend on it**: Activity Frames is a research paper; the schema/compiler/eval harness reference [14] isn't yet on GitHub per public search. We adopt the empirical findings, not the implementation.

### Atlaso (atlaso.ai, ProductHunt #4 2026-08-05)

**What we learned**:
- Verdict system: tag every memory as `settled` / `contested` / `thin` based on evidence strength
- Background enrichment: nightly distillation of raw captures into structured, tagged, graded memories
- Ambient Memory (pre-session orientation injection)
- Personal + per-Project memory isolation
- Secrets scrubbing before storage (TLS in transit, encrypted at rest)

**Shipped**: Insight 12 (verdict field) in v1.0. Adds a `verdict TEXT DEFAULT 'settled' CHECK(verdict IN ('settled','contested','thin'))` column to `memory_canonical` table. Conceptually complements our 3-tier ACL (spatial: who sees it) with confidence-grading (qualitative: how sure are we).

**Why we don't depend on it**: Atlaso is closed cloud-sync (proprietary). Free tier 1 device + 1 tool; Pro $10/mo unlocks cross-tool sync; Build $25/mo adds developer API. Our self-hosted MIT-licensed variant ships the verdict pattern without the vendor lock-in.

### Inventory (myinventory.site, ProductHunt 2026-08-03)

**What we learned**:
- Local-only conversation indexing (Cursor / Claude Code / Zed / Codex / Kiro)
- One-time purchase commercial model
- Quick Capture (⌘⇧N) command palette pattern

**Why we don't depend on it**: Inventory indexes *other people's* AI conversation history — useful for users with multi-tool workflows. Astor-Memory's `bus` captures the agent's *own* event stream. Different scope (consumer-side indexer vs producer-side event log). Inventory is also macOS-only. We acknowledge the design pattern (command palette + local index) but our architecture serves a different purpose.

### The 2026 Prompt Engineering Field Guide

**Source**: mirror at https://archiecur.github.io/ai-system-design/advanced-prompting/The_2026_Prompt_Engineering_Field_Guide/

**What we learned**: referenced as adjacent literature on prompt engineering patterns. Does not directly inform memory architecture.

**Why we don't depend on it**: not a memory system reference; tangential to our scope. Listed for completeness because the WeChat article that introduced us to Activity Frames also cited this guide.

---

## 5. Past Hermes projects (ship history)

Astor-Memory is the 4th-generation memory system for the Hermes agent project. The 33 ship sessions (P0-P36 + P42) over 2026-07-02 → 2026-08-13 produced the lessons that became Astor-Memory.

### Key ship sessions

| Session | Date | Output | Astor-Memory absorption |
|---|---|---|---|
| P0-P10 (2026-07-02 → 2026-07-13) | 11 sessions | Initial memory-bus + 4-layer reflex cascade | Layered reflex concept (v1.1+ feature) |
| P23-P29 (2026-08-12) | 7 sessions | 3-tier × 3-store ACL | Direct basis for our 3-tier isolation |
| P35 (2026-08-12) | 1 session | Agent self-pattern memory | Inspired Insight 4 (Experience ↔ Skill, deferred v1.1) |
| P42 (2026-08-13) | 1 session | Cross-user pattern matching | Informed Insight 11 (cross-LLM adapter) |
| 2026-08-13 grill-me session | 1 session | 22 design decisions + 11 absorbed insights | Direct foundation for v1.0 scope |

### Key architectural choices inherited

- **Append-only event log** (P0) → `bus` design
- **Layered cascade (L0 regex → L1 crystal → L2 3-store → L3 LLM)** (P15-P19) → retrieval ranking (Insight 1)
- **Self-evolution lifecycle** (P22) → decay/merge/promote
- **Per-user DB isolation** (P23) → multi-user mode design

The full ship history is preserved in the Hermes agent project for archival purposes.

---

## 6. Authors

### flopworld with AI

**Role**: project lead, all architectural decisions, iron rule definitions

**Background**: Hermes agent user since 2026-07. Operates production multi-platform bot (Discord, Telegram, WeChat, Feishu). Designed the precursor `memory-bus` system that ran 33 ship sessions.

### Hermes Agent (model + tooling)

**Role**: co-architect, code review, integration testing, design synthesis

**Specific contributions**:
- Cross-source synthesis (papers + open-source + own ship history)
- Iron rule taxonomy and disclosure policy
- Docs writing and review

**Specific tooling used during design**:
- grill-me skill (mattpocock/skills) for design discovery
- 3-store memory system (precursor to Astor-Memory)
- session_search for cross-session context

---

## 7. Special thanks

- **mattpocock** for the [grill-me skill](https://github.com/mattpocock/skills) — used heavily during Astor-Memory design
- **The Mem-π paper authors** for the cross-LLM adapter insight
- **The PowerContext team** (oceanbase) for the search ↔ context-pack separation principle
- **The hermes-agent community** for testing early prototypes

---

## 8. License note

Astor-Memory is MIT-licensed. See [`LICENSE`](../LICENSE).

The Acknowledgements above do NOT grant additional rights beyond the MIT license of Astor-Memory itself. Each project's license applies to their own code, not to Astor-Memory's code.

If you believe any part of Astor-Memory improperly incorporates copyrighted material, please open an issue at https://github.com/flopworld/astor-memory/issues.
