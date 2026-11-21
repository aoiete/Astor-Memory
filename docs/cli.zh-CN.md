# CLI 参考 (中文)

> `am` 命令行工具参考。这是 [`cli.md`](./cli.md) 的中文精选版：只覆盖主要命令，用法代码块保持英文原文。

每个 `am` 子命令由 CLI 自己的 argument parser 生成，`python tools/gen_cli_doc.py` 可重新生成英文完整版 — 存在的命令一定有文档，删掉的命令自动消失。

---

## 命令索引（精选）

| 命令 | 用途 |
| --- | --- |
| `am init` | 初始化 astor-memory |
| `am seed` | 填充演示数据 |
| `am write` | 写入一条 fact |
| `am recall` | 召回 facts |
| `am doctor` | 健康检查 |
| `am reembed` | 为所有 canonical facts 计算 + 持久化 embedding |
| `am export` | 把记忆导出为 Markdown 文件 |
| `am bundle` | 把记忆状态打包成一个文件 / 恢复 |
| `am config` | 查看 / 修改配置 |
| `am preflight` | 检查本机能否运行 astor（装完先跑这个） |
| `am install` | 把 Astor-Memory 安装进另一个 agent |
| `am learn` | 写入 + 自动识别成功模式 + 复现成功自动晋升 |
| `am fail` | 写入失败 fact（kind=failure_pattern, tier=public） |
| `am postmortem` | 写入 LESSON fact |
| `am procedure` | 写入 Procedure Memory fact（trigger/diagnose/act/verify） |
| `am consult` | 主动咨询：按 query 匹配 success/failure pattern、lesson |
| `am recall-auto` | 把错误 / 工具输出自动分类到正确的召回区 |
| `am pending` / `am confirm` / `am reject` | 候选 fact 的列出 / 晋升 / 丢弃 |
| `am retired` / `am reactivate` | 列出已退役 fact / 恢复为当前事实 |
| `am cascade` | 重放级联写入队列（embed 失败的） |
| `am mcp` | MCP 服务器（stdio） |
| `am bot` / `am admin` / `am platform` | 多用户 bot 管理 / admin 操作 / bot-binding.db |
| `am peer` / `am relay` | 节点身份与关系 / 中继服务 |

其余子命令见文末英文完整版。

---

## 初始化与数据

### `am init` / `am seed`

```bash
am init          # 初始化数据目录与 9-db 布局
am seed          # 填充演示数据（首次运行 / 截图 / HN）
```

### `am export`

导出为 Markdown（MEMORY.md + 每个 memory 一条 bullet），可选 `--git` 在输出目录建 git 仓库并提交，`--dry-run` 只报告不写入。

```
usage: am export [-h] [--out OUT] [--tiers TIERS] [--astor-dir ASTOR_DIR]
                 [--git] [--dry-run]

options:
  -h, --help            show this help message and exit
  --out OUT             Output directory (default: ~/astor-export)
  --tiers TIERS         Comma-separated tiers to include (default: all)
  --astor-dir ASTOR_DIR
                        Astor runtime dir (default: $ASTOR_DIR)
  --git                 Init or reuse a git repo in the output and commit
  --dry-run             Report what would be written without writing it
```

### `am bundle`

三个子命令：`export`（打包成 tar.gz）、`import`（恢复 bundle）、`inspect`（查看 manifest）。备份 / 迁移首选；日常导出用 `am export`。

---

## 写入

### `am write`

```
usage: am write [-h] [--user USER] [--mode MODE] [--tier TIER] [--allow-pii]
                text

positional arguments:
  text         Fact text

options:
  -h, --help   show this help message and exit
  --user USER  User ID
  --mode MODE  Extract mode: auto|none|regex|llm
  --tier TIER  Tier: public|source|private_<user>
  --allow-pii  Write to the public tier even if it contains PII
```

`--tier` 控制三层隔离目标；即使文本疑似含 PII，也要显式 `--allow-pii` 才会写进 public 层。

### `am learn` / `am fail` / `am postmortem`

三个语义化写入入口：

- `am learn` — 成功经验。相似 success facts 达到阈值（默认 3）自动晋升到 public；`--no-promote` 只写不晋升。
- `am fail` — 失败方法，写为 `failure_pattern`（默认 tier=public）。
- `am postmortem` — LESSON fact，必须包含 error + root cause + fix（v1.16+ 默认 tier=public）。

### `am procedure` / `am mental-model` / `am knowledge-page`

- `am procedure` — 四段式过程记忆（`--trigger/--analysis/--action/--verify`，或 `--text` / `--from-file` 自由文本自动拆分）。
- `am mental-model {list,rebuild}` — Hindsight 式固定问题答卷。
- `am knowledge-page {upsert,list,get}` — Hindsight 式知识页。

