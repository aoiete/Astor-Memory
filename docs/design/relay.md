# Astor Relay — 设计文档

**版本** v0.3（决策已定，待 review）
**日期** 2026-10-29
**状态** 待 review —— **本文只定方案，不含实现**

---

## 0. 决策摘要

本文经四轮设计推敲定稿。下表是全部已决项，review 时按这张表逐条对。

| # | 决策 | 理由 |
|---|---|---|
| D1 | **连接模式显式分离**：`direct` / `relay` 二选一，**不做自动降级** | 自动降级让排错无从下手 |
| D2 | **连接测试按钮**：端到端跑一次真搜索，输出 stage 分类 | 把「四道门」从文档变成按钮上的红字 |
| D3 | **relay 不保留数据** → 强化为：**E2E 加密，relay 内存里只有密文** | 需求要能被验证，不是靠自律 |
| D4 | **relay 公共化**：陌生人可用，**无门槛 + 强限流** | 降低接入摩擦，用机器手段防滥用 |
| D5 | **破 v0.1 的界**：relay 也转发搜索请求与结果（密文） | 不破这条界，relay 只能加好友不能互相搜 |
| D6 | **对方不在线**：内存排队 + 60s TTL | 排的是密文，relay 解不开 → 不违反 D3 |
| D7 | **relay 路由表纯内存**（重启即空，节点重新注册） | 磁盘上零路由数据 |
| D8 | **审计日志持久化**：只存「谁搜了谁」的元数据，**保留 7 天** | 滥用追溯需要；不含任何内容 |
| D9 | **公钥只在好友请求链路交换**，不做公开目录 | 公开目录 = 给 Sybil 攻击者一份可冒充名单 |
| D10 | **新增 `key_id = sha256(public_key)[:16]` 作为真正身份**，peer_id 降级为「本机实例标识」 | peer_id 与公钥无密码学绑定 → 可被冒充 |
| D11 | **fanout 与 trust 解耦**：已有的 `outbound_search` 开关**对新增 peer 默认改成 False** | 「我信他」和「让他看到我在搜什么」是两件事；开关已存在且 v1.17.3 已接进广播选择，只是默认开着。**「陌生人 trust 封顶 40」不可实现 —— 朋友与陌生人走同一条批准路径,机制上分不出来**;能分出来的只有「这条 peer 记录是不是新加的」 |
| D12 | **E2E 用临时密钥交换**（ephemeral X25519 + NaCl Box） | 零预共享、无需新增 identity 文件、前向保密 |
| D13 | **公共 relay 治理进本期**：ToS + 举报 + 手动封禁 | 公开就要能治理 |
| D14 | **支持多个 relay 地址**，按顺序 failover | relay 是单点；机制已存在 |
| D15 | **open-search 默认关**（公共 relay 上） | 唯一不需要好友关系就能搜的路径，最易滥用 |

**v0.1 → v0.3 的实质变更**：D3/D5 改变了 relay 的定位（从「只转好友请求」到「密文路由器」）；D10 改变了身份模型；D1/D2 是新增的用户可见功能。

---

## 1. 要解决的问题

两台 astor 节点要互相搜,必须知道对方的**可直连地址**:

