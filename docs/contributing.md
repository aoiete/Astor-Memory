# Contributing to Astor-Memory

> For contributors. End users should read [`README.md`](../README.md).

This document is the source of truth for:
- Development setup
- Code style
- Test requirements
- Ship-time iron rules (8 Engineering + 5 Docs engineering)
- Implementation details for runtime rules (style reference, dedup, cron enum)

---

## Table of contents

1. [Quick start](#quick-start)
2. [Development setup](#development-setup)
3. [Code style](#code-style)
4. [Testing](#testing)
5. [Iron rules reference](#iron-rules-reference)
6. [Style reference (P-CONF-003 implementation)](#6-style-reference-p-conf-003-implementation)
7. [Multi-source consult example (P-MULTISRC-002)](#7-multi-source-consult-example-p-multisrc-002)
8. [Cron role-based enforcement (P-CRON-DATA-010)](#8-cron-role-based-enforcement-p-cron-data-010)
9. [Dedup implementation (P-DEDUPE-014)](#9-dedup-implementation-p-dedupe-014)
10. [PR workflow](#pr-workflow)

---

## Quick start

```bash
git clone https://github.com/aoiete/Astor-Memory.git
cd astor-memory
pip install -e .[dev]
pytest tests/
```

If all tests pass, you're set up. See [Development setup](#development-setup) for full details.

---

## Development setup

### Python version

Python 3.10-3.13. We test against 3.10, 3.11, 3.12, 3.13 via GitHub Actions matrix.

### Install with dev dependencies

```bash
pip install -e .[dev]
```

This installs:
- `pytest`, `pytest-cov`, `pytest-asyncio` — testing
- `ruff` — linting
- `mypy` — type checking
- `hatch` — building

### Verify setup

```bash
am --version          # → am 0.1.0.dev0 (astor-memory 0.1.0.dev0)
pytest --version      # → pytest 7.x or higher
ruff --version        # → ruff 0.4.x or higher
mypy --version        # → mypy 1.8.x or higher
```

### Run all checks

```bash
# Lint
ruff check astor_memory/

# Type check
mypy astor_memory/

# Tests with coverage
pytest --cov=astor_memory --cov-report=term-missing

# All in one
make check  # if Makefile present; otherwise run each command
```

---

## Code style

### Python conventions

- **Type hints everywhere** (mypy strict mode enforced)
- **Path operations**: `pathlib.Path` only, never `os.path` strings (P-IO-PATHLIB-023)
- **Imports**: isort-style (enforced by ruff)
- **Line length**: 100 characters
- **Naming**: `snake_case` for functions/variables, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants

### Module docstring convention (P-CITE-LOGS-018)

Every module top docstring must include `# Inspired by: <reference>` for files derived from open-source patterns or papers.

```python
"""
astor_memory.bus — append-only event log.

SQLite + WAL. Time-series.

Inspired by:
  - PowerContext RFC 0020 (lifecycle evolution)
  - CoALA paper §4 (episodic memory)
"""
```

### Function docstrings

Use Google-style docstrings:

```python
def write(text: str, scope: str = "long_term", tier: str = "public") -> FactId:
    """Append a fact to bus; forge extracts in background.

    Args:
        text: The fact text. Will be embedded for vector search.
        scope: short_term | long_term | profile. Default long_term.
        tier: public | source | private. Default public.

    Returns:
        FactId that can be used for citation in recall output.

    Raises:
        MemoryWriteError: If write fails after retry.
    """
```

### Commit messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add MCP server adapter (v1.1)
fix: handle SQLite database lock in concurrent writes
docs: clarify citation-first design in architecture.md
refactor: extract deduplication logic to astor_memory.bus.dedup
test: add coverage for revision tracking edge cases
```

---

## Testing

### Coverage requirement

**80% line coverage required** for all PRs (P-TEST-80-019). The CI check will fail below this.

Check coverage locally:

```bash
pytest --cov=astor_memory --cov-report=term-missing
```

### Test structure

```
tests/
├── unit/
│   ├── test_bus.py
│   ├── test_forge.py
│   ├── test_nest.py
│   ├── test_router.py
│   └── test_acl.py
├── integration/
│   ├── test_write_read_pipeline.py
│   ├── test_lifecycle.py
│   └── test_migration.py
└── conftest.py
```

### Markers

Use markers for test categorization:

```python
import pytest

@pytest.mark.unit
def test_bus_append():
    ...

@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_read_pipeline():
    ...
```

Run subsets:

```bash
pytest -m unit                    # only unit tests
pytest -m "not integration"       # skip slow integration tests
pytest -m "integration and not slow"
```

### Async tests

Use `pytest-asyncio`:

```python
import pytest

@pytest.mark.asyncio
async def test_async_write():
    from astor_memory import write_async
    fid = await write_async("test")
    assert fid is not None
```

---

## Iron rules reference

Astor-Memory ships 5 categories of iron rules. Three categories (Engineering ship-time, Docs engineering, Vendor-neutral, Personal) live in this document. The Core 15 runtime rules are in the source code (`astor_memory/defaults/iron_rules.yaml`) and enforced at runtime.

### Engineering ship-time (8 rules, enforced in CI)

| # | ID | 中文 | 描述 |
|---|---|---|---|
| 30 | P-TEST-80-019 | 80% 测试覆盖 | pytest-cov check; PR fails below 80% line coverage |
| 31 | P-NO-OBFUSC-032 | 不混淆代码 | No minification, no opaque variable names; clear intent |
| 32 | P-CONTRIB-WELCOME-033 | 欢迎贡献 | CONTRIBUTING.md present, PR template provided, first-time-friendly labels |
| 33 | P-SEMVER-035 | 语义版本 | semver.org compliance; version bumps in pyproject.toml |
| 34 | P-DEEPRECATION-036 | 过时警告 | Removed features go through 2-version deprecation cycle |
| 35 | P-PIN-DEPENDENCIES-038 | 依赖范围宽松 | `>=` not `==` for libs; `~=` for compat |
| 36 | P-CI-MUST-PASS-039 | CI 必过 | All checks (lint, type, test, docs link-check) must pass |
| 37 | P-FAIL-FAST-DEPS-030 | 依赖失败要响 | `pip install` failures cause build to fail loudly |

### Docs engineering (5 rules, enforced in CI)

| # | ID | 中文 | 描述 |
|---|---|---|---|
| 25 | P-CITE-LOGS-018 | 决策有据 | Every module top docstring includes `# Inspired by: <reference>` |
| 26 | P-CHANGELOG-034 | 变更日志 | CHANGELOG.md updated per release; Keep a Changelog format |
| 27 | P-RUN-AS-IS-037 | 克隆即可跑 | README quickstart works at any commit (CI-verified) |
| 28 | P-OPEN-SOURCE-031 | 纯开源依赖 | All pip deps OSI-approved (MIT/BSD/Apache-2.0) |
| 29 | P-LINK-CHECK-DOCS-041 | docs 链接可查 | All docs links checked via CI (`.github/workflows/link-check.yml`) |

### Vendor-neutral (4 rules, opt-in via config)

These are NOT default; users opt in via `~/.astor/config.yaml`:

```yaml
# ~/.astor/config.yaml
vendor_neutral:
  llm_provider: openai   # openai | anthropic | gemini | deepseek | zhipu | ollama
  vector_store: numpy    # numpy | hnsw (v2.0)
  embedder: fastembed    # fastembed | sentence-transformers (v1.2)
  agent_language: en     # en | zh | ja | ko | fr | es
```

### Personal (4 rules, opt-in via config)

Reference implementation for the project maintainer's workflow — NOT default. See [Style reference](#6-style-reference-p-conf-003-implementation).

---

## 6. Style reference (P-CONF-003 implementation)

The Core runtime rule P-CONF-003 says: "avoid filler and redundant status narration." This section describes the *implementation* — what counts as "filler" in practice.

### What's filler

- "Great question!" / "I'd be happy to help!" / "Sure!"
- Emoji headers in status messages (🎉, ✅, etc.) unless user explicitly asked
- Multi-line celebrations for milestones ("🎊 SHIPPED! 🎊")
- Repeating the user's question back to them
- "I will now..." / "Let me..." stage announcements

### What's NOT filler

- Concrete results (e.g. "shipped in 8.3s, 4 files modified")
- Error messages with reproducible steps
- Code snippets when code is the answer
- Tables when tabular data is the answer

### Style choices (project reference)

This is a personal style guide for one specific workflow. Most open-source projects should ignore this section; it's here for transparency about how one contributor prefers to write.

- **Chinese as default language** for prose; English only for code/API/identifier
- **No emoji** in commit messages or docs (except where contextually required)
- **Short status updates**: one-line, terse, factual
- **Skip preamble**: jump to the answer, no "Let me explain..."
- **Markdown over prose**: use bullet lists and tables for structured data

If you're a contributor, write in the style that suits your audience. The Core rule is "avoid filler" — what counts as filler is contextual.

---

## 7. Multi-source consult example (P-MULTISRC-002)

The Core runtime rule says: "analyze via available knowledge sources (memory, tools, retrieval indices) before LLM synthesize."

This section gives the reference implementation (the project's stack) and the abstract pattern for other stacks.

### Abstract pattern (universal)

```python
def analyze(question: str) -> Answer:
    # 1. Consult memory (long-term facts)
    facts = astor_memory.read(question, top_k=5)
    
    # 2. Consult tools (live data)
    tool_results = [tool.run(question) for tool in relevant_tools]
    
    # 3. Consult retrieval indices (documentation, papers)
    indices = [index.search(question) for index in available_indices]
    
    # 4. Synthesize via LLM (with all 3 sources as context)
    return llm.synthesize(
        question=question,
        memory=facts,
        tools=tool_results,
        indices=indices,
    )
```

The principle: **don't ask LLM to generate from scratch when knowledge sources exist**.

### Project reference implementation

The reference stack uses 3 specific knowledge sources:

1. **skill** — hermes-agent skill library (`~/.hermes/skills/`)
2. **wiki** — personal knowledge base (`~/wiki/`)
3. **memory** — Astor-Memory itself (the system you're contributing to)

```python
# Reference analyze() implementation:
def analyze(question: str) -> Answer:
    skill_hits = hermes_skills.match(question)      # skill
    wiki_hits = wiki.search(question)               # wiki
    memory_hits = astor_memory.read(question)       # memory
    return llm.synthesize(question, skill_hits, wiki_hits, memory_hits)
```

Other stacks should adapt: replace "skill/wiki/memory" with whatever knowledge sources your agent has access to.

---

## 8. Cron role-based enforcement (P-CRON-DATA-010)

The Core runtime rule says: "operator role restricted to enum-validated cron types (config-defined whitelist); admin unrestricted."

This section reveals the enum list — implementation detail, not exposed in user-facing docs.

### Enum types (operator role)

Operator role can create cron jobs of these types ONLY:

```python
# astor_memory/cron/types.py
OPERATOR_ALLOWED_TYPES = frozenset({
    "data_pull",    # fetch data from external source
    "transform",    # process/transform data (built-in functions only)
    "deliver",      # send data to user (telegram/discord/email/etc.)
})
```

Admin role is unrestricted (can also create `custom_code` type).

### Type-specific constraints

| Type | Constraints |
|---|---|
| `data_pull` | URL must be in egress whitelist; no shell interpolation; HTTPS only |
| `transform` | Function name in built-in whitelist; no shell access; pure data in/out |
| `deliver` | Save_to path must be in user-configured destinations; no shell |
| `custom_code` | (admin only) Arbitrary code; requires explicit admin approval on creation |

### Why this matters

Operator role is for non-admin users (e.g. cron jobs triggered by external systems). The enum whitelist prevents:
- Shell injection (`type=data_pull&url=; rm -rf /`)
- Data exfiltration (`type=transform&func=open('/etc/passwd').read()`)
- Arbitrary code execution

Admin role can do anything because admins are trusted; operator role cannot.

### Implementation

```python
# astor_memory/cron/validate.py
def validate_cron_type(cron_type: str, role: str) -> None:
    if role == "admin":
        return  # no validation
    if cron_type not in OPERATOR_ALLOWED_TYPES:
        raise CronValidationError(
            f"Operator role cannot create cron of type '{cron_type}'. "
            f"Allowed: {sorted(OPERATOR_ALLOWED_TYPES)}"
        )
```

Config schema is strict: any extra fields are rejected at parse time.

---

## 9. Dedup implementation (P-DEDUPE-014)

The Core runtime rule says: "writes are deduplicated via content-aware identity (default 24h window, configurable via `am config dedup.window`)."

This section reveals the implementation — algorithm and collision handling.

### Identity computation

```python
# astor_memory/bus/dedup.py
import hashlib

def compute_stable_id(text: str, scope: str, tier: str, user_id: str | None) -> str:
    """Stable ID for a fact. Same content + same scope/tier/user = same ID."""
    canonical = f"{scope}|{tier}|{user_id or 'none'}|{normalize(text)}"
    return "f_" + hashlib.sha256(canonical.encode()).hexdigest()[:12]

def compute_content_fingerprint(text: str) -> str:
    """Content fingerprint for collision detection within the dedup window."""
    normalized = normalize(text)
    # Use multiple hash algorithms to detect collision
    return {
        "sha256": hashlib.sha256(normalized.encode()).hexdigest(),
        "simhash": simhash(normalized),  # 64-bit locality-sensitive hash
    }
```

### Dedup logic

```python
def should_dedupe(fact_id: str, fingerprint: dict, window_hours: int = 24) -> bool:
    """Check if fact_id was written within the dedup window."""
    cutoff = datetime.utcnow() - timedelta(hours=window_hours)
    existing = bus.query(fact_id=fact_id, since=cutoff)
    
    if not existing:
        return False
    
    # Verify content matches (not just ID collision)
    for e in existing:
        if e.content_fingerprint == fingerprint:
            return True  # exact duplicate
    
    return False  # ID collision but content differs — write as new
```

### Configuration

```yaml
# ~/.astor/config.yaml
dedup:
  window_hours: 24       # default; range [1, 168]
  algorithm: sha256+simhash  # default; alternatives in v1.1+
  collision_policy: merge   # merge | separate | alert
```

`window_hours=0` disables dedup entirely. Use this only for testing.

### Security note

The dedup algorithm is not cryptographically secure against adversarial input. It's designed for natural-language deduplication, not security. If your threat model includes adversarial writes, use `collision_policy=alert` to surface anomalies rather than relying on dedup alone.

---

## 10. PR workflow

### Step 1: File an issue first

For non-trivial changes, file an issue describing the problem. This avoids wasted work on PRs that don't match project direction.

### Step 2: Fork and branch

```bash
git clone https://github.com/your-username/astor-memory.git
cd astor-memory
git checkout -b feat/my-feature
```

### Step 3: Develop

```bash
# Run tests as you go
pytest tests/unit/

# Lint and type check before commit
ruff check astor_memory/
mypy astor_memory/
```

### Step 4: Update CHANGELOG.md

Add an entry under "Unreleased" section. Use the same format as existing entries:

```markdown
## [Unreleased]

### Added
- New MCP server adapter (v1.1) (#123)

### Fixed
- SQLite database lock during concurrent writes (#124)
```

### Step 5: Push and open PR

```bash
git push origin feat/my-feature
gh pr create --title "feat: my feature" --body "Closes #123"
```

### Step 6: Address review feedback

CI must pass. Reviewers may request changes. Push fixes to the same branch.

### Step 7: Squash and merge

Once approved, maintainers will squash-merge. Your branch can be deleted after merge.

---

## Release process (for maintainers)

1. Update version in `pyproject.toml` and `astor_memory/__init__.py`
2. Update `CHANGELOG.md` — move "Unreleased" to versioned section with date
3. Tag: `git tag v1.2.3 && git push --tags`
4. GitHub Actions builds wheel + sdist on tag push
5. Manually trigger PyPI publish workflow
6. Smoke test: `pip install astor-memory==1.2.3` in fresh venv

---

## Next

- [`README.md`](../README.md) — end-user overview
- [`docs/architecture.md`](./architecture.md) — design deep dive
- [`docs/faq.md`](./faq.md) — frequently asked questions
- [`docs/troubleshooting.md`](./troubleshooting.md) — common errors and fixes


---

## 11. Forking Astor-Memory

This section is for maintainers of derivative works (forks). If you fork this repo to add features, change the layout, or adapt it for a different agent framework, this guide tells you what to read first, what's safe to change, and what's load-bearing.

### What to read first (in order)

1. **[`README.md`](../README.md)** — entry point, install, first-run.
2. **[`docs/architecture.md`](architecture.md)** — the *why* of every component. Skip no sections.
3. **[`bots/README.md`](../bots/README.md)** — if your fork involves multi-platform bots.
4. **Module-level docstrings** in `astor_memory/` — each module's purpose is in the first ~20 lines.
5. **`tests/`** — the behavior specification. Any change must keep `pytest tests/` green.

### Safe to change

- **All CLI command names + their help text.** Just update `docs/api.md` accordingly.
- **LLM provider list** in `astor_memory/forge/llm_extract.py`. Add a new provider by implementing the 4-method protocol.
- **Embedding model** in `astor_memory/nest/embeddings.py` — any `fastembed`-compatible model works.
- **Tier count** (currently 3) — schema migration is supported via `am migrate --add-tier <name>`. See `docs/migration.md`.
- **Plugin adapters** in `astor_memory/installer/handlers.py` — add a new agent (Cursor, Continue, etc.) by implementing the 2-method handler.

### Load-bearing — think twice before changing

- **`astor_memory/bus/schema.py`** — the canonical fact table. Changes ripple to every store.
- **`astor_memory/_internal/acl.py`** — ACL enforcement. Any change here is security-sensitive. Add a test in `tests/test_acl.py` BEFORE editing.
- **`astor_memory/_internal/bot_binding.py`** — the bot ↔ user binding table. Multi-platform agents depend on the schema. Migration tool exists but is one-way.
- **`astor_memory/nest/vector_store.py`** + **`lex_index.py`** — vector + FTS5 indices. Schema bump = full rebuild required.
- **`pyproject.toml` `[tool.hatch.build.targets.sdist]`** — sdist include/exclude list. Removing items accidentally ships private data to PyPI.

### PII hygiene for forks

If your fork will be published and installed by other operators:

1. **Search for hardcoded operator data** before each release:
   ```bash
   grep -rnE "(user_e|user_c|user_a|user_d|operator|bot-account|@im.wechat|C:\\Users\\operator|D:\\AI\\Astor-Memory)" \
     --include="*.py" --include="*.md" --include="*.yaml" --include="*.toml" .
   ```
   Should return zero matches (excluding `CHANGELOG.md`).

2. **Verify fresh install is empty.** `am init` should create only the schema, no seed user/platform/binding data.

3. **Verify sdist is clean.**
   ```bash
   python -m build --sdist
   python -m tarfile -l dist/astor_memory-*.tar.gz | grep -E "(bots|tests|scripts|backup_astor|\.db)"
   ```
   Should return no matches.

4. **Verify tests are isolated.** `tests/test_platform_bridge.py` was the canonical example — it used to depend on the operator's real bot-binding.db. The fixed version uses a `fresh_db` fixture with synthetic fake tokens. Apply the same pattern to any new integration test.

5. **Anonymize the author** in `pyproject.toml`. The default is `the maintainer`. Change it before publishing if your fork is under a different maintainer name.

### When to bump the version

- **Patch** (1.2.6 → 1.2.7): bug fix, PII cleanup, doc update, no behavior change.
- **Minor** (1.2.6 → 1.3.0): new endpoint, new CLI command, new schema column with backward-compat read path.
- **Major** (1.x → 2.0): schema migration required, breaking API change, default tier change.

### Rebranding checklist (if you fork under a different name)

- [ ] `pyproject.toml` `name=` field
- [ ] `pyproject.toml` `authors=` field
- [ ] `README.md` repo URL
- [ ] `pyproject.toml` `[project.urls]` (Homepage, Repository, Issues, etc.)
- [ ] `bots/README.md` any references to the original maintainer path
- [ ] `ACKNOWLEDGEMENTS.md` Authors section
- [ ] `CHANGELOG.md` add a "Forked as <name>" entry at the top

### Where to ask for help

- **General questions:** open a GitHub Discussion on the upstream repo.
- **Bug reports:** GitHub Issues with `pytest -v` output + `astor doctor` report.
- **Security issues:** email maintainer (see `pyproject.toml` authors). Do NOT open a public issue.
