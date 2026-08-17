# API 参考 (中文)

> Astor-Memory REST API 完整参考。所有 18 个端点的请求/响应 schema + 错误码。

本文档是 [`api.md`](api.md) 的中文翻译版本。结构对齐;示例和 curl 命令可互换。

---

## 基础信息

- **默认端点:** `http://127.0.0.1:7803`
- **API 前缀:** `/v1`
- **认证:** 在 ACL 上下文中通过请求体的 `user` 字段。无 token-based 认证 — 设计如此。
- **数据格式:** 所有请求/响应都是 JSON UTF-8。
- **版本:** `1.2.7`(跟随 astor-memory `__version__`)

### 启动服务器

```bash
# 直接运行
python -m astor_memory.server --host 127.0.0.1 --port 7803

# 或通过 restart.py(推荐用于生产)
python /path/to/astor-memory/restart.py start
```

### 健康检查

```bash
curl http://127.0.0.1:7803/v1/health
```

**响应 200:**

```json
{
  "status": "ok",
  "version": "1.2.7",
  "astor_dir": "$ASTOR_DIR (例如 /home/you/.astor)",
  "dbs": {
    "bus": "$ASTOR_DIR/public/memory/astor_bus_public.db",
    "nest": "$ASTOR_DIR/public/memory/astor_nest_public.db"
  },
  "facts": 866,
  "events": 86,
  "embeddings": 624
}
```

---

## 通用 schema

### 公共请求字段

每个写/读请求体都包含:

| 字段 | 类型 | 必需 | 含义 |
|---|---|---|---|
| `user` | string | ✅ | 调用者的 user_id(从 `bot-binding.db` 解析为 actor + role) |
| `tier` | string | ✅ | `public` / `source` / `private` / `private_<user_id>` |
| `user_id` | string | 仅 private 时 | 目标用户(默认 = actor 的 user_id) |
| `text` | string | 写入 | 要存储的事实文本 |
| `query` | string | 读取 | 自然语言查询 |
| `top_k` | int | 否(默认 5) | 返回的 hits 数 |
| `scope` | string | 否(默认 `long_term`) | `short_term` / `long_term` / `profile` |
| `kind` | string | 否 | `fact` / `rule` / `behavior` / `skill` |

### 错误响应

所有错误返回带 `error` 和 `detail` 字段的 JSON。

| 状态 | `error` | 含义 |
|---|---|---|
| 400 | `text_required` | 写入请求缺少 `text` |
| 400 | `invalid_scope` | `scope` 不是 `short_term` / `long_term` / `profile` |
| 400 | `tier_repo_requires_repo_id` | `tier=repo` 但缺 `repo_id` |
| 403 | `permission_denied` | 下游 `astor_check_*` 拒绝(例如 user → source tier) |
| 403 | `cross_user_forbidden` | `role=user` 尝试跨用户访问 |
| 403 | `acl_init_failed` | `astor_init_acl` 本身失败(bad tier / private 缺 user_id) |
| 404 | `not_found` | fact_id 不存在或被 tombstone |
| 500 | `internal_error` | 未捕获异常(查看 server 日志) |

示例 403:

```json
{
  "detail": "actor='user:alice' (role=user) cannot read tier=source; only first_admin may read source.db",
  "error": "permission_denied"
}
```

---

