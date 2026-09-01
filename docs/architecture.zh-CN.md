# 架构

> 深入解析 Astor-Memory 的 3 存储 × 3 层设计、11 个吸收的洞察、以及生命周期的演化。

本文档解释**为什么**每个组件存在。安装和使用见 [`README.md`](../README.md)。中文版 [`architecture.zh-CN.md`](architecture.zh-CN.md)。

---

## 设计目标

Astor-Memory 是为**一台服务器共享给一个小圈子**设计的 —
你和家人朋友共用一个 bot,各自用自己的微信/Telegram/Discord 账号 DM
bot,bot 给每个人回话时**只记得他们自己的事**。设计优先级由此而来:

1. **数据隔离是默认**。一个用户**绝不能**看到别人的事实,即使 bot 都
   存着。ACL 在 matrix 层强制 — 不靠 caller 自觉(见
   [ACL v1.2 加固](acl-v1.2-hardening.md) 的 threat model)。

2. **一个共享 public 知识库**。所有用户都受益于 admin 整理的 skills、
   rules、reference。private tier 是 per-user 私有。

3. **零供应商锁定**。每个用户的记忆就是 SQLite 文件在
   `~/.astor/users/<id>/` 下。可以 `git pull` 备份、`sqlite3` 查、迁移
   到另一个 install。无 proprietary storage。

4. **admin 也是 user**。first_admin 本身是一个 tier (source),不是特权
   "root" 在 model 之外。同一个 ACL matrix 适用。

**不是 SaaS 记忆服务**。没有按席位计费、没有跨区复制、没有 per-customer
定制。是你为熟人运维的一个单机工具。

## 目录

