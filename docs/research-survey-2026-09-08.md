# Memory-System Survey (2026-09-08)

Reference document for Astor-Memory architecture decisions, kept as a hook
for future ship work. Not load-bearing — delete if it goes stale.

## Scope

Surveyed **akitaonrails/ai-memory** (5.9k★, MIT, Rust) on 2026-09-08 at
the request of the project operator. Other adjacent projects (basic-memory,
agentmemory, cognee, mem0, PowerMem) are already covered in
[`ACKNOWLEDGEMENTS.md`](../ACKNOWLEDGEMENTS.md) and [`ARCHITECTURE.md`](./architecture.md).

## ai-memory at a glance

- Single Rust binary + FTS5 + SQLite. Docker image + Arch AUR package.
- **Markdown wiki on disk** is the source of truth; the SQLite index is
  *derived* and can always be rebuilt from files (`grep`, `rsync`, Obsidian).
- **20+ agent harnesses** wired via MCP / lifecycle hooks (Claude Code,
  Codex, Cursor, Gemini CLI, OpenCode, Grok, Devin, Kimi, Kiro, Hermes
  Agent as "Community", etc.).
- **Handoff protocol** — typed, owned, claimed exactly once. The README
  draws an explicit line: "Handoffs are a protocol here, not a
  convention."
- **Audit log** of every mutation. Measured write ceiling ~700/s.
- **Zero-LLM mode** — capture / search / handoff all run without any
  API key (FTS5 only).
- Influences they cite: Karpathy LLM Wiki, agentmemory, basic-memory,
  cognee, Hermes Agent, A-MEM.

## Architecture comparison

| | **ai-memory** | **Astor-Memory** |
|---|---|---|
| Form factor | One Rust binary + Docker | pip-installable Python package (`am` CLI + FastAPI server) |
| Source of truth | Git-backed markdown wiki | SQLite (9-db layout: 3 stores × 3 tiers + per-user privates) |
| Search | FTS5 + optional vector rerank | FTS5 BM25 + multilingual-e5-large embeddings + hybrid merge |
| LLM dependency | Optional (zero-LLM mode = default for solo work) | Optional (auto_relax + grounding audit use LLM; recall works without) |
| Cross-agent | 20+ harnesses via MCP | Hermes-only (designed for one bot operator stack) |
| Multi-user | auth + per-person attribution + audit log | ACL matrix at the store level + bot-binding + per-user privates |
| Cross-machine | One server, many clients (homelab pattern) | One server, many clients (central-server pattern, `ASTOR_DIR` isolated) |
| Handoff | First-class, typed, claim-once | Implicit (same agent resumes; cross-agent via shared bus) |
| Lifecycle | Hook-based capture + session-end consolidation | Capture hook + forge extractor + nest promotion + decay/merge |
| Audit | Per-mutation log, measured write ceiling | Grounding audit + R-class enforcement + per-request bus tracking |

## What we want to borrow

1. **"Plain markdown as a co-equal SSoT" framing.**
   ai-memory markets hard on "your memory is plain markdown; grep it,
   open it in Obsidian, edit it by hand." Astor could grow a similar
   export-from-bus path that lets an operator dump facts to a wiki page
   and trust it as a 1:1 mirror. *Not implemented — kept as a hook.*

2. **Zero-LLM mode as a headline feature.**
   Their quickstart runs without any API key. Our `astor_recall`
   hybrid already degrades gracefully without an embedding model
   (BM25 + lex-only path), but we do not advertise it. **Now
   documented in README** (v1.14.4+).

3. **Typed cross-fact edges.**
   ai-memory uses `typed-edges.md` to encode relations like `fixes`
   and `contradicts`. We already have `tags` JSON on every fact —
   a future helper could write `["edge:fixes", "edge:causes"]` to
   encode the same semantic without a schema migration. *Not
   implemented — kept as a hook.*

## What we explicitly did NOT copy

- **Single-binary Docker deployment.** Operator runs a Python stack
  on Windows; Docker is overhead we do not need.
- **20+ harness cross-compat.** Astor is intentionally scoped to one
  operator's stack (Hermes + Discord/Telegram/WeChat). Trying to
  serve Claude Code / Codex / Cursor would dilute the project.
- **"Compile-not-retrieve" (Karpathy-style LLM Wiki).** We use
  hybrid retrieve (BM25 + embedding rerank). Compile-only loses the
  citation-first property we ship with every recall result.
- **Markdown files as primary SSoT.** Bus SQLite is operationally
  faster for the 9-db layout; markdown would force per-user
  directory munging for private tiers.

## Status (2026-09-08)

- README updated with the survey entry + zero-LLM feature row.
- This file is the durable reference. If ai-memory publishes a
  breaking API change or new release that affects our position,
  update this file rather than scattering notes elsewhere.
- Triage criteria for re-survey: ai-memory release with **typed-edges
  v1** going GA, or a new project crossing 1k★ in the agent-memory
  topic that materially differs on multi-user / cross-machine axes.
