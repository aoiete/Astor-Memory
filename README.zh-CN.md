# Astor-Memory

> **自托管的 AI agent 记忆系统。** 三库三档,联邦 public 档,零供应商锁定。

> **English:** [README.md](README.md) · **Dashboard 文档:** [docs/dashboard.md](docs/dashboard.md) · **架构:** [docs/architecture.md](docs/architecture.md) · **API:** [docs/api.md](docs/api.md)

---

## 这是什么

一个单机 SQLite 后端的记忆层,为共享同一个 bot 的可信小群体(家人、朋友、共同管理员)服务。每个用户拥有自己的 private 档可读写;运营者维护一个 `source` 档存放运营专属模式;所有人共享一个 `public` 档作为跨用户知识。

同一个 server 同时充当外部 agent 平台(Muse、自定义 HTTP 客户端、Slack 适配器)接入的 spoke endpoint,提供持久记忆后端 + ACL 强制用户隔离 + 每次调用审计。

```
+---------------------------------------+
|  一个 astor-memory server (端口 7803)  |
|                                        |
|   first_admin    first_admin 档        |
|   mom            private_mom 档        |
|   friend_a       private_friend_a 档   |
|   friend_b       private_friend_b 档   |
|   cousin            private cousin 档    |
|                                        |
|   共享记忆 = public + source           |
|   个人记忆 = private_<user_id>         |
+---------------------------------------+
```

每个用户拥有自己的 SQLite 数据库、自己的 ACL 授权、自己的 bot binding。admin 可见 `source` (运营专属模式) 加上任何用户明确授权的 `private` 档。

这不是一个多租户 SaaS。它是一个单机记忆,为足够信任彼此去共用 bot 的小群体服务。隐私在矩阵级层面([ACL 加固文档](docs/acl-v1.2-hardening.md))强制执行,而不是靠信任每个用户自觉。

---

## 为什么做这个

现代 AI agent 需要记忆。现有方案让你在能力与自主权之间二选一:

| 方案 | 获得 | 失去 |
|---|---|---|
| **纯 RAG**(向量库) | 简单检索 | 无事件日志、无事实抽取、无用户隔离 |
| **Letta**(Memory Blocks) | 只读保护 + 归档 | 重 runtime,严格架构 |
| **mem0**(4-tier ACL) | 多租户 + scope 标签 | 紧耦合云服务、默认异步 |
| **Astor-Memory** | 三 SQLite 库 + 三档 + ACL + 事件日志 + 审计 + PII 防御 + 衰减 + dashboard + REST + 开放磁盘数据 | 单机范围(不水平分片) |

实际获得:

- **Bus / Forge / Nest** 三库架构,支持跨档晋升(per-user → source / public)。
- **ACL 矩阵** 在 per-actor × per-tier 单元格级别,任何数据库读写前强制门控。
- **事件日志** —— 每条事实写入作为不可变事件追加,带 provenance(来源平台、agent、kind)。可重放。
- **PII 防御** —— 44 模式扫描器(API key、email、电话、chat ID、token)接入 write endpoint,带 `redact` 和 `block` 策略。审计安全用 `sha256[:12]` 指纹。
- **衰减 + HOT 晋升** —— 从不召回的事实衰减更快;召回命中 3+ 次的事实晋升 HOT 获得有上限的相关性加成。运营作者面(mental_model、knowledge_page)排除在衰减外。
- **记忆召回组合** —— lexical(BM25) + vector(多语 embed) + MMR 多样性 rerank + HOT 提升 + 跨档晋升 + ECV 链提升 + meta-recall(success / failure / lesson 模式自动注入每次 read)。每个环境变量可调。
- **Dashboard** —— 实时健康、最近捕获面板(按 kind / tier / platform 轴分页)、peer-friends 面板、Recall debugger 带实时 /v1/read 回显、Knowledge Pages 面板、Mental Models 面板、增长 + 分布聚合。
- **公开 REST** —— `/v1/{health, identity, dashboard, write, read, consult, skill, peer, binding, episode, bitemporal, audit, staleness, forget}` 加上 dashboard HTML 在 `/dashboard/`。

---

## 部署形态

一个 server、一个端口(默认 7803)、磁盘上一个 `ASTOR_DIR` 目录:

```
<ASTOR_DIR>/
├── public/memory/      # astor_bus_public.db + astor_nest_public.db
├── source/memory/      # astor_bus_source.db + astor_nest_source.db
├── users/<id>/memory/  # astor_bus_<id>.db + astor_nest_<id>.db  (每个用户)
├── audit/              # audit_log.sqlite (跨档)
├── logs/                 # server.log + side-log + watch logs
└── identity/            # keypair.json (首次启动自动生成)
```

Server 默认绑定 `127.0.0.1:7803`。远程访问请用 Cloudflare Tunnel、nginx 或 peer-anything 反向代理 —— server 不会绑定 `0.0.0.0`,除非你显式传 `--host 0.0.0.0`。

通过 `pip install astor-memory` 安装,之后 `astor-server` 成为 console script。可选 `[muse]` extra 安装 Muse 适配器包。

---

## 快速开始

