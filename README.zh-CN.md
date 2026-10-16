# Astor-Memory

> **面向 AI agent 的点对点公开记忆层。** 私有记忆留在你自己的机器上;公开知识在可信节点之间点对点流通 —— 没有中心 server,没有供应商锁定。

> **English:** [README.md](README.md) · **Dashboard 文档:** [docs/dashboard.md](docs/dashboard.md) · **架构:** [docs/architecture.md](docs/architecture.md) · **API:** [docs/api.md](docs/api.md)

---

## 这是什么

每个 AI agent 都在孤立地重复学同一批东西。Astor-Memory 是一个自托管记忆
server,带点对点的公开层:你 agent 的私有记忆留在自己机器上,而你选择共享
的知识在可信 peer 网络之间 gossip 流通 —— 没有中心 server,没有一个人
独占公共知识的所有权。

一个人跑,或者加入网络。

```
[node A — 你的 server] <== gossip (仅 public 档) ==> [node B — peer 的 server]

private_<你>, source    private_<peer>, source
(永不离开本机)          (永不离开本机)
```

- **私有就是私有。** 每个用户独立的 SQLite 库;任何读写之前先在矩阵层做
  ACL 检查。`private` 和 `source` 档永不跨越 peer 边界。
- **公开就是共享。** 发布到 `public` 的 fact 会传播给你信任的 peer;
  它们的公开 fact 也能被你的 agent 读到。每 peer 信任分(0-100,默认 30)
  加每主题权重决定什么流向哪里。
- **信任要显式,不靠默认。** 未知 peer 进入隔离区等待运营者审核,而不是
  直接被拒;peer 流量按 peer 限流,避免单个吵闹节点污染公共层。

单个节点内部,同样的三档布局依然把用户分开:每人一个 SQLite 库、一套 ACL
授权、一个 bot 绑定。admin 能看到 `source`(运营专属模式),以及用户显式
授权给他的任何 `private` 档。

同一个 server 也作为外部 agent 平台的接入端点(Muse、自定义 HTTP 客户端、
Slack 适配器),提供带 ACL 用户隔离和逐次调用审计的持久记忆后端。隐私靠矩阵
层的 ACL 保证(见 [ACL 加固](docs/acl-v1.2-hardening.md)),不靠每个用户自觉。

---

## 为什么做这个

现代 AI agent 需要记忆。现有方案让你在能力与自主权之间二选一:

| 方案 | 获得 | 失去 |
|---|---|---|
| **纯 RAG**(向量库) | 简单检索 | 无事件日志、无事实抽取、无用户隔离 |
| **Letta**(Memory Blocks) | 只读保护 + 归档 | 重 runtime,严格架构 |
| **mem0**(4-tier ACL) | 多租户 + scope 标签 | 紧耦合云服务、默认异步 |
| **Astor-Memory** | 三 SQLite 库 + 三档 + ACL + 事件日志 + 审计 + PII 防御 + 衰减 + Dashboard + REST + 点对点 `public` gossip + 数据全在磁盘 | 私有档留在你自己机器上(架构保证,不是政策保证) |

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

完整 Muse 集成方案: [docs/MUSE_INTEGRATION.md](docs/MUSE_INTEGRATION.md)

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
| `/v1/peer/<peer_id>/trust` | POST | 调整信任分数 (0-100, 默认 30) |
| `/v1/peer/<peer_id>/blacklist` | POST | 屏蔽一个 peer |
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

## 点对点公开档

Peer gossip 是开箱即用的 —— 这不是路线图上的待办。新节点会向网络宣告自己,
与 peer 同步 `public` 档 fact,并把未知 peer 放进隔离区等待运营者审核。

| 操作 | 怎么做 |
|---|---|
| 添加 peer | `POST /v1/peer/add` |
| 列出 peer | `GET /v1/peer/list` |
| 调整信任 | `POST /v1/peer/<peer_id>/trust` (0-100,默认 30) |
| 屏蔽 peer | `POST /v1/peer/<peer_id>/blacklist` |
| 审核隔离区 | `GET /v1/peer/quarantine/list` → `POST /v1/peer/unquarantine` |
| 接纳待定 peer | `POST /v1/peer/adopt` |
| 观察网络 | Dashboard → peer 面板,或 `GET /v1/peer/health` |
| 限流状态 | `GET /v1/peer/rate-limit` |

硬边界:只有 `public` 会 gossip。`private` 和 `source` 永不离开本节点,
跟信任分多高无关。进来的 peer 流量按 peer 限流,未被你批准的 peer 待在
隔离区里,无法写入。

**60 秒上手:** 本地起两个节点,互相添加为 peer,在节点 A 上 `POST /v1/write`
一条 `"tier": "public"` 的 fact,然后在节点 B 上 `/v1/read` 出来。

---

## 为什么点对点,而不是中心记忆服务

1. **没有需要信任或付费的中心 server。** 共享的 agent 知识不该寄居在别人
   的 SaaS 上。自托管节点之间的 gossip 让公共知识的所有权留在参与者手里。
2. **靠架构保证隐私,不靠政策。** 共享是一次显式的 publish 到 `public`,
   按 peer、按主题 —— 绝不是把记忆一次性全量上传。
3. **网络本身就是护城河。** 每加一个节点,其他所有节点的公开召回都更丰富。
   中心化的记忆产品做不到这点,除非它变成所有人数据的托管者。

单个节点内部的运维面刻意保持很小:一个 server 进程、一个 `ASTOR_DIR`、
一份备份。跑多节点是可选的,而且每个节点都独立完整。

---

## 贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

MIT —— 见 [LICENSE](LICENSE)。

## 维护者

Astor-Memory Maintainers —— 见 [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md)。