## 端点清单

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/v1/health` | 健康检查 |
| `POST` | `/v1/write` | 写入一条事实 |
| `POST` | `/v1/read` | 召回 top-k 事实 |
| `POST` | `/v1/recall` | 带引用的上下文包(精排) |
| `POST` | `/v1/forget` | 软删除一条事实(tombstone) |
| `POST` | `/v1/compact` | 合并近似重复的事实 |
| `POST` | `/v1/search` | 纯搜索(粗排,无 budget) |
| `POST` | `/v1/verify` | 验证一个 `<ref fact_id:revision_id>` 仍然存在 |
| `GET` | `/v1/stats` | 系统级统计 |
| `GET` | `/v1/fact/{fact_id}/provenance` | 一条事实的引用血统 |
| `GET` | `/v1/fact/{fact_id}/lineage` | 一条事实的所有修订 |
| `GET` | `/v1/fact/{fact_id}/versions` | 一条事实的所有 `revision_id`(按时间) |
| `POST` | `/v1/fact/{fact_id}/restore` | 把一条事实恢复到之前的 revision |
| `GET` | `/v1/lex/stats` | FTS5 索引统计 |
| `POST` | `/v1/lex/rebuild` | 从头重建 FTS5 索引 |
| `GET` | `/v1/audit/log` | 读审计日志(admin-only) |
| `GET` | `/v1/admin/locks` | 列出当前管理员锁(部署相关) |
| `GET` | `/v1/admin/audit-log` | 同 `/v1/audit/log`,别名 |

---

## 详细端点

### `POST /v1/write`

写入一条新事实(或创建一个新 revision)。

**请求体:**

```json
{
  "user": "alice",
  "tier": "private",
  "user_id": "alice",
  "text": "alice 喜欢浓缩咖啡",
  "kind": "fact",
  "scope": "long_term",
  "tags": ["preference", "food"],
  "references": ["src_conversation_42"]
}
```

**响应 200:**

```json
{
  "count": 1,
  "event_id": 123,
  "fact_ids": ["f_8a3b2c1d"],
  "mirrored": [],
  "scope": "long_term",
  "tier": "private"
}
```

**错误:** `400 text required`, `400 invalid scope`, `400 tier=repo requires repo_id`,
`403 permission_denied` (user → source tier), `403 acl_init_failed`。

### `POST /v1/read`

向量召回 top-k 事实。

**请求体:**

```json
{
  "user": "alice",
  "tier": "private",
  "user_id": "alice",
  "query": "咖啡偏好",
  "top_k": 5,
  "scope": "long_term",
  "min_confidence": 0.5
}
```

**响应 200:**

```json
{
  "count": 3,
  "results": [
    {
      "confidence": 0.92,
      "content": "alice 喜欢浓缩咖啡",
      "fact_id": "f_8a3b2c1d",
      "revision_id": 2,
      "kind": "fact",
      "namespace": "/preference/food",
      "similarity": 0.94,
      "score_kind": "hybrid",
      "tags": ["preference", "food"],
      "references": ["src_conversation_42"]
    }
  ]
}
```

**字段说明:**
- `confidence`: cosine × 衰减 × 使用奖励,clamped 到 [0, 1]
- `similarity`: 原始余弦相似度
- `score_kind`: `cosine` / `temporal` / `hybrid` (FTS + 向量 + 时间融合)
- `references`: 原始 `Event.reference` 列表(原始事件 URL / ID)

### `POST /v1/recall`

上下文包:取 `search` 的 top-30,按 `max_bytes` 预算精排。

**请求体:**

```json
{
  "user": "alice",
  "tier": "private",
  "user_id": "alice",
  "query": "我应该喝什么咖啡?",
  "max_bytes": 2048,
  "min_confidence": 0.7,
  "include_omitted": false
}
```

**响应 200:**

```json
{
  "content": "[0.92] alice 喜欢浓缩咖啡\n  ref: f_8a3b2c1d:rev_2\n  conf: 0.94\n[0.78] alice 不喜欢加糖\n  ref: f_3e1b2f9c:rev_1\n  conf: 0.81",
  "references": ["f_8a3b2c1d:rev_2", "f_3e1b2f9c:rev_1"],
  "truncated": [],
  "omitted": false,
  "byte_count": 387
}
```

`truncated` 是被 budget 裁掉的 hits 列表;`omitted` = `true` 表示至少一个 hit 被完全丢弃。

### `POST /v1/forget`

软删除一条事实(tombstone + 审计行)。事实永远不会被硬删除。

**请求体:**

```json
{
  "user": "alice",
  "tier": "private",
  "user_id": "alice",
  "fact_id": "f_8a3b2c1d",
  "reason": "错误:文本不正确"
}
```

**响应 200:**

```json
{"ok": true, "fact_id": "f_8a3b2c1d", "tombstoned_at": "2026-08-16T..."}
```

`forget` 要求原因(reason);管理员可以覆盖。

### `POST /v1/compact`

合并余弦相似度 ≥ 0.85 的事实(revisions 保留)。

**请求体:**

```json
{
  "user": "admin",
  "tier": "source",
  "threshold": 0.85,
  "dry_run": false,
  "scope_filter": "long_term"
}
```

**响应 200:**

```json
{
  "merged": 14,
  "kept": 286,
  "fact_ids_created": ["f_new01", "f_new02"],
  "fact_ids_revised": ["f_8a3b2c1d", "f_8a3b2c1e"]
}
```

### `POST /v1/search`

纯搜索(无 budget,粗排,top-30)。

**请求体:**

```json
{
  "user": "alice",
  "tier": "private",
  "user_id": "alice",
  "query": "咖啡",
  "top_k": 30,
  "scope": "long_term"
}
```

**响应 200:** 与 `read` 相同的 `results` 数组,但最多 30 个 hits,无 `content` 字段。

### `POST /v1/verify`

验证 `<ref fact_id:revision_id>` 仍然存在且未被取代。

**请求体:**

```json
{
  "user": "alice",
  "tier": "private",
  "user_id": "alice",
  "fact_id": "f_8a3b2c1d",
  "revision_id": 2
}
```

**响应 200:**

```json
{
  "exists": true,
  "fact_id": "f_8a3b2c1d",
  "revision_id": 2,
  "is_latest": false,
  "latest_revision_id": 5,
  "tombstoned": false,
  "content": "alice 喜欢浓缩咖啡"
}
```

智能体在 recall 之后调用 verify 来审计引用。

### `GET /v1/stats`

系统级统计。

**响应 200:**

```json
{
  "version": "1.2.7",
  "astor_dir": "$ASTOR_DIR (例如 /home/you/.astor)",
  "generated_at": "2026-08-16T...Z",
  "counts": {
    "facts_total": 6808,
    "facts_by_tier": {"public": 618, "source": 3080, "private": 3104, "repo": 6},
    "facts_by_scope": {"long_term": 6243, "short_term": 4, "profile": 5},
    "events_total": 3096,
    "candidates_total": 142,
    "embeddings_total": 624
  },
  "lifecycle": {
    "pending_compaction": 12,
    "last_compact_at": "2026-08-15T..."
  }
}
```

### `GET /v1/fact/{fact_id}/provenance`

一条事实的引用血统 — 哪些事件产生了它。

**响应 200:**

```json
{
  "fact_id": "f_8a3b2c1d",
  "lineage": [
    {"event_id": 42, "ts": "2026-07-12T...", "source": "user_conversation", "score": 1.0},
    {"event_id": 45, "ts": "2026-07-12T...", "source": "explicit_edit", "score": 0.8}
  ]
}
```

### `GET /v1/fact/{fact_id}/lineage`

一条事实的所有修订(按 `revision_id` 升序)。

**响应 200:**

```json
{
  "fact_id": "f_8a3b2c1d",
  "revisions": [
    {"revision_id": 1, "ts": "2026-07-12T...", "content": "用户偏好简洁回复", "author": "user:alice"},
    {"revision_id": 2, "ts": "2026-07-15T...", "content": "用户偏好非常简洁的回复", "author": "user:alice"},
    {"revision_id": 5, "ts": "2026-08-16T...", "content": "用户偏好简短的回复", "author": "user:alice"}
  ]
}
```

### `GET /v1/fact/{fact_id}/versions`

列出所有 `revision_id`(轻量级版 lineage — 没有内容)。

### `POST /v1/fact/{fact_id}/restore`

把一条事实恢复到之前的 revision。**这不会硬删除** — 它创建一个指向旧内容的新 revision。

**请求体:**

```json
{
  "user": "alice",
  "tier": "private",
  "user_id": "alice",
  "fact_id": "f_8a3b2c1d",
  "restore_to_revision": 1,
  "reason": "修正错误编辑"
}
```

**响应 200:**

```json
{"ok": true, "fact_id": "f_8a3b2c1d", "new_revision_id": 6, "restored_from": 1}
```

### `GET /v1/lex/stats`

FTS5 词汇表 + 文档统计。

**响应 200:**

```json
{
  "private/admin": {"documents": 3104, "terms": 9197, "tombstoned": 0},
  "private/alice": {"documents": 8, "terms": 240, "tombstoned": 0},
  "public/_": {"documents": 5, "terms": 21, "tombstoned": 0},
  "source/_": {"documents": 12, "terms": 145, "tombstoned": 0},
  "version": 1
}
```

### `POST /v1/lex/rebuild`

从头重建 FTS5 索引(从 `memory_canonical`)。bulk import 后运行。

**请求体:**

```json
{
  "user": "admin",
  "tier": "source",
  "scope": "all"
}
```

**响应 200:**

```json
{"ok": true, "rebuilt": 7081, "duration_seconds": 12.3}
```

### `GET /v1/audit/log`

读审计日志(admin-only)。追加日志 — 所有 `private` 和 `source` 读写都在这里。

**查询参数:** `since` (ISO timestamp), `actor`, `user_id`, `action`, `limit` (默认 50)。

**响应 200:**

```json
{
  "rows": [
    {
      "id": 12345,
      "ts": "2026-08-16T...",
      "actor": "user:alice",
      "role": "user",
      "tier": "private",
      "user_id": "alice",
      "action": "read",
      "target": "memory_canonical/f_8a3b2c1d:rev_2",
      "reason": null,
      "metadata": {"top_k": 5}
    }
  ],
  "total_rows": 3096,
  "has_more": false
}
```

### `GET /v1/admin/locks`

列出当前管理员锁 — 防止并发 `am init` 或 `am bot on` 操作。

### `GET /v1/admin/audit-log`

`/v1/audit/log` 的别名,保持向后兼容。

---

## 错误恢复模式

| 错误 | 怎么办 |
|---|---|
| 503 — server not ready | 等几秒重试;`am doctor` 检查启动状态 |
| `403 permission_denied` | 检查 `user` 字段是否在 `bot-binding.db user_meta` 中 |
| `400 invalid_scope` | 必须是 `short_term` / `long_term` / `profile` |
| `500 internal_error` | 查看 `logs/astor_server.log`;文件 bug 报告到 GitHub issues |

---

## curl 速查表

```bash
# 健康检查
curl http://127.0.0.1:7803/v1/health

