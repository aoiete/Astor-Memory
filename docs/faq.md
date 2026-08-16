# FAQ

> Frequently asked questions about Astor-Memory.

If your question isn't here, check [`docs/troubleshooting.md`](./troubleshooting.md) for error-specific help, or [`docs/architecture.md`](./architecture.md) for deep design explanations.

---

## General

### What is Astor-Memory?

A self-owned memory system for AI agents. Three stores (event log + fact extractor + vector store), three tiers (public / source / private × N), pure Python, no vendor lock-in.

### Why not just use RAG?

RAG is one component of memory — the retrieval part. Astor-Memory adds:
- Append-only event log (audit trail)
- LLM fact extraction (raw → structured)
- Revision tracking (no silent overwrites)
- Per-user ACL (isolation)
- Lifecycle evolution (decay, merge, promote)

RAG-only is fine for read-only knowledge bases. For agents that write and evolve their own memory, you need more.

### Why not use Letta / mem0 / PowerContext?

See [`README.md`](../README.md#why-we-built-this) for the full comparison. Short version: each of those forces a trade-off (heavy runtime, cloud coupling, pre-1.0 status, etc.) that we weren't willing to accept.

### Is this production-ready?

Astor-Memory v1.0 is the first public release. We've run a precursor (the `memory-bus` system) in production for 33 ship sessions and ~6 months. v1.0 ships the lessons learned, rewritten from scratch with the lessons absorbed.

For production use: pin the version (`astor-memory==1.0.0`), set up `am doctor` monitoring, and review the [migration guide](./migration.md) if upgrading from `memory-bus`.

---

## Installation

### Python version?

Python 3.10 or higher, lower than 3.14. We test against 3.10, 3.11, 3.12, 3.13.

### Install footprint?

< 50 MB. Compare to:
- chromadb: ~80 MB
- transformers + torch: ~3 GB
- memu.ai SDK + deps: ~200 MB

### Does it work on Windows / macOS / Linux?

Yes. We test on Ubuntu 24.04 (primary CI), macOS 14, and Windows 11. The codebase uses `pathlib.Path` everywhere, no shell-specific assumptions.

### Can I install without admin / sudo?

Yes. Use a venv:

```bash
python -m venv ~/.astor-venv
source ~/.astor-venv/bin/activate  # or Windows equivalent
pip install astor-memory
```

---

## Usage

### Where does Astor-Memory store data?

Default: `~/.astor/` (Unix) or `%USERPROFILE%\.astor\` (Windows).

Override with `ASTOR_HOME=/path/to/dir` env var.

Layout:
```
~/.astor/
├── config.yaml          # user config
├── astor_bus.db         # events + memory_candidates + memory_canonical + audit_log
├── astor_nest.db        # vector embeddings (1 row per fact, model_name indexed)
├── astor_forge.db       # LLM extraction cache (v0.2+ LLM extract)
└── private_<user>.db    # per-user DBs (multi-user mode, v1.1+)
```

### How do I back up Astor-Memory?

Three files are the source of truth:
- `astor_bus.db` — events + canonical facts
- `astor_nest.db` — vector embeddings
- `private_*.db` — per-user DBs (multi-user mode, v1.1+)

Copy them somewhere safe. To restore, put them back in `~/.astor/`.

`astor_forge.db` is a cache and can be deleted without data loss (extraction will re-run on next `write`).

### Can I use multiple LLM providers?

Yes. Configure per-write or globally:

```python
# Global
configure(llm_provider="anthropic")

# Per-write (v1.1+)
write("text", llm_provider="gemini")
```

### Does it work offline?

Partial. The vector store (`nest`) is fully local. The event log (`bus`) is local SQLite. The LLM extractor (`forge`) requires network access to your configured provider.

For fully-offline mode (v1.2+), use `llm_provider="ollama"` with a local model.

---

## Performance

### How fast is read?

~1 ms per query (brute-force NumPy kNN) up to 5 K docs. Linear scaling: 10 K docs ≈ 2 ms, 50 K docs ≈ 10 ms.

For > 100 K docs, HNSW index is deferred to v2.0.

### How fast is write?

~10 ms per write (SQLite insert + fire-and-forget LLM call). Async mode (`write_async`) returns in <1 ms; extraction happens in background.

### How much disk space?

Roughly 1 KB per event (raw) + 5 KB per fact (after extraction + embedding). For 10 K facts, expect ~50 MB. The DB grows linearly; old events can be pruned per TTL policy.

### What about memory leaks?

None known. Astor-Memory uses SQLite for durability (no in-memory unbounded growth) and NumPy for vector ops (garbage-collected normally). Run `am doctor` weekly to confirm event count is not unexpectedly growing.

---

## Migration from memory-bus

See [`docs/migration.md`](./migration.md) for the full 5-step guide. Quick answers:

### Can I run both systems in parallel?

Yes. `am init --parallel` uses ports 7804-7806 (vs legacy 7801-7803). Both run concurrently.

### Will my old cron jobs break?

No. Env vars are aliased (`MEMU_URL` → `ASTOR_FORGE_URL`). Existing cron configs continue to work.

### How long does data migration take?

~10 seconds per 1000 events. For a typical 10 K-event bus, expect ~2 minutes.

### Can I roll back after migration?

Yes. Three-step rollback: stop Astor-Memory, restart legacy services, verify. Data is preserved in `~/.memory-bus/` during parallel-run.

---

## Comparison

### How is this different from CoALA paper's memory taxonomy?

CoALA (arXiv:2309.02427) describes 4 memory types in cognitive architectures:
- Working memory
- Episodic memory
- Semantic memory
- Procedural memory

Astor-Memory implements this taxonomy differently:
- Working memory ≈ `bus` (recent events)
- Episodic memory ≈ `forge` output (extracted facts with timestamps)
- Semantic memory ≈ `nest` (vector-indexed general knowledge)
- Procedural memory ≈ skills (external; scanned via `am skill scan`)

We don't claim to *implement* CoALA — we *aligned* our architecture with it.

### How is this different from Mem-π paper?

Mem-π (Mem-π: Adaptive Memory through Learning When and What to Generate) proposes:
- On-demand memory generation (don't just retrieve, generate guidance)
- Abstain mechanism (71% abstention rate; simple tasks don't need memory)
- Cross-LLM transfer (memory strategy is independent of executor)

Astor-Memory v1.0 ships **Insight 11 (cross-LLM adapter)** from Mem-π. Insights 9 (on-demand generation) and 10 (abstain) deferred to v1.1.

We absorbed Mem-π's insights but built the storage layer differently — they focused on the *generation* policy; we focused on the *storage substrate*.

---

## Contributing

### How do I contribute?

See [`docs/contributing.md`](./contributing.md). Short version: open a GitHub issue first, then a PR.

### What's the code style?

Python 3.10+, type hints everywhere, `pathlib.Path` for paths, ruff for linting, mypy strict mode. 80% test coverage required (P-TEST-80-019).

### What's the release cadence?

4 weekly phases:
- v0.1: external dependency removal
- v0.2: rename + restructure
- v0.3: pip-installable + REST + CI
- v1.0: docs + polish + open-source release

After v1.0, semantic versioning (semver.org). Minor versions every 1-3 months.

---

## Next

- [`docs/architecture.md`](./architecture.md) — deep dive on 3-store × 3-tier
- [`docs/migration.md`](./migration.md) — upgrade from `memory-bus`
- [`docs/agent-adapters.md`](./agent-adapters.md) — MCP / LangChain / REST / Python
- [`docs/troubleshooting.md`](./troubleshooting.md) — common errors and fixes
- [`docs/contributing.md`](./contributing.md) — for contributors