```bash
# 安装
pip install astor-memory
# 或带 Muse 适配器
pip install astor-memory[muse]

# 初始化全新 runtime
export ASTOR_DIR=/var/lib/astor
mkdir -p "$ASTOR_DIR"
python -m astor_memory.cli.main init

# 添加 admin 用户
python -m astor_memory.cli.main user add admin --role first_admin

# 启动 server
astor-server --host 127.0.0.1 --port 7803

# 健康检查
curl http://127.0.0.1:7803/v1/health
# { "status": "ok", "astor_dir": "<dir-name>", "dbs": {"bus":"ok","nest":"ok"}, ... }

# 写一条事实 (admin)
curl -X POST http://127.0.0.1:7803/v1/write \
     -H 'Content-Type: application/json' \
     -d '{"text":"my favorite color is teal","user":"admin","tier":"private"}'

# 读取带召回
curl -X POST http://127.0.0.1:7803/v1/read \
     -H 'Content-Type: application/json' \
     -d '{"query":"favorite color","user":"admin","tier":"private","top_k":5}'
```

完整 endpoint 参考: [docs/api.md](docs/api.md)
每库 schema 和 ACL 矩阵: [docs/architecture.md](docs/architecture.md)

---

## Muse 与外部 agent 平台集成

外部 agent 平台(Muse、自定义 HTTP 客户端、Slack 适配器、Discord relay bot)通过 **binding API** 接入。流程分四步 —— 都走 `POST /v1/binding/*`:

### 1. 注册平台

```bash
curl -X POST http://127.0.0.1:7803/v1/binding/platform \
     -H 'Content-Type: application/json' \
     -d '{
       "platform_id": "muse_main",
       "kind": "muse",
       "endpoint": "https://muse.example.com",
       "auth_token_ref": "env:MUSE_AESK"
     }'
```

Server 在 `platform_id` 下存储平台。`auth_token_ref` 是一个指针(`env:<NAME>` 或 `vault:<path>`);真实 token 永远不进数据库。

### 2. 注册平台说话的用户

```bash
curl -X POST http://127.0.0.1:7803/v1/binding/user \
     -H 'Content-Type: application/json' \
     -d '{
       "user_id": "alice",
       "role": "user",
       "subscription_plan": "free",
       "platform_id": "muse_main"
     }'
```

`role` ∈ {`first_admin`, `admin`, `vip`, `power`, `user`}。role 决定 ACL 授权范围:`first_admin` 和 `admin` 可见 `source`,`vip` 和 `power` 可见 `public` 加上自己的 `private`,`user` 可见 `public` 加上自己的 `private`。

### 3. 把聊天会话绑给用户

```bash
curl -X POST http://127.0.0.1:7803/v1/binding/bind \
     -H 'Content-Type: application/json' \
     -d '{
       "platform_id": "muse_main",
       "chat_id": "session-uuid-abc123",
       "user_id": "alice",
       "role_inherit": "user"
     }'
```

返回当前激活绑定;同一个 `chat_id` 的后续调用自动解析为 `user_id=alice`。Binding 通过 `POST /v1/binding/lookup` 用 `{platform_id, chat_id}` 查询 —— 这是每一层服务的规范 "这是谁?" 解析器。

### 4. 通过平台范围的 endpoint 读写

```bash
# 作为 Alice 写事实 (从 chat_id 解析)
curl -X POST http://127.0.0.1:7803/v1/write \
     -H 'Content-Type: application/json' \
     -d '{
       "platform_id": "muse_main",
       "chat_id": "session-uuid-abc123",
       "text": "alice prefers dark roast coffee",
       "kind": "fact"
     }'

# 带完整召回组合的 read
curl -X POST http://127.0.0.1:7803/v1/read \
     -H 'Content-Type: application/json' \
     -d '{
       "platform_id": "muse_main",
       "chat_id": "session-uuid-abc123",
       "query": "coffee preferences",
       "top_k": 5
     }'
```

Server 通过 binding lookup 解析 `platform_id + chat_id` 到 `user_id`,然后对该用户强制 ACL。档路由在 server 端 —— Muse 不选档;server 把 `platform_id + user_role` 映射到合适的档组合。

### Skill chaining

外部平台可以通过 `POST /v1/skill/chain` 运行多 skill pipeline:

```bash
curl -X POST http://127.0.0.1:7803/v1/skill/chain \
     -H 'Content-Type: application/json' \
     -d '{
       "platform_id": "muse_main",
       "chat_id": "session-uuid-abc123",
       "skills": ["coref_resolve", "match_experiences", "consult"],
       "context": {"text": "alice said she is moving to Berlin next month"}
     }'
```

每个 skill 通过线程化 `context` dict 看到前一个 skill 的输出。结果包含每个 skill 的耗时 + 链中触发的任何 meta-recall lesson。

### 平台的审计 + admin endpoint

```bash
# 列出所有 binding (仅 admin)
curl http://127.0.0.1:7803/v1/binding/list

# 每档事件审计
curl "http://127.0.0.1:7803/v1/audit/health?user=alice"

# PII gate 统计 (生命周期)
curl http://127.0.0.1:7803/v1/audit/health
```