---

## 读取

### `am recall`

```
usage: am recall [-h] [--user USER] [--top-k TOP_K] [--kinds KINDS]
                 [--zone {all-zones,failure,lesson,none,success}]
                 [--jev-relevance {off,on}]
                 query

positional arguments:
  query                 Query text

options:
  -h, --help            show this help message and exit
  --user USER           User ID
  --top-k TOP_K         Number of results
  --kinds KINDS         Comma-separated kind filter (e.g.
                        user_preference,failure_pattern)
  --zone {all-zones,failure,lesson,none,success}
                        Named outcome zone (default: success). failure |
                        success | lesson | all-zones | none (raw)
  --jev-relevance {off,on}
                        Rerank top hits via jev (off | on; default off)
```

### `am consult` / `am recall-auto`

- `am consult` — 卡住时按 kind（success_pattern / failure_pattern / lesson）各取 top-k 命中。
- `am recall-auto` — 把错误信息或工具输出（`-` 读 stdin）自动分类到正确的召回区，无需人工选 zone。

### `am recall-history` / `am recall-trail`

- `am recall-history` — 列出最近的召回查询（读 `recall_log.jsonl`），支持 `--hours/--contains/--tier` 过滤。
- `am recall-trail <fact_id>` — 反向追踪：哪些召回查询把这条 fact 带回来的。

---

## 健康检查与修复

### `am doctor`

进程内健康检查：schema、canonical 计数、embedding 覆盖率、bot-binding.db、Visual C++ runtime（过旧的 VC++ runtime 会让 onnxruntime 无法导入、服务器静默死亡，此项有自己的退出码 2）。

```
usage: am doctor [-h] [--memory] [--schema] [--migrate-user-meta] [--apply]

options:
  -h, --help           show this help message and exit
  --memory             Memory stats
  --schema             Schema check
  --migrate-user-meta  Report user_meta rows with unroutable default_tier /
                       subscription_plan
  --apply              With --migrate-user-meta, apply the repair
```

### `astor-doctor check` — 分层留存检查（v1.17.77）

`scripts/astor_doctor.py check` 是独立只读扫描（不 import astor_memory，无服务器也能跑）。v1.17.77 起它输出**每层留存（retention）**：

```
   retention: 5430 alive facts, 181 without an embedding
   ⚠ 181 facts are unreachable by the semantic channel (run `am reembed`)
```

含义：一条活着的 fact 若没有 embedding，就是"已捕获、未留存" — 语义通道够不到它，只能靠精确关键词命中。把 capture 和 retention 分开测量，能直接暴露这类静默缺口。

两个配套修复：扫描目录现在包含 `users/<name>/memory/` 下的 per-user DB（此前漏掉了 private 层 — fact 最多、缺 embedding 也最多的那层）；任何一层有缺口即以非零码退出，方便 cron 监控。其余子命令：`repair-conn`（回填 NULL 必填列）、`repair-orphan`（删除无 event 的候选）、`stats`（每 DB 一行计数）。

### `am reembed` — 修复缺失的 embedding（v1.17.76）

为还没有 embedding 的 canonical facts 重新计算并写入向量。遍历每个 (tier, user) 组合：public、source，以及 `list_user_ids()` 里的每一个 private 用户。

关键行为：

1. **以 owner 身份打开 private 层。** 写入 private scope 需要 owner 身份（与写入路径自 v1.17.36 起的同一不变量）。CLI 用 `own_private_scope(user_id)` 包住写检查；没有它，严格隐私模型会拒绝每一个 private scope，整个层被跳过 — 而恰恰是这一层存放着几乎所有缺 embedding 的 facts，命令报告"修复了 0 条"。
2. **跳过必须说明原因。** 权限检查失败时打印：

   ```
   [astor] [private/<user>] skipped: actor='admin:admin' (role=admin) cannot write private_<user>; user write-grant required
   ```

   静默跳过会被读成"无事可做" — 一个修复命令就这样在什么都没修复的情况下报告成功。命令结束时若仍有 scope 打不开，输出里会点名它们。
3. **幂等。** 只补 `embeddings` 表里缺的 fact_id（`INSERT OR REPLACE`，`publishable=0`），模型取自 `astor_get_model_name_for_ram()`；nest 库为空时自动套用 schema。

```
usage: am reembed [-h] [--limit LIMIT] [--batch-size BATCH_SIZE]

options:
  -h, --help            show this help message and exit
  --limit LIMIT         Max facts to re-embed (default: all)
  --batch-size BATCH_SIZE
                        Batch size for model.embed()
```