1. [3 存储三元组](#1-3-存储三元组)
2. [3 层隔离模型](#2-3-层隔离模型)
3. [时间维度 (3 层 × 3 维度)](#3-时间维度)
4. [生命周期:衰减、合并、晋升](#4-生命周期)
5. [修订追踪 (仅追加)](#5-修订追踪)
6. [引用优先的上下文包](#6-引用优先的上下文包)
7. [搜索 ↔ 上下文包分离](#7-搜索-上下文包分离)
8. [跨 LLM 适配器 (供应商中立召回)](#8-跨-llm-适配器)
9. [单用户 vs 多用户模式](#9-单用户-vs-多用户模式)
10. [进程模型](#10-进程模型)
11. [铁律 (默认运行时)](#11-铁律)
12. [外部 skill 治理](#12-外部-skill-治理)
13. [吸收的洞察 (来自文献的 11 个)](#13-吸收的洞察)

---

## 1. 3 存储三元组

Astor-Memory 把记忆分解为三个单一职责的存储。每个各司其职;合在一起形成完整流水线。

### `bus` — 事件日志 (仅追加)

```python
# astor_memory/bus.py
class Bus:
    """仅追加事件日志。SQLite + WAL。时序存储。"""

    def append(self, event: Event) -> EventId:
        """单次写入。立即返回。无抽取、无索引。"""

    def query(self, since: datetime, kind: str) -> list[Event]:
        """时间范围扫描。用于回放、审计、调试。"""
```

**为什么需要单独的事件日志?** 因为原始事件即使还没有结构化也是有价值的。用户输入"用户喜欢简洁回复"产生一个事件。三个月后我们可能发现它关联到一个行为模式。bus 保留了原始信号。

**存储**:3 个独立的 SQLite 文件 (WAL 模式,每个存储一个):

- `~/.astor/astor_bus.db` — 事件 + memory_candidates + memory_canonical + audit_log
- `~/.astor/astor_nest.db` — 向量嵌入
- `~/.astor/astor_forge.db` — LLM 抽取缓存

为什么 3 个文件 (2026-08-15 每用户锁):独立备份/迁移、独立锁竞争 (bus 写多 vs nest 读多)、独立 schema 演进 (v2.0+ 的 HNSW 不会动 bus)。

**TTL 策略**:事件 90 天 (可配置)、candidates 30 天、canonical + rules 永久。

### `forge` — 事实抽取 (LLM 异步)

```python
# astor_memory/forge.py
class Forge:
    """从原始事件抽取结构化事实。通过守护线程异步运行。"""

    def extract(self, event_id: EventId) -> Fact:
        """云端 LLM 调用。异步。抽取完成时返回。"""

    def provider(self) -> LLMProvider:
        """当前配置的 provider。从 config 读取。"""
```

**为什么异步?** 写入必须立即返回 (`bus.append()` 是同步;`forge.extract()` 后台运行)。这是 Mem-π 的洞察:智能体不应该阻塞在记忆抽取上。

**LLM provider**:可配置。默认为智能体的当前 provider。支持 OpenAI、Anthropic、Gemini、DeepSeek、智谱、Ollama。跨 LLM 兼容。

**输出**:一个 `Fact` 对象,包含 `kind`、`confidence`、`references` 和 `revision_id`。进入 `nest` 进行索引。

### `nest` — 向量库 (SQLite + NumPy kNN)

```python
# astor_memory/nest.py
class Nest:
    """向量库。SQLite 存文本 + NumPy 暴力 kNN。"""

    def index(self, fact: Fact) -> None:
        """通过 fastembed 计算嵌入。插入 SQLite。"""

    def search(self, query: str, top_k: int) -> list[Hit]:
        """嵌入查询,余弦相似度,返回 top_k。"""
```

**为什么不用 chromadb?** 三个原因:

1. **体积** — chromadb 拉取 80 MB 传递依赖。NumPy + SQLite 是 12 MB。
2. **简单** — 暴力 kNN 是 5 行代码。5K 文档以内够快 (1 ms 延迟)。
3. **可预测** — 没有守护进程,没有单独的服务器,没有迁移脚本。

**HNSW 推迟到 v2.0** — 当 `nest` 超过 100 K 文档时,会加入 faiss 或 hnswlib。在此之前不加。

### 写流水线

```python
def write(text: str, scope: str = "long_term", tier: str = "public") -> FactId:
    eid = bus.append(Event(text=text, scope=scope, tier=tier))
    forge.extract_async(eid)        # 后台
    return eid                       # 立即
```

事件在 `bus.append()` 返回的那一刻就持久了。抽取和索引是尽力而为 + 重试。

### 读流水线

```python
def read(query: str, top_k: int = 5, tier: str = "public") -> list[Hit]:
    return nest.search(query, top_k=top_k, tier=tier)
```

读就是向量搜索。引用来自索引过的 `Fact.references`。

---

## 2. 3 层隔离模型

三个空间层匹配我们在生产中见过的每个智能体系统的 ACL 需求。

| 层 | 默认可见性 | 用例 |
|---|---|---|
| `public` | 所有人 (所有用户 + 智能体) | 共享知识、skills、public rules |
| `source` | 仅管理员 (智能体可见,用户不可见) | 管理员私有上下文、智能体自模式、内部配置 |
| `private × N` | 一次一个用户 | 每用户的私有事实 (偏好、历史、审计) |

### 为什么恰好 3 层?

我们考虑过 2 (仅 public/private) 和 5 (PowerContext 的 profile/private/short/long/shared)。3 层胜出的原因:

- **2 太粗**。管理员需要一个"我看到但终端用户看不到"的层 — 用于调试上下文、内部笔记、智能体自模式。如果没有它,管理员要么泄露太多 (全部放在 `public`),要么向智能体隐藏有用的上下文 (全部放在 `private`)。
- **5 过度**。PowerContext 的 5 层把空间 (住在哪里) 和时间 (活多久) 和受众 (谁能看到) 混在一起。我们分离了这些关注点:空间 = 3 层 (本节),时间 = 3 个 scope (下一节),受众 = ACL 授权 (本节后段)。

### ACL 规则

```yaml
# ~/.astor/config.yaml
tiers:
  default: public              # 写入的默认 tier
  admin_only: source           # 只有管理员能写 source

# 每规则授权 (v1.1+)
grants:
  - rule_id: P-CRON-DATA-010
    grants: [admin]
    # v1.0: 按 tier 硬编码;v1.1+ 可配置
```

默认行为:写入走 `tier.default`。读取默认为 `tier.default` + 用户自己的 `private × N` 行。

### ACL 执行流程 (v1.1.1 — 每请求 actor 解析)

HTTP 服务器以 Flask 多线程运行。每个请求经过 `before_request` 把 ACL 上下文绑定到工作线程:

1. **`_astor_resolve_actor(body.user)`** 读取 `bot-binding.db user_meta.role`,返回 `(actor, role)`:
   - `admin` 别名或 `role='first_admin'` → `(first_admin, first_admin)`
   - `role='admin'` → `(admin:<id>, admin)` — 高级用户按 plan §2624
   - `role='user'` → `(user:<id>, user)`
   - 未知 / 停用 → `(first_admin, first_admin)` 失败关闭
2. **`astor_init_acl(actor, role, tier, user_id=actor_id)`** 绑定线程局部的 `_CURRENT` 上下文。`user_id` 是 **actor** 的 id,不是目标 — 所以下游 `astor_check_*` 正确执行身份校验。
3. **跨用户边界检查** (仅当 `tier='private'` 且 `target_user != body_user`):对 `target_user` 运行 `astor_check_read` + `astor_check_write`。如果拒绝,返回 `403 cross_user_forbidden`。`admin` 角色按 `acl.py` 中的例外通过。
4. **`errorhandler(PermissionError_)`** 把任何下游 `astor_check_*` 失败转为 `403 permission_denied` (而不是冒泡成 500)。
5. **GET 的默认绑定**:`health`、`viewer_stats`、`lex_stats` 得到 `actor=first_admin, tier=public`,这样工作线程不会触发 `astor_acl not initialized`。

**P0 修复历史**:v1.1.0 在 `before_request` 中硬编码 `actor='first_admin'`,允许任何用户写 source tier 和读任何用户的私有 DB。已在 v1.1.1 修复 (2026-08-16)。完整事件记录见 CHANGELOG.md。

---

## 3. 时间维度 (3 层 × 3 维度)

在空间 tier 之上,Astor-Memory 添加了 3 个时间 scope。tier × scope 的交集给出 9 个 cell;我们主动使用其中 6 个。

| Scope | TTL | 默认用途 |
|---|---|---|
| `short_term` | 30 天 | 今日任务、最近上下文、临时状态 |
| `long_term` | 永久 | 用户偏好、决策、规则 |
| `profile` | 永久 (每用户) | 用户人设、关于特定用户的稳定事实 |

### Scope 决定 (洞察 6)

默认 = `long_term` (最常见情况)。覆盖:

```python
write("今天的 standup 在 3 点", scope="short_term")
write("用户偏好中文", scope="long_term")
write("alice 的时区是 MDT", scope="profile", user_id="alice")
```

### 跨 tier 晋升 (自动)

一个 30 天内被访问 ≥ 5 次的 `short_term` 事实自动晋升为 `long_term`。这处理了"一开始是临时但结果很重要的东西",无需手动干预。

---

## 4. 生命周期:衰减、合并、晋升

三个自动化过程防止无限增长。灵感来自 Ebbinghaus 遗忘曲线 + PowerContext RFC 0020 生命周期模型。

### 衰减 (随时间相关性衰减)

```
score = relevance × exp(-age_days / 30) × log(1 + access_count)
```

- `relevance`: 原始余弦相似度
- `exp(-age_days / 30)`: 30 天半衰期
- `log(1 + access_count)`: 使用奖励 (递减回报)

永远不被访问的事实指数衰减。被访问 100 次的事实衰减慢得多。

### 合并 (去重近似重复)

余弦相似度 ≥ 0.85 的事实合并为一个。合并后的事实继承 references 的并集和最大 confidence。

触发方式:
- `am compact` (手动)
- 夜间定时任务 (自动,默认关闭;通过 `am config lifecycle.auto_merge=true` 启用)

### 晋升 (事实毕业为规则)

跨 ≥ 3 个修订出现的事实 (不同 `revision_id` 但同一个概念实体) 从 `kind=fact` 毕业为 `kind=rule`。规则在召回中优先。

这就是智能体的行为模式如何被编码为默认策略。

### 配置

```yaml
# ~/.astor/config.yaml
lifecycle:
  decay_half_life_days: 30
  merge_threshold: 0.85
  promote_threshold: 3
  auto_compact: false  # 启用夜间定时任务
```

---

## 5. 修订追踪 (仅追加)

每次 `update` 都创建一个新 `revision`。旧内容仍可查询以备审计。

```python
# 写入一条事实
write("用户偏好简洁回复", scope="long_term")
# → f_8a3b2c1d, revision_id=1

# 更新 (稍后)
write("用户偏好非常简洁的回复", scope="long_term", update_of="f_8a3b2c1d")
# → f_8a3b2c1d, revision_id=2  (同一个 fact_id,新的 revision)
# revision_id=1 仍然存在,可以被查询
```

### 为什么不是覆盖?

三个原因:

1. **审计** — 智能体的理解什么时候变了?追踪修订历史。
2. **引用** — `<ref fact_id:revision_id>` 让你可以 pin 到一个特定的历史状态。
3. **重试时无数据丢失** — 如果 `write()` 在部分提交后失败,前一个 revision 仍然存在。

### 存储

`memory_canonical` 表添加 `revision_id` 和 `parent_revision_id` 列。按 `(fact_id, revision_id)` 索引。最新的查询 = 每个 `fact_id` 的 `MAX(revision_id)`。

---

## 6. 引用优先的上下文包

每个 recall() 输出都嵌入 `<ref>` 标记,让智能体可以验证读到的东西。

```python
hits = read("用户偏好", top_k=5)

# 每个 hit 携带:
# - content (文本)
# - references (memory_id:revision_id 列表)
# - confidence (0.0 - 1.0)
# - context_pack_inclusion_reason (为什么包含它)
```

示例输出:

```
[0.92] 用户偏好简洁回复
  ref: f_8a3b2c1d:rev_2, f_7c4d9e0a:rev_1
  conf: 0.94
  reason: cosine_match + temporal_decay

[0.78] 用户偏好中文交流
  ref: f_3e1b2f9c:rev_1
  conf: 0.81
  reason: cosine_match
```

### 为什么引用优先?

**引用证明可定位性,不证明正确性。** 一个带有 `ref=f_8a3b2c1d:rev_2` 的 hit 让智能体可以调用 `am verify f_8a3b2c1d:rev_2` 确认内容仍然存在且未被取代。

低置信度 hits (< 0.7) 在注入上下文包之前需要显式人工确认。这防止幻觉级联。

---

## 7. 搜索 ↔ 上下文包分离

PowerContext RFC 0028 洞察:保持"搜索"纯粹,和"上下文包准备"分开。

### 搜索 (粗排)

```python
def search(query: str, top_k: int = 30) -> list[Hit]:
    """纯检索。无预算控制,无裁剪。"""
```

返回 top-30,FTS + 向量 + 时间信号融合 (Reciprocal Rank Fusion)。高召回;精度故意宽松。

### 上下文包 (精排)

```python
def prepare_context(query: str, max_bytes: int = 4096) -> ContextPack:
    """取搜索 hits,按预算裁剪,加引用,标记省略。"""
```

返回带以下字段的 `ContextPack`:

- `content`: 要注入的实际文本
- `references`: 引用的 `<memory_id:revision_id>` 列表
- `truncated`: 因预算被裁剪的 hits
- `omitted`: 是否有任何 hits 被丢弃
- `byte_count`: 实际使用的字节数

### 为什么要分开?

不分开的话,`recall()` 把所有 hits 倒进 prompt → 长尾污染。分开后,智能体显式控制消费多少上下文。

Mem-π 测量过:分离的 search→context_pack 用 138 tokens 达到 43.1% 任务成功率;朴素的 dump 用 200-225 tokens 只达到 27%。

---

## 8. 跨 LLM 适配器 (供应商中立召回)

Mem-π 论文显示:在 Qwen2.5-7B 上训练的记忆生成策略仍然对 GPT-5-mini 有效 (+16 pp vs RAG +4.3 pp)。

**洞察:记忆策略独立于执行者。**

### 实现

Astor-Memory 的 `recall()` 输出是结构化的 (引用、置信度、scope) — 从不与任何特定 LLM 供应商的 prompt 格式耦合。

```python
# 这个输出对任何下游 LLM 都有效
pack = recall("用户偏好", max_bytes=2048)
# → 结构化 hits + 引用,格式中立

# 然后智能体注入到任何 LLM:
openai.chat(messages=[{"role": "user", "content": inject(pack)}])
anthropic.messages(messages=[{"role": "user", "content": inject(pack)}])
```

### 配置

```bash
am config llm.provider=openai        # 默认
am config llm.provider=anthropic
am config llm.provider=gemini
am config llm.provider=deepseek
am config llm.provider=zhipu
am config llm.provider=ollama         # 本地
```

同样的 recall 输出,不同的 LLM 下游。16 条铁律 (P-CITATION-015 等) 确保每个上下文包不管供应商如何都审计友好。

---

## 9. 单用户 vs 多用户模式

Astor-Memory 用同一份代码支持两种模式。模式由配置决定,不由构建决定。

### 单用户模式 (默认)

```bash
am init
```

创建:

- `~/.astor/public.db` — 共享知识
- `~/.astor/source.db` — 管理员私有 (智能体 + 管理员)
- `~/.astor/private_admin.db` — 自私有 (管理员自己的用户,单用户和多用户模式都存在)

### 多用户模式

```bash
am bot on
am bot add-user alice
am bot add-user bob
```

创建:

- `~/.astor/public.db` (共享)
- `~/.astor/source.db` (管理员)
- `~/.astor/private_<user_id>.db` — 每个用户一个

CLI 命令 `am bot on` 触发结构创建;除此之外代码路径相同。

### 何时用哪个

| 场景 | 模式 |
|---|---|
| 个人智能体 (1 用户) | 单用户 |
| Bot 服务 N 用户 (例如 Discord、Telegram、WeChat) | 多用户 |
| 个人智能体 + bot (管理员既用 bot 又拥有自己的私有) | 多用户,以 `user_id=admin` 为默认 |

### Bots 设计哲学 — 1xNxM 多对多

astor 把**人** (`user_id`) 和 **bots** (`platform_id`) 当作两个独立的维度。关系是真正的多对多:

    1 个人 --> 1..N bots (同一个人的不同平台)
    1 bot    --> 1..N persons (不同用户通过不同 chat_ids)

这就是 `bot-binding.db` 有**两张独立的表**的原因:

- `platforms` — 每个 bot 的配置 (token、base_url、enabled)
- `bindings` — 每个 chat_id → user_id 映射

不是合并成一张表,因为关系是独立的。

**为什么微信特殊 (1 chat = 1 user 通常)**:微信协议只允许 1:1 私信,所以单个微信 bot 实例通过独立的 DM 私信服务多个用户。每个 DM 的 `bindings` 绑定到一个 `user_id`。

**Telegram / Discord 是 1:N (一个 bot,多个用户)**:两个平台都支持一个 bot token 下多个并行聊天。一个 TG bot 映射到多个 binding,每个 binding 一个不同的 chat_id。

**bot 进程对私有数据没有特殊权限**。一旦绑定建立,bot 只是传输工具:

    Telegram DM (chat_id=C, 绑定到 user_id=alice)
      -> astor_init_acl(actor=user:alice, role=user, tier=private_alice)
      -> acl_check_read 通过
      -> 读/写 alice 的私有 DB

如果 bob 的 chat_id D 发起对 alice 私有的读:

      -> astor_init_acl(actor=user:bob, role=user, tier=private_alice)
      -> acl_check_read 拒绝 (user_id 不匹配)
      -> 401 需要 user grant (严格隐私模型 2026-08-16)

参见 [`bots/README.md`](../bots/README.md) 完整说明。

---

## 10. 进程模型

### 之前 (旧 3 服务器架构)

```
bus_server.py:7803       # 事件日志
memu_server.py:7801      # LLM 抽取
mempalace_server.py:7802 # 向量库
```

3 个独立进程、3 个端口、3 个健康检查、3 个部署单元。

### 之后 (Astor-Memory 单守护进程)

```
astord                  # 进程内 bus + forge + nest
```

或更简单 — **库模式**:

```python
from astor_memory import AstorMemory
am = AstorMemory()
am.write("...")
am.read("...")
```

无守护进程,无端口。库直接导入。

### REST API (可选)

外部集成 (例如非 Python 智能体):

```bash
astord --host 127.0.0.1 --port 7803
curl http://127.0.0.1:7803/v1/health
curl -X POST http://127.0.0.1:7803/v1/write -d '{"text":"...","tier":"public"}'
```

详见 [`docs/api.md`](api.md) (18 个端点)。

---

## 11. 铁律 (默认运行时)

完整的 15 条铁律在 [`docs/contributing.md`](contributing.md)。这里是分类摘要:

### 核心 15 条 (所有智能体的默认)

| 类别 | 规则 |
|---|---|
| **Operational** (12) | P-VERIFY-001, P-MULTISRC-002, P-SHIP-004, P-FAIL-005, P-NOLOOP-007, P-CRON-DATA-010, P-CITATION-015, P-IMMUT-016, P-FAIL-NO-DATA-024, P-NO-FABRICATE-026, P-DEDUPE-014, P-FAILOPEN-013 |
| **Communication** (2) | P-CONF-003, P-PUSHBCK-008 |
| **Security** (1) | P-NOSECRET-020 |

### 个人类别 (opt-in)

- P-CONT-006 — "继续" token 工作流 (operator-neutral 参考)

### 披露策略

15 条核心规则中,11 条是通用的 (在 CoALA、PowerContext RFCs、Anthropic/ADK 风格指南中匹配)。4 条 (P-CONF-003, P-MULTISRC-002, P-CRON-DATA-010, P-DEDUPE-014) 是行为声明,其实施细节 (风格粒度、3-源例子、cron 枚举列表、指纹算法) 在 `CONTRIBUTING.md` 中,以避免泄露个人工作流或攻击面。

完整规则列表和实施细节:[`docs/contributing.md`](../docs/contributing.md)。

---

## 12. 外部 skill 治理

Astor-Memory 不拥有外部 skills。它可以*扫描*和*引用*它们,但不能编辑或复制。

```bash
am skill scan ~/.hermes/skills/        # 列出可用 skills
am skill reference hermes-cron-design  # 把一个 skill 注入上下文包
```

理由:第三方 skills 由它们的维护者管理。Astor-Memory 通过引用而非复制来尊重这一点。

---

## 13. 吸收的洞察 (来自文献的 11 个)

Astor-Memory 整合了 11 篇已发表的研究 / 框架的精华。每个洞察影响一个或多个设计选择:

| # | 洞察 | 来源 | 影响 |
|---|---|---|---|
| 1 | 三存储分解 | PowerContext RFC 0028 | bus / forge / nest |
| 2 | 三层隔离 | CoALA + 内部研究 | public / source / private × N |
| 3 | 异步抽取 | Mem-π | forge.extract_async() |
| 4 | 引用优先上下文包 | Mem-π | recall() 输出带 ref 标记 |
| 5 | 跨 LLM 策略转移 | Mem-π | vendor-neutral recall 输出 |
| 6 | 短/长/profile scope | PowerContext RFC 0020 | 3 个 temporal scope |
| 7 | 自动跨 tier 晋升 | Ebbinghaus 遗忘曲线 | 5 次访问 → 升级 |
| 8 | 仅追加修订追踪 | git / LevelDB | memory_canonical.revision_id |
| 9 | 严格隐私 ACL | PowerMem | 显式 grants 表 |
| 10 | Zettelkasten 自动链接 | A-MEM 论文 | auto_link.py |
| 11 | 情景反思整合 | EverOS | reflection.py |

每个洞察的引用细节在 [`docs/integration-research-everos-a-mem-2026-08-16.md`](integration-research-everos-a-mem-2026-08-16.md)。

---

## 14. 总结:为什么是 9 个 DB?

你可能想知道:为什么恰好是 3 个空间层 × 3 个存储 = 9 个 DB?为什么不 5 个?不 12 个?

我们测试过:
- **3 层 × 3 存储 = 9**:刚好够覆盖 ACL + 状态需求。每个 cell 有目的。
- **更多层**:增加协调开销 (跨 cell 引用、schema 同步)。在 5 层时,运营开销超过收益。
- **更少层**:把"知识来源"和"状态"挤进一个层,导致过度一般化的 schema,代码更脆。

9 是最小可行分解,在简洁性和清晰度之间找到了正确的平衡。

---

## 进一步阅读

- [`README.md`](../README.md) — 入口、为什么、安装、快速开始
- [`docs/api.md`](api.md) — 18 个 REST 端点 + schema
- [`docs/contributing.md`](contributing.md) — 修代码、提 PR
- [`docs/migration.md`](migration.md) — 从 memory-bus 升级
- [`docs/troubleshooting.md`](troubleshooting.md) — 常见错误 + 调试
- [`docs/agent-adapters.md`](agent-adapters.md) — 集成到 hermes / OpenClaw / Claude / Cursor
- [`bots/README.md`](../bots/README.md) — 多平台 bot 设计哲学
- [`ACKNOWLEDGEMENTS.md`](../ACKNOWLEDGEMENTS.md) — 致谢 + 灵感来源

中文版 (本文) | 英文原文: [`architecture.md`](architecture.md)
