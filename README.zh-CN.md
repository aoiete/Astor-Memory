# Astor-Memory (中文)

> **AI 智能体的自托管记忆系统。** 三个存储,三层隔离,零供应商锁定。

---

## 为什么我们做这个

现代 AI 智能体需要记忆。现有的方案都强迫你在"能力 vs. 自主权"之间二选一:

| 方案 | 你得到 | 你失去 |
|---|---|---|
| **纯 RAG** (向量库) | 简单的检索 | 没有事件日志、没有事实抽取、没有用户隔离 |
| **Letta** (Memory Blocks) | 只读保护 + 归档 | 运行时重、架构强约束 |
| **mem0** (4级 ACL) | 多租户 + scope 标签 | 强耦合云端、默认异步 |
| **PowerContext / PowerMem** | 搜索↔上下文包分离 | 1.0 之前、中文优先、不能自托管 |
| **自己造** (chromadb + memu.ai SDK) | 完全控制 | 3 GB 虚拟环境、3 个服务器进程、升级脆弱 |

**Astor-Memory** 存在是因为我们在 33 次 ship 周期和 50+ 定时任务的自托管智能体中遇到了所有四个痛点。我们学到的:

1. **三个存储是可行的最小分解**。一个仅追加的事件日志 (`bus`)、一个 LLM 事实抽取器 (`forge`)、一个向量库 (`nest`),清晰对应"发生了什么 → 该记住什么 → 该召回什么"。更多层会增加协调成本;更少层会混叠语义。
2. **三层隔离匹配真实 ACL 需求**。公共知识 (skills、rules) + 管理员私有 (智能体可见,用户不可见) + 每用户私有 (N 个隔离数据库) — 不多不少。
3. **供应商锁定是无声的杀手**。chromadb 迁移、memu.ai SDK 破坏性变更、transformers 吃掉 3 GB 虚拟环境 — 我们选的每个依赖都在 6 个月内反噬我们。教训:不拥有代码,就拥有风险。

如果你对以上任何一个有共鸣,Astor-Memory 就是为你造的。

---

## Astor-Memory 的不同之处

| 差异化点 | 含义 |
|---|---|
| **3-store 三元组** | `bus` (仅追加事件日志) + `forge` (LLM 事实抽取) + `nest` (向量库)。每个存储各司其职。 |
| **3 层隔离** | `public` + `source` (管理员私有) + `private × N` (每用户私有)。一条命令开启多用户模式。 |
| **自拥有代码** | 纯 Python + SQLite + NumPy。无 chromadb、无 memu.ai SDK、无 transformers、无 torch。安装体积 < 50 MB。 |
| **供应商中立的 LLM** | `forge` 支持 OpenAI / Anthropic / Gemini / DeepSeek / 智谱 / Ollama。同样的召回输出,任意供应商。 |
| **引用优先** | 每个上下文包都嵌入 `<ref memory_id revision_id>`,智能体可以验证读到的内容。 |
| **自我演进的生命周期** | 艾宾浩斯式衰减 + 余弦合并 + 出现 3 次即晋升为规则。智能体主动遗忘、合并、把事实毕业为规则。 |
| **仅追加 + 修订追踪** | 更新产生新修订;旧内容仍可查询以备审计。不会静默覆盖。 |
| **跨 LLM 适配器** | 在 Qwen2.5-7B 上训练的记忆策略对 GPT-5-mini 仍然有效 (+16 pp vs RAG +4.3 pp)。来自 Mem-π 论文的洞察。 |

## 三个存储,三层隔离 (60 秒看懂架构)

```
┌─────────────────────────────────────────────────────────────┐
│                      astor_memory                           │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │     bus      │  │    forge     │  │     nest     │      │
│  │  (events)    │─→│ (extraction) │─→│   (vector)   │      │
│  │              │  │              │  │              │      │
│  │  SQLite WAL  │  │  cloud LLM   │  │ SQLite+numpy │      │
│  │  append-only │  │  async       │  │ kNN brute    │      │
│  └──────────────┘  └──────────────┘  └──────────────┘      │
│         │                  │                  │             │
│         └──────────────────┴──────────────────┘             │
│                            │                                │
│                   ┌────────▼─────────┐                      │
│                   │  3-tier ACL      │                      │
│                   │ public / source  │                      │
│                   │ / private × N    │                      │
│                   └──────────────────┘                      │
└─────────────────────────────────────────────────────────────┘
```

- **`bus`** 记录发生的所有事情。仅追加 SQLite + WAL。时序存储。
- **`forge`** 通过云端 LLM 把原始事件转为结构化事实。异步,所以写入不阻塞。
- **`nest`** 把事实索引为向量。SQLite + NumPy 暴力 kNN — 5K 文档以内够快 (1 ms 延迟);HNSW 推迟到 v2.0。
- **3 层 ACL** 包裹三个存储。`public` 是共享知识;`source` 是管理员私有 (智能体可见,终端用户不可见);`private × N` 是每个用户一个数据库。

单用户模式 = `public + self-private`。多用户模式 = `am bot on` 按需创建 `private × N`。

### Bots (多平台) 设计哲学

