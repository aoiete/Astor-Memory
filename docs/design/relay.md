# Astor Relay — 设计文档

**版本** v0.1 (draft)
**日期** 2026-10-21
**状态** 待 review

---

## 1. 要解决的问题

现在两台 astor 节点要成为好友,必须知道对方的**可直连地址**:

```
astor:ea1c7c31...@astor.example.com
              └──── 地址,收件人靠它投递请求
```

这个地址只在「对方有公网可达入口」时成立。实际情况:

| 对方状态 | 好友码里的地址 | 能加吗 |
|---|---|---|
| 有公网 IP / Cloudflare 隧道 | `host:port` | ✅ 能 |
| 家用宽带,路由器后面 | 路由器公网 IP | ❌ 端口没映射,连不上 |
| 公司网络 / CGNAT | 无 | ❌ 完全不可达 |
| 防火墙挡了 7803 | `host:7803` | ❌ 连不上 |

**已验证的失败形态**:客户机在 NAT 后,好友码带了地址,请求发出去得到
`URLError: [WinError 10061] Connection refused` 或超时,对方完全没有记录 ——
因为请求根本没到。

**Relay 解决的就是这一类:对方不可直连,但双方都能出网。**

---

## 2. 定位与边界

Relay **只做两件事**:

1. **发现** — 把「peer_id → 可联系地址」登记下来,并提供查询
2. **转发好友请求** — A 不知道 B 的地址时,由 relay 代投;B 应答也经 relay 回传

**不做的事**(明确划界):

- ❌ 不中继记忆数据(搜索结果、召回内容)
- ❌ 不存业务数据
- ❌ 不做身份权威 — peer_id 的真实性永远由 Ed25519 签名保证,relay 只是邮差

内存数据仍然**节点间直连**。理由:

- VPS 带宽便宜但没必要全走它
- 直连时数据不经过第三方,隐私更好
- relay 只在「直连不可行」时承担兜底

**关键性质:relay 不可信也不需要可信。**
所有经 relay 转发的消息都带发起方的 Ed25519 签名,收件方独立验签。
relay 改一个字,签名就废。relay 唯一能做的坏事是**不转发**或**重放** ——
前者是可用性问题(有 fallback),后者由时间戳 + 幂等 ID 防住。

---

## 3. 架构

```
        ┌──────────────┐
        │  Node A      │
        │ (NAT 后)     │
        └──────┬───────┘
               │ ① 查询 peer_id → endpoint
               │ ② 投递 friend_request
               ▼
        ┌──────────────────────┐
        │   Relay (VPS)       │
        │   极简: SQLite       │
        │   只存映射 + 待投邮箱 │
        └──────────┬───────────┘
                   │ ③ B 来轮询
                   ▼
        ┌──────────────┐
        │  Node B      │
        │ (NAT 后)     │
        └──────────────┘
```

### 为什么 B 要「轮询」而不是「被推送」

B 在 NAT 后,relay 无法主动连 B(除非 B 开反向隧道)。所以 B 必须**出网**来取消息。
这是长轮询模型,和 IM 的离线消息是同一个道理。

轮询间隔:正常 30s,relay 有待投消息时服务端会立刻返回(长轮询 hold 住连接),
所以体感接近实时。

---

## 4. 协议

### 4.1 登记(register)

```http
POST {relay}/v1/relay/register
{
  "peer_id": "astor:ea1c7c31...",
  "endpoint": "https://astor.example.com",   // 可选:能直连的才填
  "public_key": "base64...",
  "contact": "https://astor.example.com/v1/peer/recv",  // relay 投递用
  "contact_via_relay": true,
  "ttl_seconds": 3600
}
→ 200 {"ok": true, "expires_at": "..."}
```

- `contact` 是 relay **投递**用的地址(消息落点)
- `endpoint` 是给别的 peer **直连**用的地址(可选)
- 两者都可以是 relay 自己的地址 —— 那就是纯 relay 模式
- TTL 到期自动清理,离线节点不会留下永久垃圾条目

### 4.2 查询(lookup)

