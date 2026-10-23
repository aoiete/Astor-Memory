"""Evidence-gated scope assignment (v1.16.92, 2026-10-22).

背景
----
《Self-Evolving 四》说记忆系统的核心不是存储而是治理, 并给出三级晋升:

  1) 临时经验 —— 门槛最低, 可不完整, 含噪声
  2) 项目记忆 —— 跨任务验证过, 提炼成规则
  3) 独立能力 —— 稳定触发条件+步骤+验证标准, 跨项目复用

并明确要求: "错误经验一旦被证伪, 要快速降级或删除; 正确经验则应该经过
多次验证后谨慎晋升"。

astor 的 schema 本来就支持这个分层:

  - memory_canonical.scope_type 的 CHECK 允许 'short_term'
  - memory_canonical.expires_at 存在 (DATETIME)
  - memory_candidates 有 ttl_days / expires_at

但**没有任何代码用过它们**。实测 4616 条活 fact 的 scope_type 100% 是
long_term, expires_at 非空的有 0 条。于是"执行日志里的一次性观察"和
"用户明确说过要记住的规则"躺在同一个永久桶里 —— 既没有降级通道, 也
没有晋升门槛。

这个模块按**写时就能拿到的证据**决定一条 fact 落哪一层。

证据阶梯 (强 -> 弱)
------------------
| # | 证据 | scope_type | 依据 |
|---|---|---|---|
| 1 | explicit (调用方明确) | long_term | 人主动要求的 |
| 2 | provenance ∈ {manual, user_distilled, rule, operator} | long_term | 人/运营主动放进来的 |
| 3 | kind ∈ 已提炼类别 | long_term | 见下方 DISTILLED_KINDS |
| 4 | outcome ∈ {failure, lesson, success} | long_term | 文章: "失败经验单独保存" |
| 5 | capture_intent (说了"记住/切记") | long_term | 明确要求 |
| 6 | importance >= 0.7 | long_term | 抽取器自己的"重要"判决 |
| 7 | 其余 | **short_term** | 门槛最低的一层: 可能是对的, 也可能是噪声 |

第 3 条的"已提炼类别": lesson / failure_pattern / success_pattern /
rule / user_preference / decision / mental_model / knowledge /
knowledge_page / skill / reflection / correction —— 这些 kind 本身就
表示"已经被整理过"。

第 6 条为什么用 0.7: forge/extractor._score_importance 里, failure/lesson
得 0.85, success+capture_intent 得 0.8, 而 **neutral 封顶 0.6**
(长文本 0.6 / 中等 0.5 / 短 0.3)。所以 importance >= 0.7 恰好等价于
"不是 neutral" —— 是抽取器已经算好的验证信号, 不需要重新跑检测器。

为什么这样分
------------
分界线是**有没有付出过验证成本**。用户明确说"记住"、运营手工落库、
失败教训、已验证配方、抽取器判定的重要 —— 这些该长期保留。而执行过程
中顺手抽出来的一句话, 没有验证成本, 却和上面那类享受同样的永久地位
—— 那就是治理缺失。

文章说的是"经过多次验证后谨慎晋升", 所以 short_term 不是垃圾桶: 它
是**工作集**。它可以被召回, 但:

  - 每次被召回确认就**续期** (scope_lifecycle.renew_on_confirm)
  - 累计确认够多次就**升为 long_term** (scope_lifecycle.promote_recurring)
  - 过期就**降级** (scope_lifecycle.demote_expired)
  - 排序上永远低于同等相关度的 long_term (server 读路径的 scope 权重)

实测安全性 (2026-10-22, 对三个真实库跑了一遍这条规则):
admin 层 4616 条活 fact 里 50.8% 会被划进临时层, 但其中 2342/2343
**曾经被召回过** —— 也就是说这条规则几乎只会降级"从来没被用到过"的
事实, 不会误伤正在起作用的知识。真正会被降级的: admin 1 条, public
30 条, source 0 条。
"""
from __future__ import annotations

from dataclasses import dataclass

# scope_type 取值 (与 memory_canonical 的 CHECK 一致)
LONG_TERM = "long_term"
SHORT_TERM = "short_term"

# 临时层的默认存活期 (天)
DEFAULT_TTL_DAYS = 30