astor 把**人** (`user_id`) 和 **bots** (`platform_id`) 当作两个独立的维度。关系是真正的多对多:

- 1 个人可以有 N 个 bots (例如手机上用 TG + 桌面上用 DC + 朋友用 WX,都绑定同一个 user_id)
- 1 个 bot 可以服务 M 个人 (例如一个微信 bot,12 个朋友各发独立私信,每个 chat_id 绑定到不同的 user_id)

所以 `bot-binding.db` 有**两张独立的表**:

- `platforms` — 每个 bot 的配置 (token、base_url、enabled)
- `bindings` — 每个 chat_id → user_id 映射

不是合并成一张表,因为关系是独立的。

**为什么微信特殊 (1 chat = 1 user 通常情况)**:微信协议只允许 1:1 私信,所以单个微信 bot 实例通过独立的 DM 私信服务多个用户。每个 DM 的 `bindings` 绑定到一个 `user_id`。

**Telegram / Discord 是 1:N (一个 bot,多个用户)**:两个平台都支持一个 bot token 下多个并行聊天。一个 TG bot 映射到多个 binding,每个 binding 一个不同的 chat_id。

**bot 进程对私有数据没有特殊权限**。一旦绑定建立,bot 只是传输工具:

    Telegram DM (chat_id=C, 绑定到 user_id=alice)
      -> astor_init_acl(actor=user:alice, role=user, tier=private_alice)
      -> acl_check_read 通过
      -> 读/写 alice 的私有 DB

如果 bob 的 chat_id D 发起对 alice 私有的读请求:

      -> astor_init_acl(actor=user:bob, role=user, tier=private_alice)
      -> acl_check_read 拒绝 (user_id 不匹配)
      -> 401 需要 user grant (严格隐私模型 2026-08-16)

参见 [`bots/README.md`](bots/README.md) 了解完整处理 (反模式、四种典型场景、为什么用两张表)。

## 快速开始

### 安装

```bash
pip install astor-memory
```

要求:Python 3.10-3.13,<50 MB 依赖。

### 首次运行 (单用户模式)

```bash
am init                # 创建 ~/.astor/{public,source,private_admin}/
am doctor              # 健康检查 — schema、计数、绑定完整性
am write "我喜欢用中文交流" --tier private --user-id admin
am recall "用户偏好" --top-k 5
```

### 体验多用户模式

```bash
am bot on
am bot add-user alice
am bot add-user bob
am write "alice 的偏好..." --tier private --user-id alice
am write "bob 的偏好..."   --tier private --user-id bob
```

每个用户在自己的 DB (`users/<id>/memory/astor_*_<id>.db`) 中,隔离由 3 层 ACL 强制。

## 你实际得到什么

```
~/.astor/                                  # = $ASTOR_DIR (可覆盖)
├── public/memory/                         # 3 层 × 3 store 的公共 tier
│   ├── astor_bus_public.db
│   ├── astor_forge_public.db
│   └── astor_nest_public.db
├── source/memory/                         # admin-only tier (智能体 + 管理员可见)
│   ├── astor_bus_source.db
│   ├── astor_forge_source.db
│   └── astor_nest_source.db
├── users/                                 # 每用户私有 DB
│   ├── admin/memory/{astor_bus_admin.db, astor_forge_admin.db, astor_nest_admin.db}
│   ├── alice/memory/...
│   └── bob/memory/...
├── audit/                                 # 审计 + 跨用户授权
│   ├── astor_audit.db                    # 所有 private/source 读写的追加日志
│   └── astor_grants.db                   # 跨用户私有访问授权 (严格隐私模型)
├── bot-binding.db                         # bot 配置 + token + chat_id→user_id
└── install-state.json                     # 多用户模式标记
```

**首次安装完全是空的**。没有任何种子用户、平台或绑定 — 你自己用 `am platform token-set` 和 `am bot add-user` 填入你自己的真实数据。

## 一屏看懂 CLI

| 命令 | 做什么 |
|---|---|
| `am init` | 引导空白 ASTOR_DIR (schema + 9-DB 布局) |
| `am doctor` | 健康检查 + 计数 + 不变量校验 |
| `am write "<text>"` | 写入一条事实 (默认 tier=public) |
| `am read "<query>"` | 召回 top-k 事实 |
| `am compact` | 合并近似重复的事实 |
| `am recall --with-citations` | 输出带 `<ref>` 标记的上下文包 |
| `am bot on/off` | 切换多用户模式 |
| `am bot add-user <id>` | 创建用户 + 他们的私有 DB |
| `am platform token-set <kind> <token>` | 保存 bot token (审计行) |
| `am platform bind <bot> <chat> <user>` | 把一个 chat 绑定到用户 |
| `am platform verify` | 检查 6 条 bot-binding 不变量 |
| `am migrate --from memory-bus --source <path>` | 从旧 memory-bus 系统导入 |
| `am version` | 打印 astor + python + 平台版本 |

完整列表 + 每个子命令的细节:[`docs/api.md`](docs/api.md)。

## 文档导航