```
astor:ea1c7c31...@astor.example.com
              └──── 地址,收件方靠它投递请求
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

## 2. 为什么直连这条路整体不存在(原理)

NAT 路由器给内网设备改写源地址,并记一张映射表:

```
内网:  192.168.1.185:1234  ──►  公网: 203.0.113.45:62001
路由器记: 203.0.113.45:62001 ↔ 192.168.1.185:1234
```

外来的包只要目的地命中这张表就能进内网,不命中就丢。**A 能出站,只意味着 A
自己那条连接的回包能回来** —— 不意味着陌生人能凭空出现在 A 的内网。

所以 A 连 B 有两道独立的坎:

1. B 没有**公网可路由的地址**(内网 `192.168.x` / CGNAT `100.64.x` 公网不可路由)
2. 就算有地址,B 的 NAT 也不放行**陌生入站**

中继不改变这个限制。它把自己放在「双方都能主动发起」的位置:

```
A --出站--> 中继 (公网可路由) <--出站-- B
```

TCP 是全双工 —— 中继可以**沿着已建立的那条连接往回写**,而这条连接的另一端
就是中继自己,于是 NAT 查表命中,放行。**中继从头到尾没有进过任何人的内网。**

**推论(实现约束)**:中继**不能**主动用「A 的 NAT 映射地址」去连 A —— 那是
一条全新入站连接,查表不命中,照样丢。所以连接必须**一直挂着**(长轮询)。

---

## 3. 定位与边界

Relay **做三件事**:

1. **会合** — 双方各自出站连 relay,relay 在两条已建立的连接之间搬字节
2. **登记与查询** — `key_id → 当前连接`、`在线状态`
3. **转发** — 好友请求、搜索请求、搜索结果(全部是密文)

**不做的事**:

- ❌ 不**存储**业务数据
- ❌ 不做身份权威 —— 身份由 Ed25519 签名 + key_id 绑定保证,relay 只是邮差
- ❌ 不**主动向任意地址发请求**(见 §8.2 —— 这条是结构约束)

### 3.1 「不保留」的准确含义:看不见,不是不落盘

请求包与响应包**全程密文**。中继在转发途中拿到的每一个字节都是对收件人以外
的人无意义的密文。

**签名 ≠ 加密** —— 签名保证「没被改」,不保证「没被读」。所以 D3 的实现是
**加密,不是自律**。

**中继仍然知道的**(无法消除,必须写进用户文档):

- 谁在线、什么时候在线
- 谁在什么时候向谁发了请求(**通讯图** —— 这是本方案最大的隐私代价)
- 请求的大小与频率

**中继不知道的**:任何 fact 内容、任何 query 原文、任何可还原的元数据。

---

## 4. 身份模型

### 4.1 现状:peer_id 与公钥没有绑定

```
peer_id = "astor:" + sha256(DB_path + first_run_ts)[:32]     # DB 绑定
identity/peer_id       落盘,重启不变
identity/public_key    ed25519 验钥
identity/private_key   ed25519 签钥 — DO NOT SHARE
```

**peer_id 是哈希,公钥是独立的 —— 两者之间没有任何密码学绑定。** 后果:
任何人可以生成自己的密钥对,声称「我是 astor:<别人的 peer_id>」。收件方第一次
遇到陌生 peer 时没有已记录的公钥可比对,**无法区分真假**。

### 4.2 v0.3:key_id 是真正的身份

```
key_id  = sha256(public_key)[:16]        # 十六进制 16 字符 = 4 块
peer_id = "astor:" + sha256(DB_path + first_run_ts)[:32]   # 降级为「本机实例标识」
```

`key_id ↔ 公钥` 是**密码学绑定**的 —— 冒充者伪造不出你的 key_id。而且 16 个
hex 短到能人工核对(4 块),不像完整 base64 公钥那样容易看错。

- **不破坏现有部署**:peer_id 不变 → 现有 peer 关系、信任分、好友码全部照常
- key_id 随 public_key 一起交换(D9:只在好友请求链路交换)

### 4.3 relay 路由表的键

```
key_id → { conn, peer_id, connected_at, generation }
```

**绝不能用 IP:端口做键** —— NAT 每次出站重新分配映射端口,家宽公网 IP 也可能
DHCP 重拨后变,每次重连地址都不同。

| 事件 | relay 该做什么 |
|---|---|
| 同一 key_id 又来一条连接 | **替换**旧条目,`generation += 1` |
| 老连接还没死透 | 只保留最新 generation —— 否则请求被送到死连接上,挂到超时 |
| key_id 变了(公钥换了 = 私钥丢了重生成) | 视为新节点;旧条目在 TTL 后清掉 |
| peer_id 变了(重装 / 搬目录) | peer_id 只是实例标识,不影响路由;好友侧由 rekey 机制处理 |

---

## 5. 协议

### 5.1 登记(register)—— 带挑战验签

```http
POST {relay}/v1/relay/register
{
  "peer_id": "astor:ea1c7c31...",
  "key_id": "3f2a9c14b7e05d62",
  "public_key": "base64...",
  "nonce": "base64...",          // relay 下发的挑战
  "signature": "base64...",      // 对 (key_id|public_key|nonce) 的 ed25519 签名
  "ttl_seconds": 3600
}
→ 200 {"ok": true, "expires_at": "..."}
```

relay 用上报的 public_key 验签,验不过直接拒 —— **不进路由表**。

**为什么 nonce 而不是直接信任上报的公钥**:不验签的话,任何人可以声称
「我是 astor:xxx」并附上自己的公钥。验签证明的是「我持有与 key_id 对应的
私钥」;而 key_id 本身由公钥派生,两者绑定(D10)。

### 5.2 查询(lookup)

```http
GET {relay}/v1/relay/lookup/{key_id}
→ 200 {
    "key_id": "...", "found": true,
    "online": true,                  // 此刻是否连着
    "peer_id": "astor:...",          // 实例标识(仅供显示)
    "expires_at": "..."
  }