> 相关：`am cascade replay` 重放 embed 失败的级联写入队列（`stats` 看队列计数，`purge` 清理旧行）。写入路径修复（v1.17.36）之前丢失的 embedding，重跑 `am reembed` 即可恢复。

### `/v1/health` 的三个存储探针（v1.17.78）

三个存储、三个探针：bus（facts 本体）、nest（向量）、lex（BM25 倒排索引）。`am doctor` 是进程内检查，`/v1/health` 是 REST 健康检查，两者都值得看：

```json
{
  "status": "ok",
  "dbs": {"bus": "ok", "nest": "ok", "lex": "missing"},
  "degraded": ["lex"]
}
```

`dbs.lex` 取值 `ok` / `missing` / `error`。lex 文件丢失或不可读时，sqlite 会新建一个空库顶替 → BM25 返回空 → 混合召回静默退化成纯向量，但服务器还在跑，所以 `status` 保持 `ok`，由 `degraded` 列出是哪个库不行。重启修不回索引，因此不该让 status 变红 — 那只会招来重启风暴。探针以只读方式打开索引，检查不会顺手把它创建出来。

### `am preflight` / `am install`

- `am preflight` — 装完立刻跑：检查本机依赖（含 VC++ runtime；`--fix` 可自动安装过旧的红istributable，需管理员权限）。
- `am install --ide {claude-code,cline,opencode,hermes,openclaw,cursor,continue,windsurf,aider}` — 安装进目标 agent，默认 dry-run，`--apply` 才写文件。

---

## Fact 生命周期

| 命令 | 用途 |
| --- | --- |
| `am pending` | 列出写入但从未晋升的候选（`--tier all\|public\|private`） |
| `am confirm <id> --force` | 晋升候选为 commons（`--llm` 让 LLM 抽象出方法再发布，规则仍会把关） |
| `am reject <id>` | 丢弃候选 |
| `am retired` / `am reactivate <id>` | 列出被退役的 fact / 恢复为当前事实（`--reason` 记录原因） |
| `am decay-sweep {run,stats,stagnation}` | 按过期策略自动软删除（默认 dry-run） |
| `am noise-scan` | 只读报告池中的机器噪音行，`--archive` 可软删除（`POST /v1/fact/<id>/restore` 可逆） |
| `am reflection run` | 情景反思（合并 + tombstone） |
| `am auto-link backfill` | 为既有 facts 回填 Zettelkasten 自动链接 |
| `am scrub-public` | 替换共享层 facts 里的机器本地标识符（默认 dry-run） |
| `am mine-public` | 把可复用的 private facts 晋升到共享层（默认 dry-run） |

---

## 多用户与管理

- `am bot` — 多用户模式开关、`add-user`（创建空的 9-db 布局）、`promote`/`demote`、`bind-platform`（platform chat_id → user_id）、`status`。
- `am admin {audit-log,demote,whoami}` — 审计日志读取、把 public fact 移入 `private_<user>`（仅 admin）、当前 admin 锁。
- `am platform` — 管理 `bot-binding.db`：`list/list-users/list-bindings`、`resolve`（chat_id → user_id）、`token-get/token-set`、`bind/unbind`、`set-plan`、`verify`（校验全部 6 个不变量）。
- `am agent` — 查看各接入 agent 想用哪个 LLM provider（`--list` 看表）。
- `am lock-rule {add,list,delete,test}` — 管理驱动 `/v1/classify` 的 LOCK 规则。
- `am skill-distill` / `am skill-stale-check` — 把 procedure memory 渲染成 agent skill 文档 / 报告源 fact 已变化的 skill 文档。

## 网络与服务

- `am mcp serve` — 以 stdio 运行 MCP 服务器（Model Context Protocol）。
- `am relay` — 中继服务（postman，不是 reader）：`serve/register/add/ban/list/poll/lookup`。
- `am peer` — 节点身份 + 关系：`status/init`（身份）、`connect/read/search`（读写对端 public 层，签名验证）、`trust/quarantine/blacklist`（信任管理）、`rekey-*`（密钥轮换）、`endpoint/endpoints`（Phase 3 HTTP 传输）、`fanout/rl`（本地 miss 时自动对端搜索 / 限流管理）。

---

## 相关文档

- [`api.zh-CN.md`](./api.zh-CN.md) — REST API 中文参考（含 `/v1/health` 完整字段说明）
- [`faq.zh-CN.md`](./faq.zh-CN.md) — 常见问题
- `OPERATOR_RUNBOOK.md` — 运维手册（`degraded` 排查等）

英文完整版: [cli.md](./cli.md)
