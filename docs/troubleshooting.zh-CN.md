# 故障排查

> Astor-Memory 的常见错误与修复。

如果你的错误不在这里，请到 https://github.com/aoiete/ASTOR-Memory/issues 提 issue，附上：
- `am doctor --verbose` 的输出
- `am --version` 的输出
- 可复现步骤

---

## 目录

1. [安装错误](#安装错误)
2. [初始化错误](#初始化错误)
3. [写入错误](#写入错误)
4. [读取错误](#读取错误)
5. [性能问题](#性能问题)
6. [LLM provider 错误](#llm-provider-错误)
7. [迁移错误](#迁移错误)
8. [ACL & 权限错误](#acl--权限错误)
9. [诊断命令](#诊断命令)

---

## 安装错误

### `pip install astor-memory` 失败，提示 "No matching distribution"

Python 版本太旧。Astor-Memory 要求 Python ≥ 3.10。

```bash
python --version  # 查当前版本
python3.10 -m venv ~/.astor-venv
source ~/.astor-venv/bin/activate
pip install astor-memory
```

### 安装后报 `ImportError: No module named 'fastembed'`

可选依赖未装。基础安装会拉 `fastembed` 做 embedding；如果用了 `--no-deps`，要手动装。

```bash
pip install fastembed
```

### Windows 上 `OSError: [WinError 5] Access is denied`

文件锁。关掉所有打开了 `~/.astor/*.db` 的进程（另一个终端、SQLite 浏览器等）。

```powershell
# 找谁在锁
Get-Process | Where-Object {$_.Path -like "*astor*"}

# 杀进程
Stop-Process -Id <PID> -Force
```

### Linux 上安装失败，提示 "missing wheel for fastembed"

fastembed 在某些平台需要 C 编译器。装编译工具：

```bash
# Ubuntu/Debian
sudo apt install build-essential python3-dev

# Fedora
sudo dnf install gcc python3-devel
```

---

## 初始化错误

### `am init` 失败，提示 "Permission denied"

默认路径 `~/.astor/` 不可写。二选一：

```bash
# 选项 1: 修权限
chmod 700 ~/
mkdir ~/.astor
chmod 700 ~/.astor

# 选项 2: 用自定义路径
export ASTOR_HOME=/path/to/writable/dir
am init
```

### `am init` 失败，提示 "Database is locked"

另一个 `am` 进程在跑。找出来杀掉：

```bash
# Unix
ps aux | grep "am " | grep -v grep

# Windows
Get-Process | Where-Object {$_.ProcessName -like "*am*"}
```

### `am init` 成功，但 `am doctor` 显示 "bus: NOT INITIALIZED"

DB 文件创建了但没初始化（schema 没应用）。跑：

```bash
am init --force
```

这会重跑 schema 迁移。已有数据保留。

---

## 写入错误

### `MemoryWriteError: write failed`

通用写入失败。开 verbose 看原因：

```bash
am write "test" --verbose
```

常见原因：
- 磁盘满
- SQLite DB 损坏（见下）
- LLM provider 超时

### `sqlite3.OperationalError: database is locked`

并发写竞争。Astor-Memory 通过 SQLite WAL 模式串行化写入；这个错意味着
另一个进程开着写事务。

修：

```bash
# 查卡住的进程
am doctor --verbose  # 显示活跃事务
```

如果之前的 `am write` 被中途杀了，DB 可能处于锁状态。重启：

```bash
am serve --restart  # 如果是 daemon 模式
# 或: 杀光所有 am 进程再重启
```

### `MemoryWriteError: tier 'private' requires user_id`

你往 `tier=private` 写时没指定哪个用户。二选一：

```python
write("alice's preference", tier="private", user_id="alice")
```

或改默认 tier：

```bash
am config tiers.default=public
```

---

## 读取错误

### `read()` 返回空列表，但事实确实存在

四种常见原因：

1. **tier 错了**：事实在 `tier=private` 但你在读 `tier=public`。指定：
   ```python
   hits = read("query", tier="private", user_id="alice")
   ```

2. **user_id 错了**：多用户模式下，必须指定读哪个用户的私有 DB。

3. **分数低于阈值**：默认阈值 0.3。调低：
   ```python
   hits = read("query", min_score=0.1)
   ```

4. **衰减掉了**：如果事实很旧 + 访问少，衰减可能把分拉到阈值以下。试：
   ```python
   read("query", bypass_decay=True)
   ```

### `read()` 很慢（> 100 ms 处理 5 K 文档）

三个原因：

1. **top_k 太大**：`read("query", top_k=100)` 比 `top_k=5` 慢 20 倍。用刚好满足需求的最小 `top_k`。
2. **冷缓存**：重启后第一次查询慢，后续会命中 NumPy 缓存。
3. **`astor_nest.db` 太大**：> 50 K 文档触发暴力扫描。跑 `am compact` 合并近似重复，或升级到 v2.0 用 HNSW。

### 引用验证失败（`am verify <ref>` 返回 `valid: false`）

事实被更新（新 `revision_id`）或被修剪了。二选一：

- 用 `read()` 拿最新的 `references`（最新 revision）
- 加大保留期：`am config bus.retention_days=365`

---

## 性能问题

### `am doctor` 显示 forge 慢（latency > 2 秒）

LLM provider 慢。处理方式：

1. 切 provider：`am config llm.provider=anthropic`（或你区域里更快的）
2. 用本地：`am config llm.provider=ollama`（需要本地跑 Ollama）
3. 用更快模型：`am config llm.model=gpt-4o-mini`（vs `gpt-4`）

### 磁盘用量无界增长

事件默认 TTL 是 90 天。如果 churn 很大，修剪：

```bash
am compact --prune-events-older-than-days=30
```

或在 config 里设：

```yaml
# ~/.astor/config.yaml
bus:
  retention_days: 30
lifecycle:
  auto_compact: true  # 夜里跑 cron
```

### `astor_nest.db` 太大（> 1 GB）

有很多近似重复事实。跑合并：

```bash
am compact --merge-threshold=0.85
```

这会合并余弦相似的事实。预期体积缩小 30-50%。

---

## LLM provider 错误

### `AuthenticationError: invalid api_key`

API key 没设或错了。检查：

```bash
echo $OPENAI_API_KEY  # 应该打印一个 sk- 开头的 key
```

如果是空，设：

```bash
export OPENAI_API_KEY=sk-...
am config llm.provider=openai
```

Astor-Memory 从环境变量读：`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、
`GEMINI_API_KEY`、`DEEPSEEK_API_KEY`、`ZHIPU_API_KEY`、`OLLAMA_HOST`。

### `RateLimitError: rate limit exceeded`

撞到 provider 速率限制。两种修法：

1. **换 provider**：`am config llm.provider=deepseek`（通常额度更高）
2. **限流写入**：`am config forge.max_concurrent=2`（vs 默认 10）

### `TimeoutError: forge took too long`

LLM 调用 > 30 秒。二选一：

- 加大超时：`am config forge.timeout_seconds=60`
- 换更快模型：`am config llm.model=gpt-4o-mini`

---

## 迁移错误

### `migrate_from_mem0()` 报告零条记忆被迁移

你的 mem0 客户端凭证是只读的，或 `user_id` filter 没匹配任何用户。验证：

```python
from mem0 import MemoryClient
client = MemoryClient(api_key="<key>")
print(client.list_users())  # 应该列出你的 user_id
```

然后重跑，每次 `write()` 用匹配的 `user_id` 字段。

### `migrate_from_letta()` 因 `sqlite3.OperationalError` 崩溃

如果你在 Letta < 0.5，没有 `blocks` 表。先升级到最新版 Letta，
或直接对你的 Letta PostgreSQL DB 跑 SQL 查询，手动发 `astor_write` 调用。

### `migrate_from_chroma()` 报 "embedding dimension mismatch"

Astor-Memory 默认 `BAAI/bge-base-en-v1.5`（768 维）。如果你的 Chroma 集合
是用别的模型建的，去掉 `embedding` 参数：

```python
write(doc, user_id=user_id, metadata={...})  # 不带 embedding= 参数
```

Astor 会在首次 recall 时用配置的模型重新 embed。

### 源行数多于 Astor-Memory 行数

有些行是故意过滤掉的：bot-binding 表、审计日志、运行时状态。详见
[`docs/migration.md`](./migration.md) § "Pitfall 6: Importing
bot-bound data as facts" 的标准跳过列表。

### Import cutover 写坏了代码

如果你对非平凡代码库用了 bulk `astor-migrate-imports` 风格的批量重写，
高级调用模式（async 写、自定义 event hook、plugin loader）可能不是 1:1 对应。
review 改动 + 看看那些用到 `write()` / `read()` 之外特性的 call site。

详见 [`docs/migration.md`](./migration.md#side-by-side-reference) 的完整映射表。
常见坑：

| 旧 | 新 |
|---|---|
| `auto_route_v2.write_async(...)` | `await write_async(...)`（现在是 `async def`） |
| `auto_route_v2.read_user(query, user_id)` | `read(query, user_id=user_id)` |
| `auto_route_v2.delete(fact_id)` | `am.delete(fact_id)`（CLI；v1.0 没有 Python delete API — 通过新 revision 软删） |

---

## ACL & 权限错误

### 私有 tier 读写时 `403 cross_user_forbidden`

你在试图访问别的用户的私有 DB。设计上：

- **普通 `user` 角色** 只能访问自己的 `private_<self>`。跨用户读/写都被拒。
- **`admin` 角色**（按方案 §2624，是高权限用户）可以跨读，用于支持。
  跨写也允许，但你应该写一条 `astor_audit` 记录这次审核事件。
- **`first_admin` 角色**（系统 root，`user_id='admin'` 别名）可以访问任何
  用户的私有 DB。

如果你拿到 `403` 觉得不该被拒，检查：

```bash
# 你的用户是什么角色？
am platform list-users  # → 查你的 user_id 的 role
```

如果你缺 admin 角色，请 first_admin 把你提升：

```bash
am bot promote <your_user_id> --to=admin  # 仅 first_admin 可用
```

### 用得好好的突然 `403 permission_denied`

这是某个路由处理函数里 `astor_check_*` 失败了。可能：

- 路由被传入陈的 `body.user`（上游代码缓存了默认值）
- 用户的 `bot-binding.db user_meta.role` 被改了，现在被拒
- tier 不匹配：你在本来该是 `tier='public'` 的请求里调了 `tier='private'`

查服务日志：

```bash
tail -50 ~/.astor/logs/astor_server.log  # Linux/Mac
# 或
Get-Content $ASTOR_DIR (runtime)\logs\astor_server.log -Tail 50  # PowerShell
```

`astor_check_*` 失败详情里有 actor、role、tier 和目标 user_id — 就能看到
是哪条约束触发的。

### `sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in that same thread`（仅 Flask 服务器）

你在跑 v1.1.0 或更早。升级到 **v1.1.1 或之后** — 这次修复给
`bot_binding._connect()` 加了 `check_same_thread=False`，因为 Flask 是
多线程的，ACL `before_request` hook 现在会从 worker 线程读 `bot-binding.db`。

快查：

```bash
am --version  # 应该打印 1.1.1 或更新
```

如果暂时升级不了，把 Flask 服务器跑成单线程：

```bash
flask --app astor_memory.server run --port 7803 --without-threads
```

### Health 端点返回 `403`

`/v1/health` 应该一直返 200 用于健康监控。如果你在 v1.1.1 ACL 修复之后
看到 403，重启服务让新的 `before_request` 代码生效 — 老的 worker 线程
可能还持有旧的 ACL bind。

```bash
python $ASTOR_DIR (runtime)\restart.py restart
```

---

## 诊断命令

### `am doctor` — 整体健康

```bash
am doctor
```

输出：

```
bus:   OK (1,247 events, 12 MB)
forge: OK (provider=openai, latency=320ms)
nest:  OK (847 docs, 5 KB vector cache)
```

verbose：

```bash
am doctor --verbose
```

输出含：
- DB 文件路径和大小
- 当前 LLM 连接状态
- 近期日志错误计数

### `am doctor --repair` — 自动修常见问题

```bash
am doctor --repair
```

自动修：
- 缺失索引（重建）
- 锁住的 DB（清掉 lock 文件）
- 陈的 `astor_forge.db`（清缓存；安全）

**不**自动修：
- Schema 损坏（要人工干预）
- Provider auth 失败（用户自己改环境变量）

### `am inspect <reference>` — 调试某条事实

```bash
am inspect f_8a3b2c1d:rev_2
```

输出：

```
fact_id: f_8a3b2c1d
revision_id: 2
content: "user prefers concise replies"
scope: long_term
tier: public
created_at: 2026-07-15T10:23:45Z
updated_at: 2026-08-01T14:11:22Z
access_count: 17
references: [f_3e1b2f9c, f_7c4d9e0a]
parent_revision: f_8a3b2c1d:rev_1
```

### `am logs` — 最近活动

```bash
am logs --tail=100
```

输出：最近 100 行日志（bus + forge + nest 合并）。

筛选：

```bash
am logs --level=error --tail=20
am logs --since=1h
```

### `am benchmark` — 性能检查

```bash
am benchmark --read-queries=100 --write-events=50
```

输出：吞吐数字（queries/second、writes/second、p50/p95/p99 latency）。

用于改 config 前后的对比。