→ 404 {"found": false}
```

**返回 `online` 而不是只返回地址** —— 「对方在不在中继上」独立于「对方直连
地址通不通」,UI 必须能分开显示。

**不返回 public_key** —— D9:公钥只在好友请求链路交换。

### 5.3 投递(send)—— 好友请求与搜索请求共用

```http
POST {relay}/v1/relay/send
{
  "msg_type": "friend_request" | "public_search" | "friend_pps",
  "payload": "<base64 密文>",
  "to_key_id": "...",
  "from_key_id": "...",
  "reply_to": null                // 非空 = 这是对某请求的应答
}
→ 200 {"ok": true, "queued": true}
```

relay **不解析、不修改** `payload`,原样转投。**应答走同一条路**
(`reply_to` 指向原请求 id)—— 对称,无需特殊逻辑。

### 5.4 轮询(poll)

```http
GET {relay}/v1/relay/poll/{key_id}?wait=25
→ 200 {"messages": [ {msg_type, payload, from_key_id, received_at, msg_id, reply_to}, ... ]}
```

长轮询:无消息时 hold 住最多 `wait` 秒,期间有消息立刻返回。**对方不在线
时,消息在 relay 内存里排队,TTL 60s**(D6)—— 排的是密文,relay 解不开,
不违反 D3。

### 5.5 确认(ack)

```http
POST {relay}/v1/relay/ack
{"key_id": "...", "msg_ids": ["..."]}
→ 200 {"ok": true}
```

relay 收到 ack 才真正删除。**B 崩溃不会丢消息**(至少在 ack 前还在)。

### 5.6 端到端加密的消息格式

每次请求双方各生成一对**临时** X25519 密钥(D12):

```
请求方 A:
  eph_a = X25519 随机密钥对
  payload = NaClBox(
      key = X25519(eph_a私钥, eph_b公钥),     // 双方各带一个临时公钥
      msg = 原始 peer 消息(含 ed25519 签名)
  )
  附在请求里: eph_a公钥
```

- **零预共享** —— 不需要事先交换任何长期密钥
- **每次会话独立密钥** —— 前向保密:relay 永久保存密文也解不开历史内容
- **不得降级** —— 客户端只接受加密模式,收到明文 payload 直接拒(防 relay
  投毒骗客户端回退)

**加密强度说明**:X25519 基于离散对数问题,128 位安全强度,无已知实用攻击;
量子计算需要能跑 Shor 算法的机器,不在可预见时间尺度内。真正的风险不在
「被破解」,而在客户端被入侵、端点被 MITM、以及 relay 投毒诱导降级 —— 这三
条分别由机器安全、TLS+证书校验、以及「只接受加密模式」兜住。实现一律用
PyNaCl(已在依赖里),避免自己实现曲线运算与随机数。

### 5.7 搜索经中继的完整时序

```
A: 本地 fanout 决策 → 选中 peer B(connect_mode=relay)
A → relay:  POST /send {msg_type: public_search, payload: <密文>, to: B.key_id}
relay → B:  B 的 poll 请求返回这个密文
B:  解密 → 验签(A 的 ed25519) → 判授权(trust / allow_search / outbound_search)
B:  查自己的 public tier(只出 commons)
B → relay:  POST /send {msg_type: friend_pps, payload: <密文>, to: A.key_id, reply_to: <原 id>}
relay → A:  A 的 poll 请求返回结果
A:  解密 → 验签 → 并入 peer_results
```

**全链路不依赖 B 有任何公网入口**,且 **B 的内容只以密文经过 relay**。

---

## 6. 客户端集成

### 6.1 连接模式:显式分离

peer 行新增字段:

```
connect_mode: 'direct' | 'relay'
```

| 模式 | endpoint 是什么 | 谁能搜到谁 |
|---|---|---|
| `direct` | 对方的真实可达地址(`https://b.example.com` / `192.168.1.185:7803`) | 取决于网络 |
| `relay` | `<relay 基址>/peer/<对方 key_id>` | **双方都连着 relay** |

**两种模式的 endpoint 语义完全不同,所以不混在一个列表里自动 failover。**

- v0.1 §5.2 的「直连优先、失败自动降级 relay」**取消**。原因:用户报「搜不到」
  时,自动降级让排错无从下手 —— 日志里只看到一个 IP:端口,无法判断实际走了
  哪条,极易误判成 cloudflare 那条坏了。
- 多地址 failover(同一 peer 的内网 IP + 公网 URL)**保留在 CLI 层**
  (`am peer endpoints` 已支持),**UI 先不暴露**。

