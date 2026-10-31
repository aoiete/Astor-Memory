# Astor-Memory

[![CI](https://github.com/aoiete/ASTOR-Memory/actions/workflows/ci.yml/badge.svg)](https://github.com/aoiete/ASTOR-Memory/actions/workflows/ci.yml)
[![link-check](https://github.com/aoiete/ASTOR-Memory/actions/workflows/link-check.yml/badge.svg)](https://github.com/aoiete/ASTOR-Memory/actions/workflows/link-check.yml)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> **面向 AI agent 的点对点公开记忆层。** 私有记忆留在你自己的机器上;公开知识在可信节点之间点对点流通 —— 没有中心 server,没有供应商锁定。

> **English:** [README.md](README.md) · **Dashboard 文档:** [docs/dashboard.md](docs/dashboard.md) · **架构:** [docs/architecture.md](docs/architecture.md) · **API:** [docs/api.md](docs/api.md)

---

## 这是什么

每个 AI agent 都在孤立地重复学同一批东西。Astor-Memory 是一个自托管记忆
server,带点对点的公开层:你 agent 的私有记忆留在自己机器上,而你选择共享
的知识在可信 peer 网络之间 gossip 流通 —— 没有中心 server,没有一个人
独占公共知识的所有权。

```
[node A — 你的 server] <== gossip (仅 public 档) ==> [node B — peer 的 server]

private_<你>, source    private_<peer>, source
(永不离开本机)          (永不离开本机)
```

### 1. 一台机器,多个用户,多个 bot

不只是单人安装。一个节点可以同时服务多个用户:每人一个独立的 SQLite 库、
一套独立的 ACL 授权、自己的 bot 绑定(Telegram / Discord / 微信 / 飞书 /
MCP 客户端 / 普通 HTTP)。ACL 在矩阵层拦截每一次读写 —— 用户之间互相看不见,
某个用户的 bot 也摸不到另一个用户的层。admin 能看到 `source`(运营专属模式),
以及用户显式授权给他的任何 `private` 档。

### 2. public 与 private 是两回事,而且代码知道这件事

分层是在存储层强制的,不是靠约定:**personal 的 fact 根本进不了 public 档**。
进公共档的是可共享的那部分 —— 方法、模式、模型、流程、教训、成功与失败;
私人的东西留在 owner 自己的 private 档,出门之前路径和标识符被清洗过。
蒸馏器(private 档挖掘)从私有层里找出值得发布的部分,**不会漏**。

### 3. 节点之间互搜公共档

节点可以搜它信任的对端的公共记忆,结果回来的方式和本地搜索一样。一个节点上
找到的方法、模型、模式,在别的节点上也能用 —— 这就是这张网络的意义。

信任要显式,不靠默认:未知 peer 进入隔离区等待运营者审核,而不是直接被拒;
peer 流量按 peer 限流,避免单个吵闹节点污染公共层。每 peer 信任分
(0-100,默认 30)加每主题权重决定什么流向哪里。

### 4. NAT 后面的节点,有中继

两个互相够不着的节点,可以通过中继说话。中继是邮差不是读者:消息在到达
之前就封好了,它既读不了,也没有任何办法去联系第三方。公共中继可以在完全
看不见内容的情况下转发流量。

### 接入方式

CLI(`am`)、REST(`/v1/...`)、MCP 工具,还有一个 dashboard。同一个 server 也
作为外部 agent 平台的接入端点(Muse、自定义 HTTP 客户端、Slack 适配器),
提供带 ACL 用户隔离和逐次调用审计的持久记忆后端。隐私靠矩阵层的 ACL 保证
(见 [ACL 加固](docs/acl-v1.2-hardening.md)),不靠每个用户自觉。

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

磁盘上只有两个位置:**代码**和**数据**。分开是因为两者寿命不同 —— 代码随
`git pull` 变,数据每写一条 fact 就变一次,而备份记忆不该把代码一起打包。

```
代码:  Astor-Code/
      你 clone 的目录;非 editable 安装则在 site-packages 里

数据:  Astor-Data/
      默认是代码目录的同级兄弟目录;设 ASTOR_DIR 可以放到任意位置
```

`ASTOR_DIR` 里不放代码,永远不放。server 从 Python import 到哪里就从哪里
解析自己的包,所以数据目录和代码目录不会对"现在跑的是哪个版本"产生分歧。

```
<ASTOR_DIR>/
├── public/memory/      # astor_bus_public.db + astor_nest_public.db
├── source/memory/      # astor_bus_source.db + astor_nest_source.db
├── users/<id>/memory/  # astor_bus_<id>.db + astor_nest_<id>.db  (每个用户)
├── lex/memory/         # 词法索引,按 scope 分
├── audit/              # audit_log.sqlite (跨档)
├── logs/                 # server.log + side-log + watch logs
├── relay/                # relay 状态 (封禁名单、审计元数据)
└── identity/            # keypair.json (首次启动自动生成)
```

`am init` 首次运行时会让你选数据目录,并记在 `~/.astor/install.json`,
所以下次安装不用重新选。

Server 默认绑定 `127.0.0.1:7803`。远程访问请用 Cloudflare Tunnel、nginx 或 peer-anything 反向代理 —— server 不会绑定 `0.0.0.0`,除非你显式传 `--host 0.0.0.0`。

尚未发布到 PyPI,从源码安装(一条命令),之后 `am` 成为 console script:

```bash
pip install "astor-memory @ git+https://github.com/aoiete/Astor-Memory.git"
```

可选 `[muse]` extra 安装 Muse 适配器包。发布到 PyPI 后会改成
`pip install astor-memory`。

需要 Python 3.10–3.14。

---

## 从Claude / Cursor 使用 (MCP)

三个工具，不需要 API key，不走 HTTP：

```
astor_recall   跨 public / source / private 档做语义召回
astor_write    写入事实 → bus event → candidate → canonical
astor_status   只返回计数，绝不返回事实内容
```

```bash
am mcp serve          # stdio 传输，立刻启动
```

**Claude Desktop** —— 加到 `claude_desktop_config.json`
(macOS 在 `~/Library/Application Support/Claude/`，
Windows 在 `%APPDATA%\Claude\`)：

```json
{
  "mcpServers": {
    "astor-memory": {
      "command": "am",
      "args": ["mcp", "serve"]
    }
  }
}
```

**Claude Code**：

```bash
claude mcp add astor-memory -- am mcp serve
```

**Cursor** —— 项目里的 `.cursor/mcp.json`：

```json
{
  "mcpServers": {
    "astor-memory": {
      "command": "am",
      "args": ["mcp", "serve"]
    }
  }
}
```

如果 `am` 不在 PATH 里，用安装时生成的 console script 绝对路径
（`python -c "import shutil; print(shutil.which('am'))"`）。

`astor_recall` 会搜索调用者可读的所有档位，并给每条结果标注所属档位。
`astor_write` 显式接受 `"tier"`，默认 `public`；传 `"tier": "private"`
配合 `"user"` 可写入某个账号的私有库。两个方向上 ACL 都会拒绝跨用户访问，
客户端碰不到别人的记忆。

---

## 快速开始

```bash
# 安装(尚未发布到 PyPI,从源码装)
pip install "astor-memory @ git+https://github.com/aoiete/Astor-Memory.git"
# 或带 Muse 适配器
pip install "astor-memory[muse] @ git+https://github.com/aoiete/Astor-Memory.git"

# 初始化全新 runtime
export ASTOR_DIR=/var/lib/astor
mkdir -p "$ASTOR_DIR"
python -m astor_memory.cli.main init

# 添加 admin 用户
python -m astor_memory.cli.main user add admin --role first_admin

# 启动 server
python -m astor_memory.server --host 127.0.0.1 --port 7803

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