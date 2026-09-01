# 给 Astor-Memory 贡献

> 给贡献者看。终端用户请读 [`README.md`](../README.md)。

本文档是以下内容的真源：

- 开发环境搭建
- 代码风格
- 测试要求
- Ship-time iron rules（8 个 Engineering + 5 个 Docs engineering）
- runtime rules 的实现细节（风格参考、去重、cron 枚举）

---

## 目录

1. [快速开始](#快速开始)
2. [开发环境搭建](#开发环境搭建)
3. [代码风格](#代码风格)
4. [测试](#测试)
5. [Iron rules 参考](#iron-rules-参考)
6. [风格参考（P-CONF-003 实现）](#6-风格参考p-conf-003-实现)
7. [多源咨询示例（P-MULTISRC-002）](#7-多源咨询示例p-multisrc-002)
8. [Cron 角色强制（P-CRON-DATA-010）](#8-cron-角色强制p-cron-data-010)
9. [去重实现（P-DEDUPE-014）](#9-去重实现p-dedupe-014)
10. [PR 流程](#pr-流程)

---

## 快速开始

```bash
git clone https://github.com/aoiete/ASTOR-Memory.git
cd astor-memory
pip install -e .[dev]
pytest tests/
```

如果所有测试通过，你就 setup 好了。完整细节见 [开发环境搭建](#开发环境搭建)。

---

## 开发环境搭建

### Python 版本

Python 3.10-3.13。我们用 GitHub Actions matrix 测试 3.10 / 3.11 / 3.12 / 3.13。

### 装开发依赖

```bash
pip install -e .[dev]
```

这会装：

- `pytest`, `pytest-cov`, `pytest-asyncio` — 测试
- `ruff` — lint
- `mypy` — 类型检查
- `hatch` — 构建

### 验证 setup

```bash
am --version          # → am 0.1.0.dev0 (astor-memory 0.1.0.dev0)
pytest --version      # → pytest 7.x 或更新
ruff --version        # → ruff 0.4.x 或更新
mypy --version        # → mypy 1.8.x 或更新
```

### 跑所有检查

```bash
# Lint
ruff check astor_memory/

# Type check
mypy astor_memory/

# Tests with coverage
pytest --cov=astor_memory --cov-report=term-missing

# 一次跑
make check  # 如果有 Makefile；否则逐条跑
```

---

## 代码风格

### Python 约定

- **Type hints 到处**（mypy strict mode 强制）
- **路径操作**：只用 `pathlib.Path`，不用 `os.path` 字符串 (P-IO-PATHLIB-023)
- **Imports**：isort-style（ruff 强制）
- **行长**：100 字符
- **命名**：函数/变量 `snake_case`，类 `PascalCase`，常量 `UPPER_SNAKE_CASE`

### Module docstring 约定 (P-CITE-LOGS-018)

每个 module 顶部 docstring 必须包含 `# Inspired by: <reference>`，标注借鉴的开源模式或论文。

```python
"""
astor_memory.bus — append-only event log.

SQLite + WAL. Time-series.

Inspired by:
  - PowerContext RFC 0020 (lifecycle evolution)
  - CoALA paper §4 (episodic memory)
"""
```

### 函数 docstring

用 Google-style：

```python
def write(text: str, scope: str = "long_term", tier: str = "public") -> FactId:
    """Append a fact to bus; forge extracts in background.

    Args:
        text: 事实文本。会嵌入做向量搜索。
        scope: short_term | long_term | profile. 默认 long_term.
        tier: public | source | private. 默认 public.

    Returns:
        可在 recall 输出里用作引用的 FactId.

    Raises:
        MemoryWriteError: 写失败重试后仍然出错。
    """
```

### Commit messages

跟随 [Conventional Commits](https://www.conventionalcommits.org/)：

```text
feat: add MCP server adapter (v1.1)
fix: handle SQLite database lock in concurrent writes
docs: clarify citation-first design in architecture.md
refactor: extract deduplication logic to astor_memory.bus.dedup
test: add coverage for revision tracking edge cases
```

---

## 测试

### 覆盖率要求

**所有 PR 必须 80% 行覆盖率**（P-TEST-80-019）。CI 在低于这个值时会失败。

本地查覆盖率：

```bash
pytest --cov=astor_memory --cov-report=term-missing
```

### 测试结构

```text
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

用 markers 给测试分类：

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

跑子集：

```bash
pytest -m unit                    # 只跑 unit 测试
pytest -m "not integration"       # 跳过慢的 integration 测试
pytest -m "integration and not slow"
```

### Async tests

用 `pytest-asyncio`：

```python
import pytest

@pytest.mark.asyncio
async def test_async_write():
    from astor_memory import write_async
    fid = await write_async("test")
    assert fid is not None
```

---

## Iron rules 参考

Astor-Memory ship 了 5 类 iron rules。三类（Engineering ship-time, Docs engineering, Vendor-neutral, Personal）放在本文档。Core 15 runtime rules 在源码里（`astor_memory/defaults/iron_rules.yaml`），runtime 强制。

### Engineering ship-time（8 条，CI 强制）

| # | ID | 中文 | 描述 |
|---|---|---|---|
| 30 | P-TEST-80-019 | 80% 测试覆盖 | pytest-cov check；PR 行覆盖率低于 80% 失败 |
| 31 | P-NO-OBFUSC-032 | 不混淆代码 | 不压缩、不用隐晦变量名；意图清晰 |
| 32 | P-CONTRIB-WELCOME-033 | 欢迎贡献 | CONTRIBUTING.md 存在、PR 模板齐备、对新人友好的 labels |
| 33 | P-SEMVER-035 | 语义版本 | semver.org 合规；pyproject.toml 里 bump version |
| 34 | P-DEEPRECATION-036 | 过时警告 | 删除功能走 2-version 弃用周期 |
| 35 | P-PIN-DEPENDENCIES-038 | 依赖范围宽松 | 库用 `>=` 而不是 `==`；compat 用 `~=` |
| 36 | P-CI-MUST-PASS-039 | CI 必过 | 所有 check（lint、type、test、docs link-check）必须过 |
| 37 | P-FAIL-FAST-DEPS-030 | 依赖失败要响 | `pip install` 失败让 build 大声失败 |

### Docs engineering（5 条，CI 强制）

| # | ID | 中文 | 描述 |
|---|---|---|---|
| 25 | P-CITE-LOGS-018 | 决策有据 | 每个 module 顶部 docstring 都带 `# Inspired by: <reference>` |
| 26 | P-CHANGELOG-034 | 变更日志 | CHANGELOG.md 每次 release 更新；Keep a Changelog 格式 |
| 27 | P-RUN-AS-IS-037 | 克隆即可跑 | README quickstart 在任何 commit 都能跑（CI 验证） |
| 28 | P-OPEN-SOURCE-031 | 纯开源依赖 | 所有 pip 依赖 OSI 批准（MIT/BSD/Apache-2.0） |
| 29 | P-LINK-CHECK-DOCS-041 | docs 链接可查 | 所有 docs 链接 CI 检查（`.github/workflows/link-check.yml`） |

### Vendor-neutral（4 条，opt-in via config）

这些**不是默认**；用户通过 `~/.astor/config.yaml` 启用：

```yaml
# ~/.astor/config.yaml
vendor_neutral:
  llm_provider: openai   # openai | anthropic | gemini | deepseek | zhipu | ollama
  vector_store: numpy    # numpy | hnsw (v2.0)
  embedder: fastembed    # fastembed | sentence-transformers (v1.2)
  agent_language: en     # en | zh | ja | ko | fr | es
```

### Personal（4 条，opt-in via config）

项目维护者个人工作流的参考实现 — 不是默认。详见 [风格参考](#6-风格参考p-conf-003-实现)。

---

## 6. 风格参考（P-CONF-003 实现）

Core runtime rule P-CONF-003 说："避免填充和冗余的状态叙述"。本节描述**实现** — 实践中什么算"填充"。

### 什么算填充

- "Great question!" / "I'd be happy to help!" / "Sure!"
- 状态消息里的 emoji header（🎉、✅ 等），除非用户明确要求
- 里程碑的多行庆祝（"🎊 SHIPPED! 🎊"）
- 把用户的问题重复一遍
- "I will now..." / "Let me..." 阶段宣告

### 什么不算填充

- 具体结果（如 "shipped in 8.3s, 4 files modified"）
- 带可复现步骤的错误消息
- 当代码是答案时的代码片段
- 当数据是答案时的表格

### 风格选择（项目参考）

这是一个针对某一种工作流的个人风格指南。大多数开源项目应该忽略本节；这里列出来是为了透明化一个贡献者如何写作。

- **中文为默认语言**（prose）；英文只在 code/API/identifier
- **commit message 和 docs 不用 emoji**（除非上下文需要）
- **简短状态更新**：一行、简短、事实
- **跳过开场白**：直接给答案，不要 "Let me explain..."
- **Markdown > prose**：结构化数据用 bullet 列表和表格

如果你是贡献者，按适合你的读者风格写。Core rule 是"避免填充" — 什么算填充是上下文相关的。

---

## 7. 多源咨询示例（P-MULTISRC-002）

Core runtime rule 说："LLM 合成前通过可用知识源（memory、tools、retrieval indices）分析。"

本节给出参考实现（项目的栈）和抽象模式。

### 抽象模式（通用）

```python
def analyze(question: str) -> Answer:
    # 1. 咨询 memory（长期事实）
    facts = astor_memory.read(question, top_k=5)

    # 2. 咨询 tools（实时数据）
    tool_results = [tool.run(question) for tool in relevant_tools]

    # 3. 咨询 retrieval indices（文档、论文）
    indices = [index.search(question) for index in available_indices]

    # 4. 用 LLM 合成（带全部 3 个 source 作为上下文）
    return llm.synthesize(
        question=question,
        memory=facts,
        tools=tool_results,
        indices=indices,
    )
```

原则：**知识源存在时，不要让 LLM 从零生成**。

### 项目参考实现

参考栈用 3 个具体知识源：

1. **skill** — hermes-agent skill 库（`~/.hermes/skills/`）
2. **wiki** — 个人知识库（`~/wiki/`）
3. **memory** — Astor-Memory 本身（你正在贡献的这个系统）

```python
# 参考 analyze() 实现：
def analyze(question: str) -> Answer:
    skill_hits = hermes_skills.match(question)      # skill
    wiki_hits = wiki.search(question)               # wiki
    memory_hits = astor_memory.read(question)       # memory
    return llm.synthesize(question, skill_hits, wiki_hits, memory_hits)
```

其他栈应适配：把 "skill/wiki/memory" 替换成你的 agent 能访问的知识源。

---

## 8. Cron 角色强制（P-CRON-DATA-010）

Core runtime rule 说："operator 角色限制为枚举验证的 cron 类型（config 定义的 whitelist）；admin 不限。"

本节揭示枚举列表 — 实现细节，不暴露在用户文档里。

### 枚举类型（operator 角色）

Operator 角色**只能**创建这些类型的 cron：

```python
# astor_memory/cron/types.py
OPERATOR_ALLOWED_TYPES = frozenset({
    "data_pull",    # 从外部源拉数据
    "transform",    # 处理/转换数据（仅内置函数）
    "deliver",      # 发数据给用户（telegram/discord/email 等）
})
```

Admin 角色不受限（也能创建 `custom_code` 类型）。

### 类型特定约束

| 类型 | 约束 |
|---|---|
| `data_pull` | URL 必须在 egress whitelist 里；不允许 shell 插值；只 HTTPS |
| `transform` | 函数名在 built-in whitelist；无 shell 访问；纯数据进出 |
| `deliver` | Save_to 路径必须在用户配置的 destinations 里；无 shell |
| `custom_code` | (仅 admin) 任意代码；创建时需要明确 admin 批准 |

### 为什么重要

Operator 角色给非 admin 用户（例如外部系统触发的 cron job）。枚举 whitelist 防止：

- Shell 注入（`type=data_pull&url=; rm -rf /`）
- 数据外泄（`type=transform&func=open('/etc/passwd').read()`）
- 任意代码执行

Admin 角色可以做任何事因为 admin 被信任；operator 角色不行。

### 实现

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

Config schema 是严格的：任何额外字段都在 parse 时拒绝。

---

## 9. 去重实现（P-DEDUPE-014）

Core runtime rule 说："写入通过 content-aware identity 去重（默认 24h 窗口，可通过 `am config dedup.window` 配置）。"

本节揭示实现 — 算法和冲突处理。

### Identity 计算

```python
# astor_memory/bus/dedup.py
import hashlib

def compute_stable_id(text: str, scope: str, tier: str, user_id: str | None) -> str:
    """Fact 的 stable ID。同内容 + 同 scope/tier/user = 同 ID。"""
    canonical = f"{scope}|{tier}|{user_id or 'none'}|{normalize(text)}"
    return "f_" + hashlib.sha256(canonical.encode()).hexdigest()[:12]

def compute_content_fingerprint(text: str) -> str:
    """用于去重窗口内冲突检测的内容 fingerprint。"""
    normalized = normalize(text)
    # 用多种 hash 算法检测冲突
    return {
        "sha256": hashlib.sha256(normalized.encode()).hexdigest(),
        "simhash": simhash(normalized),  # 64-bit locality-sensitive hash
    }
```

### 去重逻辑

```python
def should_dedupe(fact_id: str, fingerprint: dict, window_hours: int = 24) -> bool:
    """检查 fact_id 是否在去重窗口内写过。"""
    cutoff = datetime.utcnow() - timedelta(hours=window_hours)
    existing = bus.query(fact_id=fact_id, since=cutoff)

    if not existing:
        return False

    # 验证内容匹配（不只是 ID 冲突）
    for e in existing:
        if e.content_fingerprint == fingerprint:
            return True  # 完全重复

    return False  # ID 冲突但内容不同 — 作为新的写
```

### 配置

```yaml
# ~/.astor/config.yaml
dedup:
  window_hours: 24       # 默认；范围 [1, 168]
  algorithm: sha256+simhash  # 默认；v1.1+ 有备选
  collision_policy: merge   # merge | separate | alert
```

`window_hours=0` 完全禁用去重。仅用于测试。

### 安全注意

去重算法**不抗对抗输入**。它是为自然语言去重设计的，不是为安全。如果你的威胁模型包含对抗写入，用 `collision_policy=alert` 暴露异常，而不是只依赖去重。

---

## 10. PR 流程

### Step 1：先开 issue

非琐碎变更，先开 issue 描述问题。避免做跟项目方向不符的 PR 浪费。

### Step 2：Fork + 分支

```bash
git clone https://github.com/your-username/astor-memory.git
cd astor-memory
git checkout -b feat/my-feature
```

### Step 3：开发

```bash
# 边开发边跑测试
pytest tests/unit/

# commit 前 lint + type check
ruff check astor_memory/
mypy astor_memory/
```

### Step 4：更新 CHANGELOG.md

在 "Unreleased" section 加条目。用现有条目的同款格式：

```markdown
## [Unreleased]

### Added
- New MCP server adapter (v1.1) (#123)

### Fixed
- SQLite database lock during concurrent writes (#124)
```

### Step 5：Push + 开 PR

```bash
git push origin feat/my-feature
gh pr create --title "feat: my feature" --body "Closes #123"
```

### Step 6：处理 review feedback

CI 必须过。Reviewer 可能要求改动。推到同一分支修。

### Step 7：Squash + merge

批准后，maintainer 会 squash-merge。合并后分支可以删。

---

## 发布流程（给 maintainer）

1. 更新 `pyproject.toml` 和 `astor_memory/__init__.py` 里的 version
2. 更新 `CHANGELOG.md` — 把 "Unreleased" 移到带日期的版本 section
3. 打 tag：`git tag v1.2.3 && git push --tags`
4. GitHub Actions 在 tag push 时构建 wheel + sdist
5. 手动触发 PyPI publish workflow
6. Smoke test：在新 venv `pip install astor-memory==1.2.3`

---

## Next

- [`README.md`](../README.md) — 终端用户概览
- [`docs/architecture.md`](./architecture.md) · [中文](architecture.zh-CN.md) — 设计深入
- [`docs/faq.md`](./faq.md) · [中文](faq.zh-CN.md) — 常见问题
- [`docs/troubleshooting.md`](./troubleshooting.md) · [中文](troubleshooting.zh-CN.md) — 常见错误 + 修复

---

## 11. Forking Astor-Memory

本节给 derivative works（fork）的 maintainer 看。如果你 fork 这个 repo 来加 feature、改 layout、或适配其他 agent framework，本指南告诉你先读什么、什么安全可改、什么是承重的。

### 先读什么（按顺序）

1. **[`README.md`](../README.md)** — 入口、install、first-run。
2. **[`docs/architecture.md`](architecture.md)** — 每个组件的*为什么*。一段都不要跳。
3. **[`bots/README.md`](../bots/README.md)** — 如果你的 fork 涉及多平台 bot。
4. **`astor_memory/` 的 module-level docstring** — 每个 module 的用途在头 ~20 行。
5. **`tests/`** — 行为规范。任何改动必须保持 `pytest tests/` 绿。

### 安全可改

- **所有 CLI 命令名 + 它们的 help text。** 同步更新 `docs/api.md`。
- **`astor_memory/forge/llm_extract.py` 里的 LLM provider 列表**。实现 4-method 协议加新 provider。
- **`astor_memory/nest/embeddings.py` 里的 embedding 模型** — 任何 `fastembed` 兼容模型都行。
- **Tier 数量**（当前 3）— 通过 `am migrate --add-tier <name>` 支持 schema 迁移。见 `docs/migration.md`。
- **`astor_memory/installer/handlers.py` 里的 plugin adapter** — 实现 2-method handler 加新 agent（Cursor、Continue 等）。

### 承重 — 改之前三思

- **`astor_memory/bus/schema.py`** — canonical fact table。改动会波及所有 store。
- **`astor_memory/_internal/acl.py`** — ACL 强制。这里的任何改动都跟安全有关。改之前先在 `tests/test_acl.py` 加测试。
- **`astor_memory/_internal/bot_binding.py`** — bot ↔ user binding table。多平台 agent 依赖此 schema。迁移工具存在但是单向。
- **`astor_memory/nest/vector_store.py`** + **`lex_index.py`** — vector + FTS5 索引。schema bump = 需要全量重建。
- **`pyproject.toml` `[tool.hatch.build.targets.sdist]`** — sdist include/exclude 列表。误删项目会把私有数据 ship 到 PyPI。

### Fork 的 PII hygiene

如果你的 fork 要发布给别人装：

1. **每次发布前搜索硬编码 operator 数据**：
   ```bash
   grep -rnE "(user_e|user_c|user_a|user_d|operator|bot-account|@im.wechat|C:\\Users\\operator|D:\\AI\\Astor-Memory)" \
     --include="*.py" --include="*.md" --include="*.yaml" --include="*.toml" .
   ```
   应该返回零匹配（排除 `CHANGELOG.md`）。

2. **验证全新安装是空的。** `am init` 应该只创建 schema，没有种子 user/platform/binding 数据。

3. **验证 sdist 是干净的。**
   ```bash
   python -m build --sdist
   python -m tarfile -l dist/astor_memory-*.tar.gz | grep -E "(bots|tests|scripts|backup_astor|\.db)"
   ```
   应该无匹配。

4. **验证测试是隔离的。** `tests/test_platform_bridge.py` 是规范例子 — 之前依赖 operator 真实的 bot-binding.db。修好的版本用 `fresh_db` fixture 合成假 token。新加的 integration test 套同样的模式。

5. **匿名作者** 在 `pyproject.toml`。默认是 `the maintainer`。如果你的 fork 是另一个 maintainer，发布前改。

### 何时 bump version

- **Patch**（1.2.6 → 1.2.7）：bug fix、PII cleanup、doc 更新、无行为变更。
- **Minor**（1.2.6 → 1.3.0）：新 endpoint、新 CLI 命令、新 schema column 配向后兼容读路径。
- **Major**（1.x → 2.0）：需要 schema 迁移、break API 变更、默认 tier 变更。

### Rebranding checklist（fork 换名）

- [ ] `pyproject.toml` `name=` 字段
- [ ] `pyproject.toml` `authors=` 字段
- [ ] `README.md` repo URL
- [ ] `pyproject.toml` `[project.urls]`（Homepage, Repository, Issues 等）
- [ ] `bots/README.md` 任何原 maintainer 路径引用
- [ ] `ACKNOWLEDGEMENTS.md` Authors section
- [ ] `CHANGELOG.md` 顶部加一条 "Forked as <name>"

### 在哪里求助

- **一般问题**：在 upstream repo 开 GitHub Discussion。
- **Bug 报告**：GitHub Issues 带 `pytest -v` 输出 + `astor doctor` 报告。
- **安全问题**：邮件 maintainer（见 `pyproject.toml` authors）。**不要**开 public issue。