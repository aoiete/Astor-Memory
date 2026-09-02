# 常见问题 FAQ

> Astor-Memory 的常见问题。

如果你的问题不在这里，先查 [`docs/troubleshooting.md`](./troubleshooting.md) 看错误处理，再查 [`docs/architecture.md`](./architecture.md) 看深入设计说明。

---

## 总览

### Astor-Memory 是什么？

AI 智能体自拥有的记忆系统。三大 store（事件日志 + 事实抽取器 + 向量库），三层隔离（public / source / private × N），纯 Python，无供应商锁定。

### 为什么不用 RAG？

RAG 只是记忆的一个组成部分 — 即"检索"那一环。Astor-Memory 在此基础上额外提供：

- 追加式事件日志（审计追溯）
- LLM 事实抽取（原文 → 结构化）
- 修订跟踪（无静默覆盖）
- per-user ACL（隔离）
- 生命周期演化（衰减、合并、晋升）

RAG-only 适合只读的知识库。对于需要"自己写、自己演化"的智能体，你需要更多。

### 不用 Letta / mem0 / PowerContext 是为什么？

详细对比见 [`README.md`](../README.md#why-we-built-this)。简版：每一个都强制你接受某个权衡（重型 runtime、云耦合、pre-1.0 状态等），我们都不愿意。

### 能上生产吗？

Astor-Memory v1.0 是第一个公开发布版本。

生产环境使用：固定版本（`astor-memory==1.0.0`）、配 `am doctor` 监控、从其他 stack 迁移看 [migration guide](./migration.md)。

---

## 安装

### Python 版本？

Python 3.10 或更高，低于 3.14。CI 测试覆盖 3.10 / 3.11 / 3.12 / 3.13。

### 安装体积多大？

< 50 MB。对比：

- chromadb：~80 MB
- transformers + torch：~3 GB
- 重型专有记忆 SDK：不定，通常 100-300 MB

### 支持 Windows / macOS / Linux 吗？

支持。CI 主要在 Ubuntu 24.04 跑，同时测 macOS 14 和 Windows 11。代码库全程用 `pathlib.Path`，没有 shell-specific 假设。

### 一键安装（Linux / macOS）

用仓库里附带的安装脚本（一次性处理 venv 创建、GitHub 拉取、数据目录初始化和 PATH 设置）：

```bash
curl -fsSL https://raw.githubusercontent.com/aoiete/Astor-Memory/main/scripts/install.sh | bash
# 或指定版本
curl -fsSL https://raw.githubusercontent.com/aoiete/Astor-Memory/main/scripts/install.sh | bash -s v1.13.1
# 交互式（会问数据目录）
./scripts/install.sh
# 非交互式（CI/自动化）
./scripts/install.sh --non-interactive
# 自定义数据目录
./scripts/install.sh --dir /opt/astor/data
```

### 一键安装（Windows）

用仓库里附带的 PowerShell 脚本：

```powershell
# 从 GitHub raw 一行安装
iwr -useb https://raw.githubusercontent.com/aoiete/Astor-Memory/main/scripts/install.ps1 | iex
# 或本地
.\scripts\install.ps1
.\scripts\install.ps1 v1.13.1
.\scripts\install.ps1 -NonInteractive
.\scripts\install.ps1 -Dir 'D:\astor\data'
```

### 怎么卸载？

```bash
# Linux/macOS
./scripts/install.sh --uninstall [--dir <path>]

# Windows
.\scripts\install.ps1 -Uninstall [-Dir <path>]
```

或者直接 `rm -rf <ASTOR_HOME>` — 数据目录和 `.venv/` 下的 venv 都是自包含的。

### 数据目录在哪？能改吗？

默认：

- Linux/macOS：`~/.astor/`（或 `$ASTOR_HOME` 如果设了）
- Windows：`%USERPROFILE%\.astor\`（或 `$env:ASTOR_HOME` 如果设了）

装之前设 `ASTOR_HOME` 环境变量即可换路径。装脚本也支持 `--dir` / `-Dir` 一次性覆盖。

装完跑 `am doctor` 验证。

---

## 使用

### 数据存在哪里？

默认：`~/.astor/`（Unix）或 `%USERPROFILE%\.astor\`（Windows）。

用 `ASTOR_HOME=/path/to/dir` 环境变量覆盖。

布局：

```
~/.astor/
├── config.yaml          # 用户配置
├── astor_bus.db         # events + memory_candidates + memory_canonical + audit_log
├── astor_nest.db        # 向量 embedding（每条 fact 一行，model_name 索引）
├── astor_forge.db       # LLM 抽取缓存（v0.2+ LLM extract）
└── private_<user>.db    # per-user DB（多用户模式，v1.1+）
```

### 怎么备份？

三个文件是 source of truth：

- `astor_bus.db` — events + canonical facts
- `astor_nest.db` — 向量 embedding
- `private_*.db` — per-user DB（多用户模式，v1.1+）

复制到安全位置。恢复：放回 `~/.astor/`。

`astor_forge.db` 是缓存，删掉不丢数据（下次 `write` 时抽取会重跑）。

### 能用多个 LLM provider 吗？

可以。每次写或全局配置：

```python
# 全局
configure(llm_provider="anthropic")

# 每次写（v1.1+）
write("text", llm_provider="gemini")
```

### 能离线用吗？

部分支持。向量库（`nest`）完全本地；事件日志（`bus`）本地 SQLite；LLM 抽取（`forge`）需要联网到你的 provider。

要完全离线（v1.2+），用 `llm_provider="ollama"` + 本地模型。

---

## 性能

### 读多快？

~1 ms / 查询（暴力 NumPy kNN），最多 5 K 文档。线性扩展：10 K ≈ 2 ms，50 K ≈ 10 ms。

> 100 K 文档的 HNSW 索引推迟到 v2.0。

### 写多快？

~10 ms / 写（SQLite insert + fire-and-forget LLM 调用）。Async 模式（`write_async`）<1 ms 返回；抽取后台跑。

### 占多少磁盘？

每条 event 约 1 KB（原文）+ 每条 fact 约 5 KB（抽取 + embedding 后）。10 K facts 约 ~50 MB。DB 线性增长；老 events 可按 TTL 修剪。

### 内存泄漏？

没有已知的。Astor-Memory 用 SQLite 持久化（无 unbounded 内存增长）+ NumPy 做向量（常规 GC）。每周跑 `am doctor` 确认 event count 没有意外增长。

---

## 从其他 memory stack 迁移

完整指南见 [`docs/migration.md`](./migration.md)，覆盖 mem0 / Letta / Zep / MemGPT / ChromaDB / Pinecone / Weaviate / 普通文件。快速回答：

### 能并行运行两个系统吗？

可以。`am init --parallel` 用 7804-7806 端口，不会和源系统冲突。两个系统同时跑。

### 老的 cron job 会断吗？

不会。现有 cron 配置继续有效，因为并行运行窗口期间两个 stack 都能用。

### 数据迁移要多久？

看源系统。mem0 → astor：~10 秒 / 1000 条记忆。Letta / Zep / Chroma 类似。Pinecone / Weaviate（网络）由网络 I/O 主导。普通文件：~1 秒 / 100 条 fact。

### 迁移后能回滚吗？

可以。三步回滚：停 Astor-Memory → 重启源 stack → 验证。并行期间老数据没动过。

---

## 对比

### 跟 CoALA 论文的记忆分类有什么不同？

CoALA（arXiv:2309.02427）描述了认知架构中的 4 种记忆：

- Working memory
- Episodic memory
- Semantic memory
- Procedural memory

Astor-Memory 用不同方式实现：

- Working memory ≈ `bus`（近期 events）
- Episodic memory ≈ `forge` 输出（带时间戳的抽取 facts）
- Semantic memory ≈ `nest`（向量索引的通用知识）
- Procedural memory ≈ skills（外部；用 `am skill scan` 扫描）

我们不是说"实现了"CoALA，而是"对齐了"这个架构。

### 跟 Mem-π 论文有什么不同？

Mem-π（Mem-π: Adaptive Memory through Learning When and What to Generate）提出：

- 按需生成记忆（不只是检索，还要生成指导）
- 弃答机制（71% 弃答率；简单任务不需要记忆）
- 跨 LLM 迁移（记忆策略独立于执行者）

Astor-Memory v1.0 吸收了 **Insight 11（跨 LLM adapter）**。Insight 9（按需生成）和 Insight 10（弃答）推迟到 v1.1。

我们吸收了 Mem-π 的 insights，但底层存储实现不同 — 他们专注"生成"策略，我们专注"存储"基质。

---

## 贡献

### 怎么贡献？

见 [`docs/contributing.md`](./contributing.md)。简版：先开 GitHub issue，再提 PR。

### 代码风格？

Python 3.10+，到处 type hints，路径用 `pathlib.Path`，ruff 静态检查，mypy strict。测试覆盖率 80% 要求（P-TEST-80-019）。

### 发布节奏？

每周 4 个 phase：

- v0.1：移除外部依赖
- v0.2：重命名 + 重构
- v0.3：pip installable + REST + CI
- v1.0：docs + polish + 开源发布

v1.0 之后走 semver.org。Minor 版本每 1-3 个月一次。

---

## 接下来看

- [`docs/architecture.md`](./architecture.md) — 深入 3-store × 3-tier
- [`docs/migration.md`](./migration.md) — 从 mem0 / Letta / Zep / MemGPT / ChromaDB / Pinecone / Weaviate / 普通文件 迁移
- [`docs/agent-adapters.md`](./agent-adapters.md) — MCP / LangChain / REST / Python 集成
- [`docs/troubleshooting.md`](./troubleshooting.md) · [中文](troubleshooting.zh-CN.md) — 常见错误 + 修复
- [`docs/contributing.md`](./contributing.md) — 给贡献者