**要写进用户文档**:切到 relay 模式**要求双方都连着 relay**。A 单方面切了,
B 没切,A 照样搜不到 B。

### 6.2 发送路径

```python
send(peer, msg):
  mode == 'direct' → POST peer.endpoint + <原路径>          # 现状,不动
  mode == 'relay'  → POST {relay}/v1/relay/send {to: peer.key_id, payload: 密文}
```

### 6.3 收信路径

后台线程(或复用 cron tick —— 倾向 cron tick,与现有 peer health 一致,不引入
新的常驻进程):

```
每 poll_interval:
  for relay in relays:                      # D14: 多 relay,按顺序 failover
    for msg in GET relay/v1/relay/poll/{my_key_id}:
      解密 → 按 msg_type 分发:
        friend_request → 验签 → record_friend_request(pending) → dashboard 弹窗
        public_search  → 验签 → 判授权 → 查本地 public tier → POST /send 应答
    POST relay/v1/relay/ack {msg_ids}
```

**验签复用现有的 `verify_friend_request` / `verify_rekey_message`** —— relay
送来的消息和直连来的走同一条验证路径,relay 拿不到任何额外的信任。

### 6.4 身份与 fanout 的两处改动

**D10 的 key_id**:新增列 `key_id`,在 `add_peer` / `update_peer` 时与
`public_key` 一起记录。旧的 peer 行(没有 key_id)在首次交互时自动补齐
(用对方上报的公钥算),不破坏现有关系。

**D11:已有的 `outbound_search` 开关,只改默认值 —— 不新增开关。**

这个开关**已经存在**(`metadata.outbound_search`,`peer_relationships.py:1107`),
**v1.17.3 已经把它接进广播选择**(`peer_search.py:374-390`):CLI 有
`am peer outbound-search`,REST 有 `/v1/peer/<id>/outbound-search`,dashboard
面板也在显示这一列。**所以不需要新增第三个开关** —— 要改的只有两处:

1. **默认值 True → False**。现状是 `_meta.get('outbound_search', True)`,
   于是新加的 peer **默认就在广播范围内**。
2. ~~**陌生人 trust 封顶 40**(第二道)~~ —— **做不出来**: 朋友与陌生人都走同一条
   friend_request 批准路径, 机制上分不出来。能分出来的只有「这条 peer 记录
   是不是新加的」, 所以封堵点只能是**新增 peer 默认不进广播范围**。

```
本地没命中 → 广播给哪些 peer?
  AND outbound_search != False      (现有的第二道, D11 把默认值改成关)
  AND trust >= 50                   (现有的第一道)
  AND endpoint 非空
  AND kind != 'blacklist'
```

**现状的问题(必须一起修)**:CLI 的 `--trust` 默认值是 **60**(`cli/main.py`
两处),而 server 端 `add_peer` 默认 **30**。60 >= 50 → **通过 CLI 加进来的
peer 立刻进入广播范围**,你的每一次本地未命中查询都会发给他。通过 dashboard
加的则不会。**同一个操作,两条路径安全边界不一样。**

还有第二处:`outbound_search` 的**默认值是 True**(`peer_search.py:389`
的 `_meta.get('outbound_search', True)`),所以**新加的 peer 默认就在广播
范围内** —— 哪怕它 trust 只有 30,只要对方把 trust 提上去就会开始收广播。

修法:**`outbound_search` 对新增 peer 默认改成 False**(已有的行保持用户原本
的选择, 不能替他决定);UI 上明确显示「此 peer 会收到你的未命中查询」。

**「陌生人 trust 封顶 40」这一条做不出来** —— 朋友和陌生人都走同一条
friend_request 批准路径,机制上分不出来。能分出来的只有「这条 peer 记录
是不是新加的」, 所以封堵点只能是**新增 peer 默认不进广播范围**。

**已实现 (v1.17.17)**: `add_peer` 在**新增**时把 `outbound_search=False`
写进 metadata;已有行不动;`add_peer` 的 UPDATE 路径改为合并 metadata,
不再整体覆盖(否则一次带 metadata 的更新会把 `allow_search` /
`outbound_search` 一起抹掉)。

顺带修:CLI `--trust` 的 help 文字写着 "default 30",实际是 60 —— 文档与实现
不符。

---

## 7. 连接测试按钮

### 7.1 为什么需要

现在 peer 面板只能显示**理论状态**(trust 够不够、endpoint 有没有)。用户报
「搜不到」时,四道门要一层层查,文档里那张错误码对照表得靠人对照。