```http
GET {relay}/v1/relay/lookup/{peer_id}
→ 200 {
    "peer_id": "...",
    "found": true,
    "endpoint": "https://...",        // 直连地址,可能为 null
    "contact": "https://...",          // relay 投递地址
    "via_relay": true,                 // true = 必须经 relay
    "expires_at": "..."
  }
→ 404 {"found": false}
```

### 4.3 投递(send)—— A → B

```http
POST {relay}/v1/relay/send
{
  "msg_type": "friend_request",
  "msg": { ...原样的 friend_request 消息,含 signature... },
  "to_peer_id": "astor:...",
  "from_peer_id": "astor:..."
}
→ 200 {"ok": true, "queued": true}
```

relay **不解析、不修改** `msg` 内部,原样存进 B 的邮箱。

### 4.4 轮询(poll)—— B 取消息

```http
GET {relay}/v1/relay/poll/{peer_id}?wait=25
→ 200 {"messages": [ {msg_type, msg, from_peer_id, received_at, msg_id}, ... ]}
```

长轮询:无消息时 hold 住最多 `wait` 秒,期间有消息立刻返回。
返回后 B **必须**处理并清空(relay 侧在 B 确认前保留副本)。

### 4.5 确认(ack)

```http
POST {relay}/v1/relay/ack
{"peer_id": "...", "msg_ids": ["..."]}
→ 200 {"ok": true}
```

relay 收到 ack 才真正删除。**B 崩溃不会丢消息**(至少在 ack 前还在)。

### 4.6 应答回程 —— B → A

B 处理完请求后,要给 A 回一个 `friend_request_response`。如果 A 也是 relay-only,
B 同样用 `POST /v1/relay/send` 发给 A。**对称,无需特殊逻辑。**

---

## 5. 客户端集成

### 5.1 新增配置

```yaml
# ~/.astor/config.yaml 或环境变量
relay:
  enabled: true
  urls:
    - https://relay.example.com
  poll_interval: 30
  # 邀请码模式(可选)
  require_invite: false
  invite_codes: []      # 自己的邀请码
```

环境变量:`ASTOR_RELAY_URLS`(逗号分隔)、`ASTOR_RELAY_ENABLED`

### 5.2 发送路径改动

`send_friend_request` 现在的逻辑是「直接 POST 对端 `/v1/peer/recv`」。
改成:

```
send_friend_request(peer_id, endpoint, ...)
  ├─ 有可达 endpoint?  → 直连 POST(现状,不动)
  │                      失败 → 降级到 relay
  └─ 无 endpoint / 直连失败?
       → POST {relay}/v1/relay/send {to_peer_id, msg}
```

**直连优先,relay 兜底。** 这就是「两者都支持」的实现方式:
能直连的节点完全不知道 relay 存在。

### 5.3 收信路径改动

新增一个轻量后台线程(或复用 cron tick):

```
每 poll_interval:
  for relay in relays:
    for msg in GET relay/v1/relay/poll/{my_peer_id}:
      if msg.msg_type == 'friend_request':
        验签 → record_friend_request(pending) → dashboard 弹窗
      POST relay/v1/relay/ack {msg_ids}
```

**验签复用现有的 `verify_friend_request`** —— relay 送来的消息和直连来的
走同一条验证路径,relay 拿不到任何额外的信任。

### 5.4 地址发现路径改动

加好友时如果用户只给了 `peer_id` 没给地址:

```
lookup(peer_id):
  1. 查本地 peer_relationships(已加过的)
  2. 查本地 relay 缓存
  3. 问 relay:GET /v1/relay/lookup/{peer_id}
  4. 拿到 → 用它投递
```

---

## 6. 安全模型

### 6.1 信任边界

| 组件 | 被信任做什么 | 不被信任做什么 |
|---|---|---|
| Relay | 存映射、当邮差 | 任何消息内容的真实性 |
| 签名 | 证明消息来自持有对应私钥的节点 | — |
| peer_id | 标识(由安装路径派生) | 本身不证明任何事 |

**核心不变式:relay 无法伪造任何消息。** 收件方永远用发送方的公钥独立验签。

### 6.2 攻击面与对策

