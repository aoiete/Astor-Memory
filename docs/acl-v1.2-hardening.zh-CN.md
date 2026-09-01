# ACL 加固 — v1.2 (2026-09-01)

## 为什么要加固 ACL

Astor-Memory 的 ACL 是系统中**每一次读写操作的唯一守门人**。ACL 的任何一点
薄弱都是全系统的数据泄漏。我们选择在 v1.2 加固，是因为最初的 1.0/1.1 设计
有三个结构性漏洞：

1. **`astor_init_acl` 接受任何字符串作为 `actor`**。拼写错误
   （如 `'first_admin'` 写成 `'firt_admin'`）或攻击者控制的调用方
   可以悄悄地把上下文绑定到错误的身份。

2. **`tier='public'` 任何角色都可写**，包括被攻破的 `user` 或 `system` actor。
   这意味着一个泄漏的用户 token 就能往公共空间注入大量事实，
   污染跨用户的 recall。

3. **`user_id=None` 配 tier=private/repo 是「静默通过」**。调用方
   bug（忘了传正在读谁的 user）表面看起来"成功"，实际读到的是
   错的 user namespace。

这三个漏洞现在都已堵上。v1.2 **不改动其他任何东西** —— 既有的矩阵逻辑、
grant 系统、audit logger、运行时查询路径一律不动。

---

#### v1.2 的四项改动（按类别分组）

### 1. `astor_init_acl` 现在验证每一个参数

| 参数 | 改前 | 改后 (v1.2) |
|---|---|---|
| `role` | 与 `_VALID_ROLES` 比对 | 不变 |
| `tier` | 与内联 tuple 比对 | 统一到 `_VALID_TIERS` 常量 |
| `actor` | **无验证** | 必须匹配 `_ACTOR_RE = ^(first_admin\|system\|admin:<id>\|user:<id>)$` |
| `actor` / `role` 一致性 | **不检查** | 拒绝（`actor='first_admin'` 不能用 `role='user'` 跑） |
| `user_id` | 检查 None vs tier | 同时按 `_USER_ID_RE` 验证格式 |
| re-init（不同 actor） | 静默覆盖 | 写 audit log，让提权操作可见 |

规范形式记录在 `AccessContext.actor`：

- `first_admin` — 系统 root
- `admin:<id>` — 用 canonical id 标识的高权限用户
- `user:<id>` — 普通用户
- `system` — 后台任务（`am compact`、`am audit` 等）

其他任何值都是拼写错误或注入尝试，在边界处直接拒绝，不静默接受。

### 2. `tier='public'` 的写操作只对 first_admin + admin 开放

改前，`user` 和 `system` 可以写 `public`。这意味着一个泄漏的用户 token
就能往公共空间投毒，污染跨用户 recall。改后：

```python
("write", "public"): {"first_admin", "admin"},  # NOT user, NOT system
```

`user` 和 `system` 仍可**读取** public，只是不能**写**。
这与 private 的设计哲学相同：写权限才是危险的那一边，读不是。

### 3. `user_id=None` 不再是 tier=private/repo 的静默通过

`astor_check_read` 和 `astor_check_write` 现在在
`tier='private'` 且 `user_id=None` 时直接抛 `PermissionError_`：

```python
raise PermissionError_(
    f"actor={ctx.actor!r} attempted to read tier=private with user_id=None; "
    f"caller must supply the target user_id"
)
```

之前函数静默返回，调用方 bug 一直不被发现。现在第一次请求时就会
大声失败 —— 这正是你希望失败出现的时机。

### 4. re-init 写 audit log

`astor_init_acl` 在同一进程内可被多次调用（operator 可能切换上下文），
但 actor **改变**的 re-init 会写一条 `action="rebind"` 的 audit row。
这意味着——未授权调用方在两个请求之间把上下文 rebind 成 `first_admin`
来提权——现在能在 audit log 里看到。

---

#### 我们故意**没有**做的事

有些改进看起来诱人，但会破坏现有调用方或与已有机制重叠。我们**没有**做：

- **`tier='repo'` 的 per-user ACL grant**：repo 是 v1.1 的 per-git-repo
  命名空间，跨用户读本来就需要 `role='first_admin'` 才能写。加 grant
  会和 `AccessContext.actor` 里记录的 agent self-pattern 冲突。
- **基于时间的 ACL 过期**：这是另一个工作流（P12 per-action audit decay），
  在 `roadmap.md` 里跟踪。
- **`first_admin` 的角色降级路径**：方案明确禁止（`first_admin`
  不可被降级）。v1.2 保留这条规则。

---

#### 如何验证 v1.2 已生效

```python
from astor_memory._internal.acl import (
    astor_init_acl, astor_check_read, astor_check_write,
    _MATRIX, _VALID_ROLES, _VALID_TIERS,
)

# 1. 矩阵检查
assert _MATRIX[("write", "public")] == {"first_admin", "admin"}

# 2. 验证拒绝非法输入
try:
    astor_init_acl(actor="hacker", role="first_admin", tier="source")
    assert False, "必须拒绝非规范的 actor"
except ValueError:
    pass

# 3. user_id=None 抛 PermissionError_
astor_init_acl(actor="user:alice", role="user", tier="private", user_id="alice")
try:
    astor_check_read("private", user_id=None)
    assert False
except PermissionError_:
    pass
```

这三条断言都在 `tests/test_acl.py::test_v12_hardening` 里
（v1.2 ship 时加的）。

---

#### 威胁模型覆盖范围

v1.2 之后，以下攻击在 ACL 层就被挡住（不需要调用方配合）：

| 攻击 | v1.2 缓解措施 |
|---|---|
| 调用方传 `actor='hacker'` 绑定假上下文 | 被 `_ACTOR_RE` 拒绝 |
| 调用方传 `actor='first_admin' role='user'`（伪装） | 被 role 一致性检查拒绝 |
| 调用方 `astor_check_read('private', user_id=None)` 然后读到任意返回 | 抛 PermissionError_ |
| 被攻破的用户 token 写 `tier='public'` 污染跨用户 recall | role 不在 `("write","public")` 集合里 |
| 代码在两次请求间 rebind ACL 上下文实现提权 | audit row 带 `action="rebind"` |
| 随机 user_id 像 `"alice@example.com"` 绕过格式检查 | 被 `_USER_ID_RE` 拒绝 |

---

#### 涉及的文件

- `astor_memory/_internal/acl.py` — v1.2 加固
- `tests/test_acl.py` — 新增 `test_v12_hardening`，覆盖上面 6 条拒绝路径

其他模块无需改动；新验证是边界处的加法操作，
传入规范参数的现有调用方继续不受影响地工作。

---

#### 锁定

v1.2 加固：2026-09-01。
兜底：未来任何依赖「静默通过」行为的调用方，将在第一次接触时大声失败
—— 这正是目标。