**一个按钮把四道门压成一次点击** —— 这是本功能最大的价值,比 relay 本身大。

### 7.2 测试什么:不是 ping,是跑一次真搜索

测「端口通不通」没用 —— **端口通 ≠ 搜得到**(还要过授权和签名)。所以测试走
**完整的真实 peer 搜索路径**:

```http
POST /v1/peer/{key_id}/test
{"mode": "direct" | "relay"}
→ 200 {
    "ok": true, "latency_ms": 42,
    "stage": "ok", "error": null, "detail": "3 条结果",
    "relay_online": true, "peer_online": true
  }
```

**stage 分类**(直接把四道门变成红字):

| stage | 含义 | UI 显示 |
|---|---|---|
| `connect` | 连不上 | 连不上(超时 3s) |
| `sign` | 签名/线格式不对 | 签名格式不对 —— 对方版本可能不兼容 |
| `authorize` | 对方没允许你搜它 | 对方还没允许你搜它(要在对方那侧打勾) |
| `trust` | 信任度不足 | 信任度 30/50,不够 |
| `match` | 通了但没匹配到 | **通了**,只是没匹配到(不是故障) |
| `ok` | 成功 | ✅ 42ms · 3 条 |

**两个必须做对的细节**:

1. **query 带唯一 token** —— 否则「命中 0 条」会被误读成「不通」(实测:
   本地 BM25 对宽泛词照样召回,真实问题 BM25=42-100%)
2. **带真实签名** —— 否则对方返 `bad_signature`,那是格式问题不是连通性问题

### 7.3 SSRF 防护:地址只能来自 DB

> **请求里不允许出现地址。** endpoint 只能从 DB 的 peer 行取。

```
mode 只是「选这个 peer 行的哪套地址」,不是「传一个地址给我连」
→ 实际连接的 IP 必须 == DB 里那个 endpoint 解析出的 IP
```

**没有任意地址这个输入 → 没有 SSRF。** 局域网 peer 照常合法(它的地址本来
就在 DB 里),任何临时 URL 直接非法。

配套:
- 禁重定向(`follow_redirects=False`)—— 302 到内网就绕过了
- **DNS rebinding**:域名解析出的 IP 要在**连接的那一刻**再校验一次,不能只
  在解析时查一次
- 超时 ≤3s、每 peer 每分钟 ≤N 次、全局 ≤M 次
- 每次测试记 audit log(复用现有 `audit` 表的 `peer_search` action)
- **只允许 admin 触发**

### 7.4 relay 模式要测两件事

中继是会合点,**B 不在就没有连接可转**。所以 relay 模式不能只测「我能不能
连上 relay」:

| 检查 | 来源 | 失败时显示 |
|---|---|---|
| 我的 relay 连接 | 本地 relay client 状态 | 你还没连上中继 |
| **对方在 relay 上** | relay 的 lookup(online 字段) | **对方还没连中继 —— 让他也填一下** |

第二条是关键 —— 否则用户对着一个红叉猜,以为是自己这边的问题。

### 7.5 UI 草图

```
┌─ peer: astor:2d99afe… (key_id 3f2a9c14…) ──┐
│                                            │
│  ◉ 直连    ○ 经中继                          │  ← 模式二选一
│                                            │
│  地址  https://b.trycloudflare.com          │
│  [ 测试直连 ]   ● 通 · 42ms · 3 条           │
│  ⓘ 收到我的未命中查询: 否                    │  ← outbound_search
│                                            │
│  ── 或 ──                                  │
│                                            │
│  ○ 经中继                                  │
│  地址  https://relay.example.com/peer/3f2a9c14…
│  [ 测试中继 ]   ● 我已连上 · 对方也在 · 340ms │
│  ⓘ 对方也必须连着中继才能被搜到              │
└────────────────────────────────────────────┘
```

---

## 8. 安全模型

### 8.1 信任边界

| 组件 | 被信任做什么 | 不被信任做什么 |
|---|---|---|
| Relay | 存路由、当邮差 | 任何消息内容的真实性与内容 |
| ed25519 签名 | 证明消息来自持有对应私钥的节点 | — |
| key_id | 节点身份(公钥指纹) | 单独不证明持有私钥(需验签) |

**核心不变式:relay 无法伪造任何消息,也无法读到任何内容。** 收件方永远用
发送方的公钥独立验签;内容永远对 relay 是密文。

### 8.2 实现约束(必须用测试锁死,不能靠人守)

> **relay 的源码里不得出现「向任意 URL 发请求」的调用。**