# 抽取器给 neutral 的 importance 上限 —— 0.7 是"不是 neutral"的分界
NEUTRAL_IMPORTANCE_CEILING = 0.6
VALIDATED_IMPORTANCE_FLOOR = 0.7

# 明确表示"人主动放进来的" provenance_kind。
# manual         —— 用户在 CLI/API 手写
# user_distilled —— 用户显式提炼
# rule           —— 系统注入的规则/教训
# operator       —— 运营手工维护 (knowledge_pages / mental_models)
EXPLICIT_PROVENANCE = frozenset({
    "manual", "user_distilled", "rule", "operator",
})

# 这些 outcome 说明"已经付出过验证成本"
VERIFIED_OUTCOMES = frozenset({"failure", "lesson", "success"})

# 这些 kind 本身就已经是提炼过的规则/结构化知识
DISTILLED_KINDS = frozenset({
    "lesson", "failure_pattern", "success_pattern", "rule",
    "user_preference", "decision", "mental_model", "knowledge",
    "knowledge_page", "skill", "reflection", "correction",
})


@dataclass(frozen=True)
class ScopeDecision:
    """一次 scope 判定的结果, 带可审计的理由。"""
    scope_type: str
    ttl_days: int | None
    reason: str

    @property
    def is_temporary(self) -> bool:
        return self.scope_type == SHORT_TERM


def classify_scope(
    *,
    kind: str | None = None,
    outcome: str | None = None,
    capture_intent: bool = False,
    provenance_kind: str | None = None,
    importance: float | None = None,
    explicit: bool = False,
    ttl_days: int = DEFAULT_TTL_DAYS,
) -> ScopeDecision:
    """按证据决定 fact 落 long_term 还是 short_term。

    参数刻意全是写时就能拿到的信号, 不依赖任何"事后"判断 (比如
    "这条后来被证明是对的") —— 那种判断属于 promote_recurring。

    explicit=True 表示调用方明确知道这是用户主动要求记住的, 优先级最高。
    """
    if explicit:
        return ScopeDecision(LONG_TERM, None, "explicit request")

    pk = (provenance_kind or "").strip().lower()
    if pk in EXPLICIT_PROVENANCE:
        return ScopeDecision(LONG_TERM, None, f"explicit provenance ({pk})")

    k = (kind or "").strip().lower()
    if k in DISTILLED_KINDS:
        return ScopeDecision(LONG_TERM, None, f"distilled kind ({k})")

    oc = (outcome or "").strip().lower()
    if oc in VERIFIED_OUTCOMES:
        return ScopeDecision(LONG_TERM, None, f"verified outcome ({oc})")

    if capture_intent:
        return ScopeDecision(LONG_TERM, None, "capture intent")

    try:
        imp = float(importance) if importance is not None else 0.0
    except (TypeError, ValueError):
        imp = 0.0
    if imp >= VALIDATED_IMPORTANCE_FLOOR:
        return ScopeDecision(
            LONG_TERM, None, f"validated importance ({imp:.2f})")

    # 门槛最低的一层: 被动抽出来、没经过任何验证的一次性观察。
    return ScopeDecision(SHORT_TERM, ttl_days, "unverified observation")


def scope_weight(scope_type: str | None) -> float:
    """读路径的排序权重: 同等相关度下 long_term 永远排在 short_term 前面。

    0.85 而不是 0: 临时经验仍然可以被召回 (它是工作集), 只是不能
    压过一条同等相关的已验证规则。权重太低会让工作集形同虚设, 太高
    又会重新变成"临时记忆和长期记忆平权" —— 那正是这个模块要修的。
    """
    return 1.0 if (scope_type or LONG_TERM) == LONG_TERM else 0.85


def expiry_from_now(ttl_days: int | None, now_iso: str) -> str | None:
    """把 TTL 换算成 expires_at (ISO 8601 UTC, 与库里其他时间戳同格式)。"""
    if not ttl_days:
        return None
    import datetime as _dt
    try:
        base = _dt.datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        base = _dt.datetime.now(_dt.timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=_dt.timezone.utc)
    return (base + _dt.timedelta(days=int(ttl_days))).isoformat().replace(
        "+00:00", "Z")
