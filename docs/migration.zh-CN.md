# 迁移指南

> 从任何常见的 AI memory stack 迁到 Astor-Memory。

本指南覆盖最常被问到的源系统：**mem0**、**Letta / Zep / MemGPT**、**ChromaDB / Pinecone / Weaviate**、**普通文件 / JSON 归档**。预留 ~2 小时专注工作 + 1-2 周并行运行窗口后再切。

---

## 目录

1. [为什么迁移](#为什么迁移)
2. [源系统兼容矩阵](#源系统兼容矩阵)
3. [通用 5 步迁移](#通用-5-步迁移)
4. [从 mem0 迁](#从-mem0-迁)
5. [从 Letta / Zep / MemGPT 迁](#从-letta--zep--memgpt-迁)
6. [从 ChromaDB / Pinecone / Weaviate 迁](#从-chromadb--pinecone--weaviate-迁)
7. [从普通文件或 JSON 归档迁](#从普通文件或-json-归档迁)
8. [API 并排参考](#api-并排参考)
9. [回滚流程](#回滚流程)
10. [常见坑](#常见坑)

---

## 为什么迁移

Astor-Memory 为一个具体部署形态而生：

> **一个 bot server，许多隔离的用户。** 妈妈的生日备忘不会泄露到
> 朋友群；你给哥们写的扑克牌简报也不会污染你表妹的职业建议。

如果你在评估要不要迁移，要问的问题是：

| 如果你现在的 stack 是... | 迁移价值 | 为什么 |
|---|---|---|
| mem0 | **高** | 同是 per-user 隔离，但 mem0 只支持多租户 SaaS；Astor 给你在单 server 上每个用户自拥有的 DB |
| Letta / Zep / MemGPT | **高** | 这些都给 agent 长期记忆，但所有用户共享一个 DB；Astor 通过设计就 per-user ACL |
| ChromaDB / Pinecone / Weaviate | **中** | 你有的是向量库，不是 agent memory system；Astor 在相同向量之上加了 facts/scenarios/profile + ACL |
| 普通文件 / JSON | **低但有用** | 你得到版本跟踪、去重、衰减、scenario clustering — 普通 JSON 没有的所有东西 |

如果不确定你适合哪个 bucket，看 [`docs/architecture.md`](./architecture.md) 了解 Astor-Memory 到底是什么。

---

## 源系统兼容矩阵

每个源系统映射到 Astor-Memory 的快速检查：

| 源系统 | 数据位置 | Astor 等价 | 一键 CLI？ |
|---|---|---|---|
| mem0 | `mem0/vectors/` + per-user JSON | `private_<user>.db`（bus + nest） | `am migrate from-mem0` (v1.3+) |
| Letta | PostgreSQL 或 SQLite | `private_<user>.db`（bus + forge + nest） | `am migrate from-letta` (v1.4+) |
| Zep | Zep 云或本地 Docker | `private_<user>.db` | `am migrate from-zep` (v1.4+) |
| MemGPT | SQLite | `private_<user>.db` | `am migrate from-memgpt` (v1.4+) |
| ChromaDB | 本地目录或 server | `private_<user>.db`（只 nest — 你的向量 DB 保留） | `am migrate from-chroma` (v1.3+) |
| Pinecone | 云索引 | `private_<user>.db`（bus + forge；nest 指向 Pinecone） | 手动 |
| Weaviate | 本地 server | 同 Pinecone | 手动 |
| 普通 JSON / YAML / Markdown | 本地目录 | `private_<user>.db`（bus） | `am migrate from-files` (v1.3+) |

CLI flag 列出来的是计划中的；如果 CLI 还没做你的源系统，用下面的 per-system 走查。

---

## 通用 5 步迁移

大多数迁移，无论源系统，都按这个骨架走：

### Step 1 — 装 Astor-Memory 跟现 stack 并排

```bash
pip install astor-memory
```

验证装：

```bash
am --version
am doctor
# → bus: NOT INITIALIZED（预期；还没跑）
# → forge: NOT INITIALIZED
# → nest: NOT INITIALIZED
```

现有 stack 不动。两个可以并排跑。

### Step 2 — 用并行模式初始化 Astor-Memory

```bash
am init --parallel --port=7804
```

`--parallel` flag 让 Astor-Memory 用 7804-7806 端口，避免和你 7801-7803 上任何东西冲突。

现在你有：

```text
7801–7803   你现有 stack（mem0 / Letta / Chroma 等）
7804        astor_forge
7805        astor_nest
7806        astor_bus
```

### Step 3 — 迁数据（一次性）

跑 per-system 迁移（看下面专门那节）。总是先 `--dry-run`。

```bash
# 示例形态
am migrate from-<source-system> --source=<old-path> --dry-run
am migrate from-<source-system> --source=<old-path>
```

迁完：

```text
~/.astor/
├── astor_bus.db      # facts + events + audit
├── astor_nest.db     # 向量 embedding + lex 索引
└── astor_forge.db    # 抽取的结构化 facts
```

如果你的源系统已经按用户分开（Letta、mem0 with `user_id` 字段），Astor 保留用户边界。如果源系统是单一共享 store（ChromaDB、普通文件），Astor 从 `user_id` metadata 字段（如果有）推断 per-user 分隔，否则 fallback 到一个 admin-private DB，等你给数据打标签。

### Step 4 — 切（1-2 周后）

```bash
# 停原 stack 服务
<your stack stop command>

# 切 Astor-Memory 到规范端口
am config bus.port=7803
am config forge.port=7801
am config nest.port=7802

# 在规范端口重启 Astor-Memory
am serve --detach
```

验证健康：

```bash
am doctor
# → bus: OK (N events migrated)
# → forge: OK (provider=openai, latency=320ms)
# → nest: OK (N docs indexed, X KB vector cache)
```

跑你现有测试集：

```bash
pytest tests/
```

如果有失败，看 [回滚流程](#回滚流程)。

### Step 5 — 归档旧 stack（不删）

```bash
mv <legacy-dir> <legacy-dir>-archived-$(date +%Y-%m-%d)
```

Astor-Memory ship 了迁移工具但不自动删老数据。决定权在你。

---

## 从 mem0 迁

[mem0](https://mem0.ai) 是 AI agent memory 的多租户 SaaS。Per-user 隔离在 API 层强制；数据在 mem0 云里。迁到 Astor-Memory 是**高价值**，因为你得到自拥有的 per-user DB 在单 server 上，无 SaaS 依赖。

### 映射

| mem0 概念 | Astor 等价 |
|---|---|
| `Memory.add(messages, user_id="alice")` | `write(text, user_id="alice")` |
| `Memory.search(query, user_id="alice")` | `read(query, user_id="alice")` |
| `user_id` 字段 | `user_id` 字段（同） |
| `agent_id` 字段 | `metadata["agent_id"]` |
| `run_id` 字段 | `metadata["run_id"]` |
| `created_at` 时间戳 | `created_at`（同） |

### 迁移脚本

```python
# scripts/migrate_from_mem0.py
from mem0 import MemoryClient
from astor_memory import write, init

init(actor="first_admin", role="first_admin")

client = MemoryClient(api_key="<your-mem0-key>")

for user_id in client.list_users():
    memories = client.get_all(user_id=user_id, limit=10000)
    for mem in memories:
        write(
            mem["memory"],
            user_id=user_id,
            scope="long_term",
            metadata={
                "agent_id": mem.get("agent_id"),
                "run_id": mem.get("run_id"),
                "migrated_from": "mem0",
                "mem0_id": mem["id"],
            },
        )
    print(f"{user_id}: {len(memories)} memories migrated")
```

> 计费：mem0 按写入计费；pull 所有记忆是每个用户一次读，所以不管记忆多少都很便宜。

---

## 从 Letta / Zep / MemGPT 迁

这三个是 Astor-Memory 最接近的竞品：都提供 **agent 管理的长期记忆**，有 blocks、archival memory、recall。主要区别是它们都共享一个 DB；Astor 默认按用户分开。

### Letta → Astor

Letta 把 agents 和它们的 blocks 存在 PostgreSQL 或 SQLite 里。每个 agent 有自己的 block 集。

```python
# scripts/migrate_from_letta.py
import sqlite3  # 或者 psycopg2 for Postgres
from astor_memory import write, init

init(actor="first_admin", role="first_admin")

conn = sqlite3.connect("<your-letta-db>.sqlite")
agents = conn.execute("SELECT id, user_id, name FROM agents").fetchall()

for agent_id, user_id, name in agents:
    blocks = conn.execute(
        "SELECT label, value, created_at FROM blocks "
        "WHERE agent_id = ?", (agent_id,)
    ).fetchall()
    for label, value, created_at in blocks:
        write(
            value,
            user_id=user_id,
            scope="long_term",
            metadata={
                "agent_id": agent_id,
                "block_label": label,
                "created_at": created_at,
                "migrated_from": "letta",
            },
        )
    print(f"agent={name} user={user_id}: {len(blocks)} blocks migrated")
```

Letta 的 `user_id` 字段（如果有）变成 Astor 的 `user_id`。如果你的 Letta 部署所有 agents 共享一个用户，把整个导入当 `first_admin` 的，以后重新打标签。

### Zep → Astor

Zep 把 sessions 和 messages 按 `user_id` 存在 Docker 容器里。

```bash
docker exec <zep-container> sqlite3 /data/zep.db \
  ".dump sessions" > zep_sessions.sql
```

然后走 SQL dump，发 `astor_write` 调用 — 模式跟 Letta 脚本一样。

### MemGPT → Astor

MemGPT 把所有东西存在单个 SQLite 文件。迁移是直接的表遍历：读 `messages`、读 `archival_memory`、每行发一个 `astor_write`，`user_id` 从 MemGPT agent config 抽取。

---

## 从 ChromaDB / Pinecone / Weaviate 迁

这些是**向量数据库**，不是 agent memory system。你有 embeddings；你没有 facts、scenarios、profiles、decay、ACL。迁来加上这些。

### ChromaDB

ChromaDB 持久化到本地目录。每个 collection 有自己的 SQLite 文件。

```python
# scripts/migrate_from_chroma.py
import chromadb
from astor_memory import write, init

init(actor="first_admin", role="first_admin")

client = chromadb.PersistentClient(path="<chroma-dir>")
for collection in client.list_collections():
    coll = client.get_collection(collection.name)
    results = coll.get(include=["documents", "metadatas", "embeddings"])

    for doc, metadata, embedding in zip(
        results["documents"], results["metadatas"], results["embeddings"]
    ):
        user_id = metadata.get("user_id") or "first_admin"
        write(
            doc,
            user_id=user_id,
            scope="long_term",
            metadata={
                "chroma_collection": collection.name,
                "chroma_id": metadata.get("id"),
                **metadata,
            },
            embedding=embedding,  # 模型匹配就传
        )
```

> **重要**：Astor-Memory 用特定模型做 embedding（默认 `BAAI/bge-base-en-v1.5`）。如果你的 Chroma collection 用别的模型，去掉 `embedding` 参数，让 Astor 在首次 recall 时重新 embed。

### Pinecone / Weaviate

这些是云 / 网络向量库。同样的模式：遍历索引，每个 vector 发 `astor_write` 带 metadata，如果模型不同可选重新 embed。

Astor 还可以**把 nest（向量存储）委派给 Pinecone 或 Weaviate**，通过 `am config nest.backend=pinecone`。这种模式下，bus + forge 还在 SQLite 里，但 embedding 存在你现有 Pinecone 索引里。已经付了 Pinecone 想保留向量的场景合适。

---

## 从普通文件或 JSON 归档迁

你有一堆 `.json`、`.yaml`、或 `.md` 文件。没有结构，没有去重，没有衰减。Astor 全给你。

### Markdown / Obsidian vault

```bash
am migrate from-files \
  --source ~/notes/alice.md \
  --user-id alice \
  --format markdown
```

Astor 在 `## `（二级标题）切分，每节当一个 fact，保留 frontmatter 作为 metadata。

### JSON Lines / array

```bash
am migrate from-files \
  --source ~/notes/alice.jsonl \
  --user-id alice \
  --format jsonl
```

每行一个 fact。如果 JSON 有 `user_id` / `created_at` 字段，Astor 用它们；否则打上你传的 `--user-id` 标签和迁移时间戳。

### YAML frontmatter（Obsidian 风格）

跟 Markdown 一样，但 frontmatter 字段直接当 metadata 不是文本。

---

## API 并排参考

### Write

| 源 | 新（astor-memory） |
|---|---|
| `mem0.add(text, user_id="alice")` | `write(text, user_id="alice")` |
| `letta.agent.block_add(value)` | `write(value, user_id=<agent.user_id>)` |
| `chroma.add(documents=[text])` | `write(text)`（chroma 没有 user 概念） |
| `json.dump({...})` 到文件 | `write(text, metadata={...})` |

### Read

| 源 | 新（astor-memory） |
|---|---|
| `mem0.search(query, user_id="alice")` | `read(query, user_id="alice")` |
| `letta.agent.block_list()` | `read(query, user_id=<agent.user_id>)` |
| `chroma.query(query_texts=[q])` | `read(q)`（chroma 返回 vectors；astor 返回带 refs 的 facts） |
| `json.load(open(file))` | `read(query, user_id=<从文件名或参数>)` |

### Health check

| 源 | 新（astor-memory） |
|---|---|
| `mem0.health()`（SaaS ping） | `am doctor` |
| `letta server status` | `am doctor` |
| `chroma.heartbeat()` | `am doctor`（覆盖 bus + forge + nest） |

---

## 回滚流程

如果切换后出问题，回滚是 3 步：

### 1. 停 Astor-Memory

```bash
am serve --stop
```

或者用 systemd：

```bash
systemctl stop astor-memory.service
```

### 2. 重启你之前的 stack

```bash
# mem0 / Letta / Zep：它们正常的启动命令
# ChromaDB：docker start <container> 或 chroma run --path <dir>
# 普通文件：不用重启；数据还在那个目录里
```

### 3. 验证老 stack 正常

```bash
curl localhost:7801/health
# → {"status": "ok", ...}
```

你的应用继续工作，因为并行期间老 call site 没改过。

### 数据回滚

Astor-Memory 数据在 `~/.astor/`。源数据在之前 stack 用的任何路径（并行期间没动过）。要回滚**数据**（不只是服务）：

```bash
# 从 astor 倒回源格式
am migrate to-<source-system> --source ~/.astor/astor.db --target <old-path>
# （这条 CLI 在计划中；现在你可以手动反着跑迁移）
```

---

## 常见坑

### 坑 1：跳过 dry-run

总是先 `--dry-run`。每个源系统有细微的 schema 差异（mem0 的 `agent_id` 字段、Chroma 的 embedding 模型、Letta 的 `block_label` 分类）你要写入前先验证。

**修**：读 dry-run 报告。查用户数、事实数、embedding 模型兼容性。

### 坑 2：把 per-user 数据混进 admin-private

如果你的源系统是单一共享 DB（普通文件、单个 Chroma collection），迁移默认 `user_id` 为 `first_admin`。如果同一个 DB 里有多个用户，运行前手动打标签。

**修**：加 `--user-id-field` 指向对的 metadata 字段，或者预处理数据注入 `user_id`。

### 坑 3：Embedding 模型不匹配

Astor-Memory 默认用 `BAAI/bge-base-en-v1.5`（768 维）做 embedding。如果你的源用别的模型（OpenAI `text-embedding-3-small`、`all-MiniLM-L6-v2` 等），embedding 直传不安全 — 相似度分算错。

**修**：(a) 去掉 `embedding` 参数重新 embed，或 (b) 配置 Astor 用同一个 embedding 模型（`am config forge.embedding_model=<your-model>`）。

### 坑 4：切太快

并行跑 1 天就切不可取。计划至少 1-2 周。注意：

- 延迟差异（Astor-Memory 进程内应该快 10-100×；如果只用 REST 反而慢）
- 引用格式变了（任何按字符串匹配老格式的代码会断）
- 依赖特定端口的 cron jobs

**修**：用 1-2 周并行跑观察和调整。

### 坑 5：忘了你原来有多个用户

如果你的源有 `user_id` 而你没意识到，迁移会默认把所有用户当成 `first_admin`。然后 `read(query)` 把他们的数据返回给 admin。

**修**：在 dry-run 里总是打印每个用户的事实数。如果数量不对，修 `--user-id-field` 重跑。

### 坑 6：把 bot-bound 数据当 facts 导入

如果你的源系统把 bot-binding 表、审计日志、运行时状态跟 facts 存在一起，这些表不属于 Astor-Memory。它们是基础设施，不是记忆。

**修**：迁移时跳过非 fact 表。在 SQL 层过滤：只对表是 fact-like（`memories`、`blocks`、`archival_memory`、`messages`）的行发 `astor_write`，不是 infra-like（`audit_log`、`bot_binding`、`sessions`）。

---

## 接下来

- [`docs/agent-adapters.md`](./agent-adapters.md) — MCP / LangChain / REST / Python 集成
- [`docs/faq.md`](./faq.md) · [中文](faq.zh-CN.md) — 常见问题
- [`docs/troubleshooting.md`](./troubleshooting.md) · [中文](troubleshooting.zh-CN.md) — 常见错误 + 修复
- [`docs/contributing.md`](./contributing.md) · [中文](contributing.zh-CN.md) — 给贡献者