| 文档 | 给谁看 |
|---|---|
| **[`docs/architecture.md`](docs/architecture.md)** | 理解 3-store × 3-tier 的"为什么"。必读。 |
| **[`docs/api.md`](docs/api.md)** | REST 端点 + 请求/响应 schema (18 个端点) |
| **[`docs/contributing.md`](docs/contributing.md)** | 修代码、提 PR、写 skills |
| **[`docs/migration.md`](docs/migration.md)** | 从 memory-bus / mem0 / chroma 升级 |
| **[`docs/troubleshooting.md`](docs/troubleshooting.md)** | 常见错误 + 自助调试 |
| **[`docs/faq.md`](docs/faq.md)** | "X 和 Y 有什么不同?" |
| **[`docs/agent-adapters.md`](docs/agent-adapters.md)** | 集成到 hermes / OpenClaw / Claude Desktop / Cursor |
| **[`bots/README.md`](bots/README.md)** | 多平台 bot 绑定 + ACL 设计 |

中文文档:`README.zh-CN.md` (本文)。架构和 API 的中文翻译版本维护在 [`docs/architecture.zh-CN.md`](docs/architecture.zh-CN.md) 和 [`docs/api.zh-CN.md`](docs/api.zh-CN.md)。

## 一眼架构

9-DB 布局来自两个轴的笛卡尔积:

- **3 个空间层 (tier)**:public / source / private_<user_id>
- **3 个存储 (store)**:bus (事件 + 规范事实) / forge (LLM 抽取缓存) / nest (向量 + 全文索引)

= **3 × 3 = 9 个 SQLite 文件**,加 audit db 和 bot-binding db。

**写路径**:

```python
from astor_memory import astor_bus
astor_bus(user_id='alice').write(
    text='alice 喜欢浓缩咖啡',
    tier='private',           # 强制 ACL
)
# 立即返回 event_id;forge 异步抽取事实;nest 异步嵌入。
```

**读路径**:

```python
hits = astor_bus(user_id='alice').read(
    query='咖啡偏好',
    top_k=5,
)
# 返回带引用的 hit 列表:
# [0.92] alice 喜欢浓缩咖啡
#   ref: f_8a3b2c1d:rev_1
#   conf: 0.94
```

详见 [`docs/architecture.md`](docs/architecture.md)。

## 与现有方案对比

| 维度 | Astor-Memory | mem0 | Letta | PowerContext |
|---|---|---|---|---|
| 自托管 | ✅ 纯本地 | ❌ 云耦合 | ✅ 自托管 | ❌ 中文优先,无 self-host |
| 多用户 ACL | ✅ 3 层 | ✅ 4 级 | ⚠️ 单租户 | ⚠️ profile 范围 |
| LLM 供应商中立 | ✅ 6 个供应商 | ❌ OpenAI 优先 | ✅ 多供应商 | ⚠️ 主要 GPT |
| 事件日志 | ✅ 追加式 | ❌ 仅事实 | ⚠️ 块级 | ✅ |
| 引用追踪 | ✅ 修订 + 血缘 | ❌ | ⚠️ 块 ID | ✅ |
| 安装体积 | < 50 MB | ~80 MB (chroma) | ~200 MB | ~120 MB |
| Python 版本 | 3.10-3.13 | 3.10+ | 3.11+ | 3.10+ |

## 运行铁律

完整的 15 条铁律在 [`docs/contributing.md`](docs/contributing.md)。最关键的 4 条:

1. **P-NO-FABRICATE-026** — 找不到事实时返回"无数据",绝不编造。
2. **P-CITATION-015** — 每个 recall() 输出必须带 `<ref memory_id:revision_id>`。
3. **P-DEDUPE-014** — cosine ≥ 0.85 的事实合并 (revisions 保留)。
4. **P-NOSECRET-020** — 永不把 token / 密码 / PII 写入记忆。

## 贡献指南

欢迎贡献!流程在 [`docs/contributing.md`](docs/contributing.md),但 TL;DR:

1. Fork → 特性分支 (`feat/<scope>/<topic>`)
2. pytest 必须全绿 (`pytest tests/`)
3. 更新 `docs/api.md` (如果你加了端点) 或 `docs/architecture.md` (架构改动)
4. CHANGELOG.md 加一行 (`### Added/Fixed/Changed`)
5. PR → main;CI 通过 → review → merge

## 许可证

MIT — 详见 [LICENSE](LICENSE)。

## 致谢

- 灵感来自 Mem-π、PowerContext RFCs、CoALA 框架、A-MEM 论文、Anthropic / Google ADK 风格指南。
- 项目维护者:**flopworld with AI** ([ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md))。
- 由 33 次 ship 周期和 50+ 定时任务的生产经验驱动。

---

如果你 fork 这份代码:
1. `README.md` (本文) 是入口。
2. `docs/architecture.md` 解释**为什么**每一部分存在。
3. `bots/README.md` 解释**为什么**bot 设计成 1×N×M 多对多。
4. 每个模块顶部的 docstring 描述模块用途,公共函数 100% 有 docstring (`pydocstyle astor_memory`)。
5. `tests/` 是行为规范 — 任何 PR 必须保持 pytest 全绿。