# 写入(alice 私有)
curl -X POST http://127.0.0.1:7803/v1/write \
  -H "Content-Type: application/json" \
  -d '{"user":"alice","tier":"private","user_id":"alice","text":"alice 喜欢浓缩咖啡"}'

# 读取
curl -X POST http://127.0.0.1:7803/v1/read \
  -H "Content-Type: application/json" \
  -d '{"user":"alice","tier":"private","user_id":"alice","query":"咖啡","top_k":3}'

# 软删除
curl -X POST http://127.0.0.1:7803/v1/forget \
  -H "Content-Type: application/json" \
  -d '{"user":"alice","tier":"private","user_id":"alice","fact_id":"f_xxx","reason":"text wrong"}'

# 验证引用
curl -X POST http://127.0.0.1:7803/v1/verify \
  -H "Content-Type: application/json" \
  -d '{"user":"alice","tier":"private","user_id":"alice","fact_id":"f_xxx","revision_id":2}'
```

---

## 速率限制

**目前无硬性速率限制**。但 forge 异步调用云端 LLM,所以快速重复写入会在 forge 队列中积压。如果超过 ~100 写/秒,forge 滞后会变得明显。

未来版本可能加入 token-bucket 限流。

---

## 版本兼容性

API 版本 = `astor-memory __version__`。v1.2+ 的所有端点都向后兼容到 v1.0。破坏性变更只在 major bump(2.0+)。

---

中文版 (本文) | 英文原文: [`api.md`](api.md)