```python
# ❌ 出现这行就是漏洞 —— relay 变成开放代理 / SSRF 跳板
requests.get(target_url); httpx.get(...); urllib.request.urlopen(...)

# ✅ relay 只有这一种对外能力:把数据交给一条已连接的 peer
peer_conn.send(payload)
```

**为什么这条最强**:relay 的对外能力只有 N 条**已建立的** peer 连接。它没有
「我给某个地址发 HTTP」的能力,**SSRF 在结构上不存在** —— 比任何 IP 黑名单
都可靠(黑名单永远有绕过,结构没有)。

**验收方式**:一条 grep 式断言测试,锁死 relay 模块里不出现对任意 URL 的
出站调用。半年后有人加一行「调试用直连」也不会破功。

### 8.3 风险登记表

分层原则:**先结构消除,再输入约束,最后限流兜底。** 黑名单挡不住的攻击面,
就别让它存在。

| # | 风险 | 层级 | 缓解 | 落在哪 |
|---|---|---|---|---|
| R1 | relay 被当开放代理,拿去扫别人内网 | 结构 | §8.2:没有任意 URL 出站能力 | relay 实现 + grep 测试 |
| R2 | 冒充他人 key_id / peer_id | 输入 | D10 的 key_id 绑定 + §5.1 nonce 验签 | relay register |
| R3 | relay 无限膨胀(邮箱 / 路由表) | 兜底 | 每 peer 邮箱上限 + 60s TTL + 全局配额 + 路由表容量上限 | relay |
| R4 | 恶意 peer 刷请求 | 兜底 | 复用现有 `peer_rate_limit`(1000 次/24h)与 open-search 限流(30 次/60s) | 节点侧 |
| R5 | 公共 relay 被灌满假节点 | 兜底 | 无门槛 + 强限流(D4):每 IP / 每 key_id 注册速率限制 + 注册总量上限 | relay |
| R6 | relay 拒绝转发 / 被封 / 宕机 | 可用性 | D14 多 relay failover + direct 模式仍可用 | 节点侧 |
| R7 | relay 泄露通讯图 | **接受** | 无法消除 —— relay 天然知道谁联系谁。写进用户文档 | 文档 |
| R8 | relay 看到 public tier 内容 | **已消除** | D3 端到端加密:relay 内存里只有密文 | 协议 |
| R9 | relay 被当 spam 跳板 | 结构 + 兜底 | §8.2 + §5.1 验签 + 限流 + 审计 | relay |
| R10 | 测试按钮被当内网扫描器 | 结构 | §7.3:地址只能来自 DB | 节点侧 |
| R11 | 测试污染对方 access_count / recall_log | 输入 | 测试 query 用 `__peertest__` 前缀,接收侧识别后跳过统计 | 节点侧 |
| R12 | 加了陌生人后自己的查询泄漏给他 | 结构 | D11:`outbound_search` 默认关 + trust 封顶 40 | 节点侧 |
| R13 | 客户端被入侵 / 端点被 MITM | 外部 | E2E 只保护「传输中」,不保护端点。TLS + 证书校验是底线 | 部署 |
| R14 | relay 投毒骗客户端降级成明文 | 输入 | 客户端只接受加密模式,收到明文 payload 直接拒 | 节点侧 |
| R15 | 公钥目录被灌满 / 通讯图暴露 | 输入 | D9:公钥只在好友请求链路交换,不做公开目录 | 协议 |
| R16 | `__peertest__` 前缀被伪造以逃限流 | 输入 | 限流计数必须独立于该前缀,不能靠前缀免限 | 节点侧 |

### 8.4 可用性约束(设计原则,不是可选项)

> **安全方案必须匹配用户实际能力 —— ship 一个用户不会用的方案等于零保护。**

每一条缓解都必须是**自动生效**或**一键生效**。任何需要用户配防火墙、改
路由器、开端口映射的缓解,在这个产品里等于不存在(这正是 relay 存在的理由)。

本设计里所有缓解都是自动的:验签在 relay 里做、限流在服务端做、结构约束在
代码里做。**用户不需要为安全做任何配置。**

---

## 9. 公共 relay 的治理

公共化(D4)意味着会有人用它传垃圾、传侵权内容、拿它做 spam 跳板。

| 机制 | 内容 |
|---|---|
| **ToS** | 明确写出:relay 只转发密文、不存内容、通讯图可见;禁止滥用;违反者可封禁 |
| **举报入口** | 按 key_id 举报 → relay 管理员收到 → 核实后封禁 |
| **手动封禁** | key_id 黑名单 + IP 段封禁(落盘,持久) |
| **自动限流** | 每 IP / 每 key_id 的注册速率与请求速率;超阈值自动断开 |
| **审计日志** | 「谁搜了谁」的元数据,保留 7 天(D8),**不含任何内容** |
| **open-search 默认关** | D15:唯一不需要好友关系就能搜的路径,公共 relay 上默认关闭 |