完整 Muse 集成方案: [docs/integration-muse.md](docs/integration-muse.md)

---

## Dashboard

Dashboard 从同一个端口的 `/dashboard/` 服务。功能包括:
  - 健康面板 —— embedding 失败、审计警告、审计总数
  - 概要面板 —— 总事实数、活跃事实数、最近事件
  - 每用户分布 —— 事实数、高重要性、最近事件、tombstoned
  - 最近捕获 —— 分块分页、每页 5 行、可切换轴(kind / tier / platform / all-flat)
  - 最近事实 —— 最新 5 条
  - Knowledge pages —— 运营作者主题页
  - Mental models —— 运营作者召回面
  - Recall debugger —— 实时 /v1/read 带 tier / user / top_k 控制
  - Peer friends 面板 —— list / trust / blacklist / 每 peer 复制
  - 自动刷新每 60 秒、缓存 TTL 30 秒、原始 JSON endpoint 在 `/v1/dashboard`

Dashboard HTML 在公开 repo 中作为通用模板发布;本地实例是运营者自己的。API 返回的路径字符串被遮蔽(只暴露目录 basename 在 `/v1/health`;sqlite 文件路径在任何公开 API 表面都不暴露)。

---

## 公开 API 表面

| Endpoint | Method | 用途 |
|---|---|---|
| `/v1/health` | GET | Server 健康 + bus 统计 |
| `/v1/identity` | GET | Server peer_id + fingerprint |
| `/v1/dashboard` | GET | 完整 dashboard payload (JSON) |
| `/v1/health/diagnose` | GET | 每用户详细健康分布 |
| `/v1/write` | POST | 追加事实 (带 ACL + PII gate) |
| `/v1/read` | POST | 带跨信号组合的召回 |
| `/v1/forget` | POST | Tombstone 一条事实 |
| `/v1/consult` | POST | 反应式 meta-recall (success / failure / lesson) |
| `/v1/skill` | GET | 列出已注册 skill |
| `/v1/skill/<name>` | GET | Skill 元数据 |
| `/v1/skill/<name>/invoke` | POST | 运行一个 skill |
| `/v1/skill/chain` | POST | 按序运行多个 skill |
| `/v1/skill/recommend` | POST | 主动 skill + 事实推荐 |
| `/v1/binding/platform` | POST | 注册外部平台 |
| `/v1/binding/user` | POST | 注册用户 |
| `/v1/binding/bind` | POST | 绑定聊天会话到用户 |
| `/v1/binding/lookup` | GET | 解析 chat_id → user_id |
| `/v1/binding/list` | GET | 列出所有 binding (admin) |
| `/v1/peer/list` | GET | 列出 peer-friends (PPS) |
| `/v1/peer/add` | POST | 添加 peer-friend |
| `/v1/peer/trust` | POST | 调整信任分数 |
| `/v1/peer/blacklist` | POST | 屏蔽一个 peer |
| `/v1/episode` | POST | 追加原始 episode (L0 cone) |
| `/v1/episode/<id>` | GET | 按 id 获取 episode |
| `/v1/episode/list` | GET | 列出最近 episode |
| `/v1/bitemporal/invalidate` | POST | 让一条事实失效,带原因 |
| `/v1/bitemporal/active` | POST | 把事实重新标记为 active |
| `/v1/audit/health` | GET | PII gate + meta-recall 计数器 |
| `/v1/audit/orphans` | GET | 列出低信号候选用于清理 |
| `/v1/backfill_memory_class` | POST | 一次性 backfill (admin) |
| `/v1/staleness` | GET | 查找需要刷新的引用 |

完整请求 / 响应 schema: [docs/api.md](docs/api.md)

---

## 联邦公开档 (P5)

多个由可信 peer 拥有的 astor 实例将直接同步它们的 `public` 档 —— 没有中心 server,没有远程-direct RPC。每 peer 信任(0-100) + 每主题权重让策划 admin 决定跟谁分享什么。`private` 和 `source` 档永远不跨边界。

这作为 `/v1/peer/*` REST endpoint 加上 `am peer` CLI 发布在现有 `peer_relationships.py` schema 之上。P5 在运营方 pilot 达到收敛时落地。

---

## 为什么是单机而不是第一天就联邦

三个理由:

1. **运维简单。** 一个 `astor-server` 进程、一个 `ASTOR_DIR` 目录、一个 backup cron。多 server 分片让运维表面积翻三倍,加上只在生产环境出现的事件一致性 bug 类。
2. **ACL 是信任边界。** 强制每用户隐私的同一个 ACL 矩阵在多 server 部署中照样工作 —— 联邦只需要每 server 信任,不需要重写访问控制。
3. **每 peer 分享对实际部署形态已经够用。** 一个家人 / 朋友 / 共同管理员群体很少需要跨组织分享。需要时,`peer_relationships.py` 里的每 peer 信任 + 每主题权重机制就是覆盖用例的最小扩展,不需要重新架构。

---

## 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

MIT —— 见 [LICENSE](LICENSE)。

## 维护者

Astor-Memory Maintainers —— 见 [AUTHORS.md](AUTHORS.md)。