| 攻击 | 对策 |
|---|---|
| relay 篡改消息 | 签名覆盖全部字段,验签失败即拒 |
| relay 重放旧请求 | 每条消息带 `msg_id`;收件方记录已处理 id,重复丢弃 |
| relay 冒充 A | 签名对不上 A 的公钥,拒 |
| relay 拒绝转发(审查) | 直连路径仍然可用;这是可用性风险,无法用密码学解决 |
| relay 无限膨胀 | 每 peer 邮箱上限 + TTL + 全局配额 |
| 恶意 peer 刷请求 | 复用现有 `peer_rate_limit` |
| relay 泄露通讯图 | **无法避免** —— relay 天然知道谁联系谁。这是本方案最大的隐私代价,要在文档里明说 |

### 6.3 邀请码模式(可选)

默认开放(拿到好友码就能加)。开启后:

- 好友码格式扩展:`astor:<32hex>@<addr>#<invite>`
- relay 校验 invite 是否在允许列表
- invite 由**被加方**生成并分发,相当于「门牌号 + 口令」

注意:invite 走明文(relay 看得到),所以它防的是「陌生人乱加」,
不是「relay 偷看」。真正的机密性要靠 TLS + relay 自身可信。

---

## 7. 部署

### 7.1 Relay 服务端

- **技术栈**:Python + Flask(和 astor 同栈,复用依赖管理)+ SQLite
- **规模**:单机 SQLite 支撑几千 peer 无压力(只有映射和邮箱)
- **代码位置**:`astor_memory/_internal/relay_server.py`(可独立部署)
- **运行**:NSSM(Windows)或 systemd(Linux VPS)

### 7.2 VPS 需求

| 项 | 需求 |
|---|---|
| CPU / 内存 | 极低,1C512M 足够 |
| 带宽 | 只转发好友请求(几百字节),几乎不耗流量 |
| 端口 | 443(走 nginx/caddy 反代 + TLS) |
| 持久化 | SQLite 文件,需备份 |

**不需要贵机器。** 因为不中继记忆数据,流量比想象中小两个数量级。

### 7.3 备选后端(可选后端模式)

同一套客户端接口,不同实现:

| 方案 | 适合 |
|---|---|
| 自建 relay | 完整可控,长期用 |
| Tailscale | 双方都装客户端,组网直接解决,不需要 relay |
| frp / rathole | 只需要给 NAT 后的节点开一个入口 |
| ZeroTier | 同 Tailscale,更老牌 |

**建议:优先试 Tailscale / frp —— 如果你的客户能装客户端,那根本不需要 relay。**
relay 的价值在于「对方什么都不用装,只要能出网」。

---

## 8. 实施计划

| 阶段 | 内容 | 可独立验证 |
|---|---|---|
| **P1** | Relay 服务端:register / lookup / send / poll / ack + SQLite | curl 跑通 5 个端点 |
| **P2** | 客户端:发送路径加 relay 兜底 | 直连失败时自动走 relay |
| **P3** | 客户端:后台轮询线程 + 验签入队 | A 经 relay 发,B 弹窗 |
| **P4** | 邀请码模式(可选开关) | 关闭时拒绝带错 invite 的请求 |
| **P5** | 文档 + `am relay` CLI | — |

**P1+P2+P3 是最小可用闭环**,约等于「NAT 后的客户能被加到」。
P4 可以延后。

### 风险

| 风险 | 缓解 |
|---|---|
| 客户端轮询线程引入并发问题 | 复用 `agent_hygiene_async` 已有的后台线程模式;或用 cron tick 而非常驻线程 |
| relay 单点 | 客户端支持多 relay URL,轮询 failover |
| 长时间运行内存增长 | 邮箱 TTL + 定期清理 |

---

## 9. 待确认

1. **P3 的轮询用常驻线程还是 cron tick?** —— 倾向 cron tick(和现有 peer health
   一致,不引入新的常驻进程)
2. **relay 存不存「谁加过谁」的社交图?** —— 建议只存必要的映射 + 未投递消息,
   ack 后即删
3. **要不要给 relay 做一个「最小模式」?** —— 只做发现不做转发(比如只想当 DNS)