**封禁错了怎么办**:封禁是按 key_id 的,而 key_id 与公钥绑定 → 换密钥即可
绕过,所以封禁必须配合速率限制才有意义(限流治本,封禁治标)。

---

## 10. 部署

### 10.1 Relay 服务端

- **技术栈**:Python + Flask(和 astor 同栈,复用依赖管理)
- **状态**:路由表纯内存(D7);**磁盘上只有两样东西** —— 审计日志(7 天)与封禁
  黑名单。**没有任何业务数据落盘。**
- **代码位置**:`astor_memory/relay_server.py`(可独立部署,`python -m astor_memory.relay_server` 或 `am relay serve`)
- **运行**:NSSM(Windows)或 systemd(Linux VPS)

### 10.2 VPS 需求

| 项 | 需求 |
|---|---|
| CPU / 内存 | 极低,1C512M 足够 |
| 带宽 | 转发密文(几 KB/次) |
| 端口 | 443(nginx/caddy 反代 + TLS) |
| 持久化 | 审计日志 + 黑名单,需备份 |

**relay 端自己也不需要公网 IP** —— 让它只监听 `127.0.0.1`,前面套一层
cloudflared 即可(与节点侧同一个原理)。

### 10.3 备选后端

| 方案 | 适合 |
|---|---|
| 自建 relay | 完整可控,长期用 |
| Tailscale | 双方都装客户端,组网直接解决,不需要 relay |
| frp / rathole | 只需要给 NAT 后的节点开一个入口 |
| ZeroTier | 同 Tailscale,更老牌 |

**建议:优先试 Tailscale / frp —— 如果客户能装客户端,那根本不需要 relay。**
relay 的价值在于「对方什么都不用装,只要能出网」。

---

## 11. 实施计划

> **状态 (2026-10-30)**:P1-P3、P5-P10 全部已 ship —— relay 服务端(v1.17.20,含结构性约束测试)、peer 模式与加密公钥(v1.17.21)、relay 传搜索的完整往返(v1.17.21)、连接测试按钮(v1.17.22)、以及一个真的在跑的 relay(Windows 服务 `astor-relay` + 定时器 `astor-relay-poll-2min`)。**还欠的只有一件事**:把 relay 暴露到公网(隧道 ingress + DNS —— 需要你点头,因为公共 relay 的服务器会看到通讯图,那是 R7 接受但应由你知情)。(隧道 ingress + DNS —— 需要你决定,因为公共 relay 的服务器会看到通讯图,那是 R7 接受但应由你知情)。

| 阶段 | 内容 | 可独立验证 |
|---|---|---|
| **P1** | 身份:`key_id` 字段 + 派生 + 交换;peer 表加列 | 指纹能人工核对 |
| **P2** | 节点侧:peer 行加 `connect_mode`;UI 模式二选一 | 面板能切模式并落盘 | **已实现 (v1.17.21)**:connect_mode 与 peer 的 x25519 公钥存在 metadata 里;`am peer connect-mode` / `relay-key` / `relay-status` |
| **P3** | 节点侧:`POST /v1/peer/<key_id>/test` + stage 分类 + SSRF 防护 + 限流 + audit | 点按钮得到 stage | **已实现 (v1.17.22)**:跑真搜索(不是 ping);地址只从 DB 取、不跟随重定向、3 秒超时;stage = connect/sign/authorize/trust/match/ok;测试查询照样计费(prefix 不能免限流);dashboard 有 test / via relay 两个按钮 |
| **P4** | fanout 解耦:`outbound_search` 默认值改 False(新 peer)+ 陌生人 trust 封顶 + CLI `--trust` 默认修正 | 陌生人收不到未命中查询 |
| **P5** | E2E 加密:临时 X25519 + NaCl Box,加解密模块 + 单测 | 密文往返;拒绝明文降级 | **已实现 (v1.17.20)** —— 用 `cryptography` 的 X25519 + HKDF-SHA256 + ChaCha20-Poly1305 而非 NaCl Box(PyNaCl 是可选依赖,本机没装);临时键每次现生成;性质声明见 `astor_memory/_internal/relay_crypto.py` |
| **P6** | Relay 服务端:register(挑战验签)/ lookup(online)/ send / poll / ack + 审计日志 + 限流 + 黑名单 | **已实现 (v1.17.20)**:register(挑战验签,nonce 单次)/ lookup(online 与 found 分开,不返回公钥)/ send(拒绝非密文 payload)/ poll(60s TTL 队列)/ ack / 内存路由表 / 注册与发送限流 / 审计只记通讯图不记内容;黑名单属 P9 未做 |
| **P7** | 客户端 relay client:发送按 mode 分流 + 轮询线程 + 解密验签 + 应答回程 | A 经 relay 发 | **已实现 (v1.17.21)**:发送按 connect_mode 分流;`relay_node.poll_and_dispatch` 收信解密分发并应答;定时器 `astor-relay-poll-2min` 每 2 分钟跑一次;节点会在 relay 忘记它时自动重登记 |
| **P8** | 搜索经 relay 完整往返 + fanout 走 relay | 两个 NAT 后节点互相搜到 | **已实现 (v1.17.21)**:两个节点各自独立 runtime(无直连地址)通过 relay 互相搜到;personal fact 不走;未 opt-in 的 peer 被拒且给出原因 |
| **P9** | 治理:ToS + 举报入口 + 手动封禁 UI | 封禁后该 key_id 无法注册 | **已完成 (v1.17.23)**:举报即封禁(`POST /v1/relay/report`),封禁名单落盘(key_id 与地址都能封,地址才是真封禁),`am relay ban add/unban/list`;首页明说存了什么与不做什么(ToS 即该页) |
| **P10** | relay 实现约束的 grep 测试 + 多 relay failover | 断言锁死 | **已完成 (v1.17.20 / v1.17.25)**:grep 断言锁死(relay 模块不得出现 requests./httpx./urlopen/socket.create_connection);多 relay failover —— 节点按顺序试,每个 relay 带自己的凭据,`am relay add` / `am relay list` |

**P1~P4 不依赖 relay**,可以先单独 ship(测试按钮 + 模式分离立刻提升排错效率)。

### 11.1 验收标准(必须可测)

- [x] relay 模块源码不含对任意 URL 的出站调用(grep 断言) —— v1.17.20
- [x] 未验签的 register 被拒,且不进路由表 —— v1.17.20
- [x] 同一 key_id 重连后,路由表只剩最新 generation —— v1.17.20(重新登记覆盖旧路由)
- [x] relay 模式下,对方未连中继时测试返回明确原因而非超时 —— v1.17.22(stage=connect + 原因)
- [x] 测试请求里的地址被忽略,只使用 DB 里的 endpoint(断言) —— v1.17.22(请求里根本没有地址字段)
- [ ] 私网地址段的 peer(局域网)仍可被测试通过
- [x] `__peertest__` 前缀的 query 不计入对方 access_count —— v1.17.22(不写审计)
- [x] 限流计数不因 `__peertest__` 前缀而减免 —— v1.17.22(照样计费)
- [x] relay 转发途中不落盘(重启 relay 后路由表为空,邮箱只剩未 ack 的) —— v1.17.20
- [x] relay 磁盘上除审计日志与黑名单外无任何数据(断言) —— v1.17.20(审计表只有通讯图四列,无内容列)
- [x] 客户端拒绝明文 payload(降级保护) —— v1.17.20(客户端不打开 + relay 不投递)
- [x] 陌生 peer 默认收不到本地未命中查询(trust 封顶 + outbound_search 默认关) —— v1.17.17
- [x] key_id 与公钥匹配才被接受(冒充测试) —— v1.17.20

### 11.2 风险

| 风险 | 缓解 |
|---|---|
| 客户端轮询线程引入并发问题 | 复用 `agent_hygiene_async` 已有的后台线程模式;或用 cron tick 而非常驻线程 |
| relay 单点 | D14 多 relay failover + direct 模式仍可用 |
| 长时间运行内存增长 | 邮箱 60s TTL + 路由表容量上限 |
| 公共 relay 带宽被刷 | 限流 + 审计 + 封禁;必要时改收费 |
| 协议复杂度上升 | E2E 与 relay 是两层,先各自单测再集成 |

---

## 12. 待确认

1. **公共 relay 的收费模式?** —— 本期先免费 + 限流;若被刷爆,再考虑微量收费
   或押金作为可选升级(D4 选了无门槛)。
2. **审计日志里要不要记 relay 自己的 IP?** —— 记了能查滥用来源,也是元数据。
3. **`__peertest__` 前缀是否要防伪造?** —— 外部也能发这个前缀,接收侧跳过
   统计会被用来免限流;限流计数必须**独立于**这个前缀。
