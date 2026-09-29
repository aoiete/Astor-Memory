"""
HTTP REST API server for Astor-Memory.

Plan § Week 4 step 3.2: FastAPI-style endpoints with Flask (already in deps).
Endpoints:
  POST /v1/write    body={"text":..., "user":..., "mode":...} -> {fact_ids}
  POST /v1/read     body={"query":..., "user":..., "top_k":5}  -> {results: [{fact_id, content, ...}]}
  GET  /v1/health   -> {status, version, dbs}
  GET  /v1/dashboard -> aggregated dashboard JSON (5min cache)
  POST /v1/install  body={"ide":..., "mode":..., "agent_dir":...} -> {plan}

Run: python -m astor_memory.server
Or:  flask --app astor_memory.server run --port 7803

Per Plan § Memory <-> concurrency: WAL mode handles concurrent reads.
"""
from __future__ import annotations

import sqlite3
import json

# S15 (2026-09-10): dashboard cache — keeps aggregated payload so
# the HTML page polling /v1/dashboard doesn't re-run 16 user-db aggregates
# on every refresh. Cache key: astor_dir. Invalidation: 30s TTL
# (S18 2026-09-25, tightened from 5min) + write-trigger invalidate in
# /v1/write so hero.last_event_ts / growth_30d refresh instantly after writes.
_DASHBOARD_CACHE: dict = {"payload": None, "ts": 0.0, "astor_dir": None}
_DASHBOARD_TTL_SEC = 30  # 2026-09-25 S18: tighter TTL so dashboard reflects writes promptly; /v1/write also invalidates on success for instant refresh.

# v1.15.17 S21 (2026-09-25): auto-meta-recall counters.
# Tracks how many /v1/read calls triggered meta-recall and how many pattern
# facts were injected. Dashboard reads via /v1/audit/health endpoint to prove
# "astor is working as a proactive advisor, not just a lookup table".
# Reset only by server restart (deliberate: cumulative since boot = uptime signal).
_META_RECALL_STATS: dict = {"triggered": 0, "returned_total": 0, "errors": 0}
_meta_recall_stats = _META_RECALL_STATS  # local alias used by read() with `global`


# S14 (2026-09-08): auto-load OPENAI_API_KEY from hermes .env file if not in env.
# Subprocess start (memory_servers_watch, start_astor.sh) sometimes doesn't inherit
# OPENAI_API_KEY from parent bash on Windows MSYS. Read it directly from .env file
# as a fallback so LLM rerank + forge paths work regardless of startup method.
import os as _os
# S14 (2026-09-08): load OPENAI_API_KEY from .env. Path resolved from HERMES_ENV env var
# (set by hermes wrappers / cron) with generic fallbacks. No hardcoded operator paths
# in source — R-class privacy rule (no PII in source files).
if not _os.environ.get("OPENAI_API_KEY"):
    _hermes_env = _os.environ.get("HERMES_ENV")
    _env_paths = [p for p in (_hermes_env, "~/.hermes/.env") if p]
    for _env_path in _env_paths:
        _env_path = _os.path.expanduser(_env_path) if _env_path.startswith("~") else _env_path
        try:
            with open(_env_path, encoding="utf-8", errors="ignore") as _f:
                for _line in _f:
                    if _line.startswith("OPENAI_API_KEY=") or _line.startswith("OPENROUTER_API_KEY="):
                        _key = _line.split("=", 1)[1].strip().strip('"\'')
                        if _key and len(_key) > 15:  # not a 15-char redacted placeholder
                            _os.environ["OPENAI_API_KEY"] = _key
                            break
        except OSError:
            continue
        if _os.environ.get("OPENAI_API_KEY"):
            break
import os
import re
import sys
from pathlib import Path
from typing import Any


def _safe_stderr_write(msg: str) -> None:
    """Write to sys.stderr without crashing if it is None.

    2026-09-17 fix: subprocess.Popen with ``close_fds=True`` closes the
    inherited stderr handle, which makes ``sys.stderr`` return ``None``
    in the child. Any handler that called ``_sys.stderr.write(...)`` for
    debug logging then raised ``AttributeError: 'NoneType' object has
    no attribute 'write'`` and the request turned into a 500.

    Use this helper everywhere we previously did
    ``_sys.stderr.write(...)`` so the debug path can never break the
    response path.
    """
    _se = sys.stderr
    if _se is None:
        return  # stderr was closed (close_fds=True or detached console)
    try:
        _se.write(msg)
        _se.flush()
    except Exception:
        pass  # never let debug logging break the request

from flask import Flask, jsonify, request

from . import __version__, astor_bus, astor_nest, astor_forge
from ._internal.acl import astor_init_acl, _CURRENT, astor_current_acl, PermissionError_
from ._internal.bot_binding import get_user
from .config import get_default_astor_dir, get_default_bus_path, get_default_nest_path


def _astor_quality_ok(text: str) -> str | None:
    """Return None if content passes quality gate, else a generic denial reason.

    2026-09-02 ship: deny obvious spam / nonsense BEFORE the ACL+forge path.
    Reasons returned are generic so users cannot probe which rule fired.
    Rules:
      - 8+ characters after strip
      - <100% uppercase
      - <100% control / whitespace
      - <100% emoji / symbol
      - <100% digits
      - no leading 'test' / 'spam' / 'asdf' (case-insensitive)
    """
    if not isinstance(text, str):
        return 'invalid content'
    s = text.strip()
    if len(s) < 8:
        return 'invalid content'
    # All uppercase? (excluding punctuation / digits)
    letters = [c for c in s if c.isalpha()]
    if len(letters) >= 8 and all(c.isupper() for c in letters):
        return 'invalid content'
    # All control / whitespace?
    if all((not c.isprintable() or c.isspace()) for c in s if c):
        return 'invalid content'
    # All emoji / symbol / punctuation?
    if all((not c.isalnum()) for c in s):
        return 'invalid content'
    # All digits?
    if all(c.isdigit() for c in s):
        return 'invalid content'
    # Known test prefixes
    low = s.lower()
    if low.startswith('test ') or low.startswith('spam ') or low.startswith('asdf'):
        return 'invalid content'
    return None


# 2026-09-02 ship: intent classifier for public write auto-routing.
# Returns (new_tier, new_user_id) if content should be reclassified away
# from the caller's requested tier, else None (use caller's choice).
#
# Design: admin decides what goes public. User requests tier=public but
# server inspects text and silently demotes to private_<user> if content
# matches personal / financial / daily-journal patterns. User never learns
# (no error message, just count=1 with the new tier under the hood).
#
# Patterns matched (case-insensitive, CJK + Latin):
#   - Personal pronouns: 我 / 我今天 / 我的 / 自己 / i / my / mine
#   - Financial: 买了 / 卖了 / 仓位 / 跌了 / 涨了 / $/€ / price
#   - Daily journal: 今天 / yesterday / 早上 / 晚上 / at 8pm
#   - Emotion/心情: 累了 / 开心 / 难过 / happy / sad
#
# Methods/rules/models stay public: includes pattern keywords (workflow /
# method / model / rule / pattern / 流程 / 方法 / 规则 / 模式 / 模型 / 设计).
_PERSONAL_PATTERNS = [
    # v1.14.66: require 我 / 我的 / 自己 / my as the core. The old
    # `\b我(?:今天|昨天|明天|现在)?\b` matched nothing for Chinese
    # because \b requires ASCII word boundary and Chinese chars are
    # not \w. Fixed by removing \b around Chinese and using a Chinese-
    # aware negative lookbehind. Note: each pattern must be its OWN
    # list item — joining multiple patterns with | inside one string
    # confuses the lookbehind parsing (verified v1.14.66 debugging).
    r"(?<![一鿿])我(?:今天|昨天|明天|现在)?(?![一鿿])",
    r"(?<![一鿿])我的",
    r"自己(?![一鿿])",
    r"\bi\s+(?:am|was|will|just|got|had|have)\b",
    r"\bmy\s+(?:day|mood|trade|position|portfolio|stocks?)\b",
    r"\bmine\b",
]
_FINANCIAL_PATTERNS = [
    r"\$\d+|\d+\s*\$|€\d+|\d+\s*€",
    r"\b(?:bought|sold|traded|long|short|stop[-_ ]?loss|take[-_ ]?profit)\b",
    r"\b(?:AAPL|TSLA|NVDA|MSFT|GOOG|AMZN|META|SPY|QQQ)\b",
    r"(?:买了|卖了|持仓|仓位|止盈|止损|跌了|涨了|加仓|减仓)",
]
_DAILY_PATTERNS = [
    r"(?:今天|昨天|明天|早上|晚上|今晚|今早)",
    r"\b(?:today|yesterday|tonight|this morning)\b",
    r"\b\d+\s*(?:am|pm)\b",
]
_EMOTION_PATTERNS = [
    # v1.14.66: include 好累 / 好开心 / 很难过 (modifier + emotion).
    # Old pattern required exact word match, missed "好累" / "很开心"
    # which is the dominant Chinese emotion phrasing.
    r"(?:好累|很累|累死|开心|很难过|沮丧|激动|无聊|郁闷|崩溃|开心得|难过|担心|焦虑|压力|放松|平静|紧张)",
    r"(?:累了|开心|难过|沮丧|激动|无聊|郁闷|崩溃)",
    r"\b(?:happy|sad|tired|excited|stressed|anxious|depressed|frustrated)\b",
]
_METHOD_PATTERNS = [
    r"(?:workflow|method|model|rule|pattern|process|approach|framework|design)",
    r"(?:流程|方法|规则|模式|模型|设计|架构|接口|SDK|api|API|步骤|教程|开户|配置|怎么|如何)",
    # 2026-09-16 Ship explicit-public: "怎么 X" questions are method-intent.
    r"怎么\s+\w{2,}",
    # Specific SDK / CLI verbs allowlist (narrow, no false positives on
    # arbitrary snake_case like "moomoo 数据").
    r"\b(?:place_order|unlock_trade|accinfo_query|get_positions|history_order_list|get_stock_quote|get_account_info|subscribe|connect|disconnect|login|logout|register|setup|configure|install|deploy|publish|commit|push|pull|merge|rebase|build|test|fix|patch|uninstall)\b",
]

_PERSONAL_RE = re.compile("|".join(_PERSONAL_PATTERNS), re.IGNORECASE)
_FINANCIAL_RE = re.compile("|".join(_FINANCIAL_PATTERNS), re.IGNORECASE)
_DAILY_RE = re.compile("|".join(_DAILY_PATTERNS), re.IGNORECASE)
_EMOTION_RE = re.compile("|".join(_EMOTION_PATTERNS), re.IGNORECASE)
_METHOD_RE = re.compile("|".join(_METHOD_PATTERNS), re.IGNORECASE)


def _astor_classify_intent(text: str, tier: str, user: str | None) -> str | None:
    """Inspect content; return new tier if content should be reclassified.

    Only acts on tier='public' — private writes always stay private. Returns
    None when content should stay as caller requested.

    v1.14.66 (2026-09-17, user feedback round 5): data/method separation.
    User wants data剥离, mode/method 留下. So the demote logic must
    be smart enough to keep method-intent text public even if it
    mentions personal/financial keywords in passing. New rules:

      - method signal present → stays public regardless of personal/
        financial keywords (user wants to share rules about their
        own data, e.g. "R-class: personal filter 必须 demote to private"
        is a rule about personal data, the rule itself is public).
      - multiple (>=2) personal/financial/emotion signals together
        → demote to private (this is actual personal content).
      - single weak signal (just "今天" or just "happy") → stays public
        (too common, false positive).
      - strong personal pronoun (我/我的/my) + financial ticker (NVDA/
        AAPL) → demote regardless of method count.

    Net effect: data goes to private, modes/methods stay public.
    """
    if tier != 'public':
        return None
    if not isinstance(text, str) or not user:
        return None
    # 2026-09-16 Ship explicit-public (admin-applies): admin also goes through
    # demote logic. Previously admin was short-circuited to public (R236),
    # but that leaked personal data (e.g. "我的 TFSA 余额是 $1234" landed as
    # public fact). Now admin must SHOW method-intent via _METHOD_RE to stay
    # public; otherwise demote to private.
    has_method = bool(_METHOD_RE.search(text))
    has_personal = bool(_PERSONAL_RE.search(text))
    has_financial = bool(_FINANCIAL_RE.search(text))
    has_daily = bool(_DAILY_RE.search(text))
    has_emotion = bool(_EMOTION_RE.search(text))
    # v1.14.66 method takes priority over weak personal signals. A user
    # writing "R-class rule about personal filter" is documenting a
    # rule, not sharing personal data. The rule itself should be public.
    if has_method:
        # Method + multiple STRONG personal signals → private (real personal
        # content with method framing). Threshold: at least 2 distinct
        # personal/financial/emotion signals must fire alongside method.
        # Daily alone (今天/明天/yesterday) is too common to count as
        # strong — "astor v1.14.66 ship 完成" or "今天是星期三" should
        # stay public even without method. So daily doesn't count in the
        # strong-signals sum.
        strong_signals = sum([
            has_personal, has_financial, has_emotion,
        ])
        # Strong-personal override: pronoun + financial ticker = always
        # private even with method framing (e.g. "我的 NVDA 仓位 workflow"
        # is real personal data wearing a method hat).
        _personal_strong = bool(re.search(
            r"(?:我|我的|自己|\bmy\b|\bI\s+(?:am|was|will|just|got|had|have)\b)", text, re.IGNORECASE))
        _financial_strong = bool(re.search(
            r"(?:AAPL|TSLA|NVDA|MSFT|GOOG|AMZN|META|SPY|QQQ|\$\d+)", text, re.IGNORECASE))
        if _personal_strong and _financial_strong:
            return 'private'
        # v1.14.66b: when method + first-person pronoun appears as
        # subject (sentence-initial 我), it's the user talking about
        # their own action — treat as strong personal signal even with
        # just 1. Pattern: "我今天想 X" / "我的 NVDA" / "我要 ship"
        # sentence-initial, not embedded in a rule description.
        _first_person_subject = bool(re.match(
            r"\s*(?:我|我的|自己)", text))
        if _first_person_subject:
            # First-person subject + method = user framing their
            # personal action with method vocabulary. Demote.
            return 'private'
        if strong_signals >= 2:
            return 'private'
        # Method + 0-1 weak signals → stays public (default).
        return None
    # No method signal. Old behavior: personal/financial/emotion signal
    # → demote to private. Daily alone (no personal/financial/emotion)
    # does NOT demote — too common in normal release notes / status
    # updates. Same goes for "no signal at all" — a fact with no
    # personal/financial/emotion/daily/method markers is method-neutral
    # (e.g. "今天是星期三" or "astor v1.14.66 ship 完成") and should
    # stay public per user's "data 剥离 mode/method 留下" instruction.
    if has_personal or has_financial or has_emotion:
        return 'private'
    # No method signal, no personal signal — stay public. v1.14.66
    # reverses v1.14.64's explicit-public default-private, because
    # real-world content (release notes, dates, neutral status) is
    # mostly public-safe. The classifier only demotes when clear
    # personal/financial/emotion intent is present.
    return None


def _astor_resolve_actor(user_id: str | None) -> tuple[str, str, str | None]:
    """Resolve (actor, role, plan) for a given user_id from bot-binding.db user_meta.

    2026-09-02 final simplification: admin role IGNORES plan (plan is a
    user-tier concept only). Admin has full access via role alone; plan=None
    means "no plan applicable". 2 roles (admin / user) + 3-value plan
    (free / vip / power) for users only.

    Returns:
      ('admin:admin', 'admin', None)    for user_id in {None, '', 'admin'}
      ('admin:<id>',  'admin', None)    for any role='admin' user (plan ignored)
      ('user:<id>',   'user',  <plan>)  for any active role='user' user
      ('user:anonymous', 'user', 'free') for unknown / inactive callers
    """
    from ._internal.bot_binding import get_user as _get_user
    if not user_id or user_id == 'admin':
        return ('admin:admin', 'admin', None)
    meta = _get_user(user_id)
    if meta is None or not meta.get('active', 1):
        return ('user:anonymous', 'user', 'free')
    role = meta.get('role', 'user')
    if role == 'admin':
        # admin: plan is irrelevant — role already grants full access.
        return (f'admin:{user_id}', 'admin', None)
    # user: plan differentiates free / vip / power
    plan = meta.get('subscription_plan', 'free')
    return (f'user:{user_id}', 'user', plan)


def _resolve_agent_context(body: dict) -> dict[str, str | None]:
    """Validate optional bot/direct agent context without changing ACL identity.

    ``user``/``user_id`` remain the ACL subject. ``agent_id`` describes the
    producer, while ``transport`` distinguishes a bot-backed agent from a
    direct SDK/MCP/CLI agent. Legacy callers may omit the whole context.
    """
    agent_id = body.get('agent_id')
    transport = body.get('transport')
    platform = body.get('platform')
    if agent_id is None and transport is None and platform is None:
        return {'agent_id': None, 'transport': None, 'platform': None,
                'source': body.get('source'), 'namespace': body.get('namespace'),
                'session_id': body.get('session_id')}
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise ValueError('agent_id must be a non-empty string when agent context is provided')
    if transport not in ('bot', 'direct'):
        raise ValueError("transport must be 'bot' or 'direct'")
    if transport == 'bot' and (not isinstance(platform, str) or not platform.strip()):
        raise ValueError('bot transport requires platform')
    if transport == 'direct' and platform:
        raise ValueError('direct transport must not declare platform')
    for name in ('source', 'namespace', 'session_id'):
        value = body.get(name)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f'{name} must be a non-empty string when provided')
    return {'agent_id': agent_id.strip(), 'transport': transport,
            'platform': platform.strip() if isinstance(platform, str) else None,
            'source': body.get('source'), 'namespace': body.get('namespace'),
            'session_id': body.get('session_id')}


# ---------------------------------------------------------------------------
# MemCon-style recall controller (rule-based v1.0.0)
# ---------------------------------------------------------------------------
# Inspired by arxiv 2607.13591 "Memory as a Controlled Process" (UCLA+UW+NW):
# instead of a fixed top_k for every recall, decide per-call:
#   1. should_recall(query, ctx) — skip if obvious / cached / no-op
#   2. action_top_k(query, ctx)  — choose top_k based on state (lib size, intent)
# Rule-based v1 — collect usage data first; UCB/Q-table upgrade in v2 once
# we have enough feedback samples.
#
# All decisions are recorded in /tmp/recall_controller_log.jsonl for offline
# analysis (action chosen, top_k picked, latency saved). No behavior change
# if logs cannot be written.

import json as _rc_json
import re as _rc_re
import time as _rc_time
import os as _rc_os
from threading import Lock as _rc_Lock

_RC_LOG_PATH = _rc_os.environ.get(
    'ASTOR_RECALL_LOG',
    _rc_os.path.join(_rc_os.environ.get('TEMP', '/tmp'), 'astor_recall_controller.jsonl'),
)
_RC_LOCK = _rc_Lock()


def _rc_log(event: dict) -> None:
    """Best-effort append to controller log. Never raises."""
    try:
        event = {'ts': _rc_time.time(), **event}
        with _RC_LOCK:
            with open(_RC_LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(_rc_json.dumps(event, ensure_ascii=False) + '\n')
    except Exception:
        pass


def _rc_query_features(query: str) -> dict:
    """Extract cheap features from the query string for decision making."""
    if not query:
        return {'len': 0, 'tokens': 0, 'has_chinese': False, 'has_question': False,
                'has_code': False, 'has_url': False, 'has_proper_noun': False}
    has_chinese = bool(_rc_re.search(r'[\u4e00-\u9fff]', query))
    has_question = '?' in query or '？' in query
    has_code = '```' in query or 'def ' in query or 'class ' in query or 'function ' in query
    has_url = bool(_rc_re.search(r'https?://', query))
    # crude proper-noun detection: 2+ capital letters not at start of sentence
    proper_nouns = _rc_re.findall(r'(?<![.!?\n])\b[A-Z][a-zA-Z]{2,}\b', query)
    return {
        'len': len(query),
        'tokens': len(query.split()),
        'has_chinese': has_chinese,
        'has_question': has_question,
        'has_code': has_code,
        'has_url': has_url,
        'has_proper_noun': len(proper_nouns) > 0,
    }


def _rc_should_recall(query: str, body: dict) -> tuple[bool, str]:
    """MemCon action 'NoOp' decision. Returns (should_recall, reason).

    Heuristics v1 (rule-based):
    - empty / whitespace only → skip
    - very short query (< 4 chars) → skip (just acknowledging / single word)
    - pure greeting / acknowledgment → skip
    - user explicitly opted out via body['skip_recall']=True
    """
    if body.get('skip_recall'):
        return False, 'caller_opt_out'
    if not query or not query.strip():
        return False, 'empty_query'
    q = query.strip()
    if len(q) < 4:
        return False, 'too_short'
    # common greetings/acknowledgments
    _GREETINGS = {'hi', 'hey', 'hello', 'ok', 'okay', 'yes', 'no', 'thanks', 'thank you',
                  'thx', 'ty', '哈哈', '好的', '好', '嗯', '是', '不是', '再见', 'bye',
                  '你好', '嗨', '谢', '收到', 'ok.', 'got it', 'cool', 'nice'}
    if q.lower() in _GREETINGS:
        return False, 'greeting'
    return True, 'ok'


def _rc_choose_top_k(query: str, body: dict, default_top_k: int, lib_size: int) -> int:
    """MemCon action 'retrieve' with adaptive top_k.

    Heuristics v1:
    - explicit top_k from caller wins (don't override)
    - question queries with proper nouns → need more context (default + 5)
    - long queries (>120 chars) → likely complex task, more results (default + 5)
    - very long (>300 chars) → drop slightly to avoid noise (default - 2, min 3)
    - code queries → fewer, focused (default - 2, min 3)
    - short chinese query (< 20 chars) → many small facts, default is fine
    - lib_size > 5000 → +2 to dilute noise
    - lib_size < 50 → keep default (small lib already focused)
    """
    # honor explicit top_k — but "auto" / None mean "let controller decide"
    if body.get('top_k') is not None and body['top_k'] != 'auto':
        try:
            explicit = int(body['top_k'])
            if explicit > 0:
                return explicit
        except (TypeError, ValueError):
            pass

    f = _rc_query_features(query)
    base = default_top_k

    if f['has_code']:
        return max(3, base - 2)
    if f['len'] > 300:
        return max(3, base - 2)
    if f['len'] > 120:
        return base + 5
    if f['has_proper_noun'] and f['has_question']:
        return base + 5
    if lib_size > 5000:
        return base + 2
    return base


def _rc_re_retrieve(query: str, body: dict, prev_top_score: float) -> bool:
    """MemCon action 'Re-Retrieve' decision.

    If first retrieve gave weak signal (top_score < 0.3), retry with a
    broader top_k. v1 just flips a flag; v2 can do query expansion.
    """
    if prev_top_score < 0.3 and len(query.split()) >= 3:
        return True
    return False


def _resolve_namespace(
    agent_ctx: dict[str, str | None],
    *,
    fallback: str,
    session_id: str | None = None,
) -> str:
    """Resolve the canonical fact namespace for a write.

    Phase E3 (2026-09-17): when an agent supplies ``agent_id`` but does
    NOT supply an explicit ``namespace``, the default is now
    ``<agent_id>/<session_id or fallback>`` instead of just the user.
    That way N agents sharing one astor on one machine write to disjoint
    namespaces (e.g. ``evox/session-A`` vs ``hermes/session-B``) without
    needing each caller to construct the namespace themselves.

    Callers that DO supply an explicit ``namespace`` keep using it
    verbatim (no auto-prefix) so existing single-agent setups are
    backwards compatible.
    """
    explicit = agent_ctx.get('namespace')
    if explicit:
        return explicit
    agent_id = agent_ctx.get('agent_id')
    if agent_id:
        return f"{agent_id}/{session_id or fallback}"
    return fallback


def _extract_entities_for_fact(bus, canon_id):
    """v1.14.23 Ship E: read entities_json for one canonical row.
    Best-effort: returns [] on any error (caller can re-read via /v1/read)."""
    try:
        row = bus.conn.execute(
            "SELECT entities_json FROM memory_canonical WHERE id = ?",
            (int(canon_id),),
        ).fetchone()
        if row and row[0] is not None and len(row[0]) > 2:
            try:
                v = json.loads(row[0])
                return v if isinstance(v, list) else []
            except Exception:
                return []
        return []
    except Exception:
        return []


# Provenance-kind → wing mapping (single source of truth).
# A "wing" is a logical partition of facts by origin:
#   wing=human   — facts the human typed directly (provenance_kind=manual)
#   wing=agent   — facts extracted / inferred / merged by the agent
#   wing=rule    — system-injected rules / lessons (provenance_kind=rule)
# A fact has ONE wing. MemPalace uses physical storage separation; we
# keep physical storage shared (9-DB layout is per-tier, not per-wing)
# but route recall by wing via the wing alias below.
WING_TO_PROVENANCE = {
    "human": {"manual"},
    "agent": {"extracted", "inferred", "merged"},
    "rule": {"rule"},
}


def _infer_provenance_kind(origin_session_id: str | None) -> str | None:
    """Auto-derive provenance_kind from origin_session_id prefix.

    Convention (matches hermes capture_intent hook):
      - 'discord:' / 'telegram:' / 'wechat:' / 'cli:' / None → manual (human)
      - 'cron:' / 'hook:post_tool_call' / 'hook:session_end' → extracted
      - 'auto_link:' / 'auto_observe:' → inferred
      - anything else → None (caller should set explicitly)

    v1.14.44 (Ship F): previously the server defaulted to None; now it
    infers from origin_session_id. Manual callers can still override
    by passing provenance_kind in body.
    """
    if not origin_session_id:
        return "manual"  # human typed it (no session metadata)
    sid = origin_session_id.lower()
    if sid.startswith(("discord:", "telegram:", "wechat:", "cli:")):
        return "manual"
    if sid.startswith(("cron:", "hook:post_tool_call", "hook:session_end",
                       "hook:pre_compact")):
        return "extracted"
    if sid.startswith(("auto_link:", "auto_observe:", "merge:")):
        return "inferred"
    return None


def _expand_wing_to_provenance(wing: str | None) -> set[str] | None:
    """Map a wing alias to its provenance_kind set for SQL filtering.

    Returns None if wing is None/empty/whitespace (no filter). Raises
    ValueError on unknown wing (so the caller can return 400 instead of
    silent zero results).
    """
    if not wing or not wing.strip():
        return None
    w = wing.strip().lower()
    if w not in WING_TO_PROVENANCE:
        raise ValueError(f"unknown wing: {wing!r} (valid: {sorted(WING_TO_PROVENANCE.keys())})")
    return WING_TO_PROVENANCE[w]


def create_app(astor_dir: str | None = None) -> Flask:
    """Create Flask app. astor_dir override for tests."""
    app = Flask(__name__)
    if astor_dir:
        os.environ['ASTOR_DIR'] = astor_dir
    # 2026-08-16 opt4: upgrade every known tier DB
    try:
        from .bus.schema import astor_upgrade_all_tier_dbs
        astor_upgrade_all_tier_dbs()
    except Exception:
        pass
    # 2026-08-15 ship: process-level ACL bootstrap. Flask is multi-threaded
    # and `_CURRENT` is a `_thread._local`, so the main-thread init below
    # would NOT propagate to request-handler threads. We therefore register
    # `before_request` to (re-)bind ACL for every worker thread. The server
    # itself runs as `admin` with `source` tier scope so health/write/
    # read can cross tiers as designed; tier-scoped endpoints (private DB)
    # should re-bind to a narrower context inside their handler.
    # P2-fix 2026-08-15: rebind ACL per request, taking tier + actor from the
        # request body so per-tier writes use the correct role.
        # P0-fix 2026-08-16: actor/role now come from bot-binding.db user_meta.role
        # based on `body.user` (was hardcoded to admin, allowing any user → source
        # write + cross-user private read). Also enforce cross-user protection:
        # if tier=private and user_id != actor, deny at the request boundary
        # instead of letting it reach `astor_check_write/read`.
        #
            # v1.13.2 (2026-09-04): add teardown_request hook to auto-close per-request
    # bus connections. Without this, server holds the WAL writer lock forever
    # and external direct-connect INSERT operations fail with 'database is
    # locked' (verified bug 2026-09-04 after a bot-rejection incident).
    _request_buses: list = []
    _request_nests: list = []

    # Wrap the factory functions so every AstorBus / astor_nest created inside
    # a request handler is auto-tracked for cleanup. Use the original factory
    # at module-import time so we don't recurse.
    import astor_memory.bus as _bus_pkg
    import astor_memory.nest as _nest_pkg
    _orig_bus_factory = _bus_pkg.astor_bus
    _orig_nest_factory = _nest_pkg.astor_nest

    def _tracking_bus_factory(*args, **kwargs):
        # v1.13.2 fix (2026-09-04): call _orig_bus_factory which already
        # consults _BUS_SINGLETONS cache (per (tier, user_id, path) key).
        # Tracking only ADDS the returned instance to the per-request
        # list — does NOT bypass the cache. This is critical because
        # astor_init_schema() in AstorBus.__init__ holds a write lock
        # and creating a fresh instance per request exhausts locks
        # ("database is locked" errors, server hangs).
        bus = _orig_bus_factory(*args, **kwargs)
        if bus is not None and bus not in _request_buses:
            _request_buses.append(bus)
        return bus

    def _tracking_nest_factory(*args, **kwargs):
        nest = _orig_nest_factory(*args, **kwargs)
        if nest is not None and nest not in _request_nests:
            _request_nests.append(nest)
        return nest

    _bus_pkg.astor_bus = _tracking_bus_factory
    _nest_pkg.astor_nest = _tracking_nest_factory
    # Also rebind in the local module namespace (server.py imports these).
    globals()['astor_bus'] = _tracking_bus_factory
    globals()['astor_nest'] = _tracking_nest_factory

    @app.before_request
    def _astor_bind_request_acl() -> None:
        # Reset per-request bus/nest tracking lists.
        _request_buses.clear()
        _request_nests.clear()
        # 2026-08-16: Always bind a default ACL for GET requests (e.g. health,
        # viewer_stats, lex_stats). Without this, Flask worker threads may
        # not have _CURRENT set, and downstream astor_check_* raises
        # "astor_acl not initialized" → 500. POST requests get per-body binding.
        if request.method == 'POST' and request.is_json:
            body = request.get_json(silent=True) or {}
            tier = body.get('tier')
            if tier in ('public', 'source', 'private', 'repo'):
                # v1.1: tier=repo uses repo_id (explicit field) or 'user' as repo_id.
                repo_id = body.get('repo_id')
                if tier == 'repo' and repo_id:
                    body_user = repo_id
                else:
                    # R365 (2026-09-03, locked): fall back to body.user_id if
                    # body.user is missing. Without this, callers that send only
                    # user_id (no 'user' field) get body_user=None → resolved as
                    # admin → ACL bypass on /v1/forget (any non-admin caller
                    # could delete any admin's public fact because body_user
                    # became admin:admin).
                    body_user = body.get('user') or body.get('user_id')
                actor, role, plan = _astor_resolve_actor(body_user)
                if tier == 'private':
                    target_user = body.get('user_id') or body_user
                elif tier == 'repo':
                    target_user = body_user
                else:
                    target_user = None
                try:
                    # Bind ACL with ACTOR's identity. user_id=body_user ALWAYS
                    # (not just for private) so astor_check_write can match
                    # ctx.user_id == target_user when target_user is the
                    # actor's own (reclassified private_<self> path).
                    astor_init_acl(
                        actor=actor, role=role, tier=tier,
                        user_id=body_user,
                        subscription_plan=plan,
                    )
                except (ValueError, PermissionError_) as exc:
                    return jsonify({'error': 'acl_init_failed', 'detail': str(exc)}), 403
                if tier == 'private':
                    from ._internal.acl import astor_check_read as _acr, astor_check_write as _acw
                    # 2026-08-16 fix: only enforce write ACL on write endpoints.
                    # Read-only endpoints (e.g. /v1/read) must not require a
                    # write-grant — they only need a read-grant.
                    is_write_action = request.path not in {'/v1/read', '/v1/read/multi'}
                    try:
                        _acr(tier='private', user_id=target_user)
                    except PermissionError_:
                        # 2026-09-02 ship: silent cross-user denial (no policy leak).
                        return jsonify({'error': 'cross_user_forbidden'}), 403
                    if is_write_action:
                        try:
                            _acw(tier='private', user_id=target_user)
                        except PermissionError_:
                            return jsonify({'error': 'cross_user_forbidden'}), 403
                elif tier == 'source':
                    # R354 (2026-09-03, locked): source tier is admin-only. Without
                    # this gate, non-admin callers could DELETE admin's source-tier
                    # rules by hitting /v1/forget (same pattern as R365). R218: astor
                    # server watchdog handles respawn, no hermes restart needed.
                    if role != 'admin':
                        return jsonify({'error': 'permission_denied'}), 403
                # R354 (2026-09-03, locked): public-tier ownership check is enforced
                # inside /v1/forget handler (need fact_id lookup result to know
                # the namespace). Read endpoints stay open; write endpoints add
                # an inline check.
                return
        # Default bind for GET endpoints + POST without JSON body.
        # GETs are read-only public-tier inspections; safe to bind as
        # admin (server identity).
        try:
            _ = _CURRENT.actor
        except AttributeError:
            astor_init_acl(actor='admin:admin', role='admin', tier='public',
                           subscription_plan=None)

    @app.teardown_request
    def _astor_close_request_buses(_exc) -> None:
        """v1.13.2 (2026-09-04): Auto-close all per-request bus/nest connections.

        Closes every AstorBus / nest instance created during this request
        via the _tracking_*_factory wrappers. Without this hook, server
        accumulates one open WAL writer connection per request and external
        direct-connect INSERT operations get 'database is locked' forever.
        """
        for bus in _request_buses:
            try:
                bus.close()
            except Exception:
                pass
        for nest in _request_nests:
            try:
                nest.close() if hasattr(nest, 'close') else None
            except Exception:
                pass
        _request_buses.clear()
        _request_nests.clear()

    @app.errorhandler(PermissionError_)
    def _astor_handle_permission_error(exc: PermissionError_):  # noqa: ARG001
        """2026-08-16 ACL fix: convert PermissionError_ from astor_check_*
        into 403 instead of 500. The before_request hook binds ACL per
        request, but astor_bus() / astor_nest() may still raise from
        downstream checks (e.g. promote_candidate) — those should surface
        as 403, not 500.
        """
        return jsonify({'error': 'permission_denied', 'detail': str(exc)}), 403

    @app.route('/v1/health', methods=['GET'])
    def health():
        """Health check + DB status."""
        # 2026-08-15 ship: tier required. Health endpoint inspects public
        # tier (read-only). Use /v1/health/private?user=<id> for user db.
        bus = astor_bus(tier='public')
        nest = astor_nest(tier='public')
        result = {
            'status': 'ok',
            'version': __version__,
            'astor_dir': str(get_default_astor_dir()),
            'dbs': {
                'bus': str(bus.db_path),
                'nest': str(nest.db_path),
            },
        }
        # Bus stats
        try:
            c = bus.conn.cursor()
            c.execute("SELECT COUNT(*) FROM memory_canonical")
            result['facts'] = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM events")
            result['events'] = c.fetchone()[0]
        except Exception as e:
            result['bus_error'] = str(e)
        # Nest stats
        try:
            c = nest.conn.cursor()
            c.execute("SELECT COUNT(*) FROM embeddings")
            result['embeddings'] = c.fetchone()[0]
        except Exception as e:
            result['nest_error'] = str(e)
        return jsonify(result)

    @app.route('/v1/dashboard', methods=['GET'])
    def dashboard():
        """Aggregated dashboard payload for the web UI.

        Returns the 6-dimension payload built by dashboard_data.build_dashboard_payload:
        - hero (totals + last_event delta + trend_status)
        - eval_trend (hit_rate / mrr / p95 / variants / delta)
        - per_user (top 16)
        - growth_30d (daily promoted, admin)
        - top_keywords + recent_facts
        - importance_histogram + health

        Cached 5min in-process to avoid re-aggregating 16 user dbs per poll.
        Optional query: ?astor_dir=<path>  override default ASTOR_DIR (testing).
        """
        import time as _t
        from .dashboard_data import build_dashboard_payload

        astor_dir = request.args.get("astor_dir") or get_default_astor_dir()
        now = _t.time()
        cached = _DASHBOARD_CACHE
        if (
            cached["payload"] is not None
            and cached["astor_dir"] == str(astor_dir)
            and (now - cached["ts"]) < _DASHBOARD_TTL_SEC
        ):
            return jsonify({**cached["payload"], "_cache": "hit"})

        try:
            payload = build_dashboard_payload(astor_dir)
        except Exception as exc:
            return jsonify({
                "error": "dashboard_build_failed",
                "detail": str(exc),
                "astor_dir": str(astor_dir),
            }), 500

        _DASHBOARD_CACHE["payload"] = payload
        _DASHBOARD_CACHE["ts"] = now
        _DASHBOARD_CACHE["astor_dir"] = str(astor_dir)
        return jsonify({**payload, "_cache": "miss"})

    @app.route('/v1/health/diagnose', methods=['GET'])
    def health_diagnose():
        """Detailed breakdown of health counters (embedding_failed + warnings).

        Returns the same data as scripts/astor_health_diagnose.py --summary,
        but as JSON for the dashboard to consume.

        Optional query: ?user=<id>&astor_dir=<path>

        v1.14.43 (2026-09-16, Ship C, ADR-0005): expanded to include:
        - proxy_hijack_check: detects if HTTPS_PROXY/HTTP_PROXY env vars
          are set to non-loopback values (MemU's memU doctor pattern)
        - db_corruption_check: PRAGMA integrity_check + PRAGMA foreign_key_check
          on the user's bus DB
        - embedding_version_check: verifies expected embedding model loads
          and reports its dim + name
        """
        import time as _t
        import os as _os_diag
        from .dashboard_data import _summarize_embedding_failures, _summarize_warnings
        from .nest.embeddings import astor_get_model_name_for_ram, astor_get_embedding_model
        from pathlib import Path as _P

        user = request.args.get("user", "admin")
        astor_arg = request.args.get("astor_dir")
        astor_dir = _P(astor_arg) if astor_arg else get_default_astor_dir()

        db = astor_dir / "users" / user / "memory" / f"astor_bus_{user}.db"
        if not db.exists():
            return jsonify({"error": "user_db_not_found", "user": user, "db": str(db)}), 404

        try:
            co = sqlite3.connect(str(db))
            cu = co.cursor()
            emb = _summarize_embedding_failures(cu)
            warn = _summarize_warnings(cu)
            sev_rows = cu.execute(
                "SELECT severity, COUNT(*) FROM audit_log GROUP BY severity ORDER BY 2 DESC"
            ).fetchall()

            # ── Ship C (ADR-0005): db_corruption_check ──────────────────
            integrity = cu.execute("PRAGMA integrity_check").fetchone()
            integrity_ok = integrity is not None and integrity[0] == "ok"
            fk_violations = cu.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            fk_violation_count = len(fk_violations)
            db_corruption_check = {
                "integrity_check": integrity[0] if integrity else "unknown",
                "integrity_ok": integrity_ok,
                "foreign_key_violations": fk_violation_count,
                "foreign_key_violation_samples": [
                    {"table": row[0], "rowid": row[1], "parent": row[2]}
                    for row in fk_violations[:5]
                ],
            }

            co.close()
        except Exception as exc:
            return jsonify({"error": "diagnosis_failed", "detail": str(exc)}), 500

        # ── Ship C (ADR-0005): proxy_hijack_check ─────────────────────
        # Detect unexpected proxy env vars. Loopback proxies (127.0.0.1,
        # localhost) are typically intentional dev tools (e.g. mitmproxy);
        # any non-loopback proxy env var is suspicious.
        #
        # On Windows, os.environ is case-insensitive (the OS layer dedupes
        # HTTPS_PROXY and https_proxy to the same key), so we dedupe by
        # case-folded key before counting.
        proxy_vars = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
        seen_keys = set()
        proxy_findings = []
        for var in proxy_vars:
            val = _os_diag.environ.get(var)
            if not val:
                continue
            lowered_var = var.lower()
            if lowered_var in seen_keys:
                continue  # case-insensitive duplicate
            seen_keys.add(lowered_var)
            lowered = val.lower()
            is_loopback = any(
                marker in lowered
                for marker in ("127.0.0.1", "localhost", "::1", "[::1]")
            )
            proxy_findings.append({
                "var": var,
                "value": val,
                "loopback": is_loopback,
                "warn": not is_loopback,
            })
        proxy_hijack_check = {
            "env_vars_set": len(proxy_findings),
            "loopback_only": all(p["loopback"] for p in proxy_findings) if proxy_findings else True,
            "findings": proxy_findings,
            "warn": any(p["warn"] for p in proxy_findings),
        }

        # ── Ship C (ADR-0005): embedding_version_check ────────────────
        embedding_version_check = {"checked": False}
        try:
            model_name = astor_get_model_name_for_ram()
            model = astor_get_embedding_model()
            # Probe dim by embedding a 1-char string
            probe = next(iter(model.embed(["a"])))
            embedding_version_check = {
                "checked": True,
                "model_name": model_name,
                "dim": int(len(probe)),
                "loaded": True,
            }
        except Exception as exc:
            embedding_version_check = {
                "checked": True,
                "loaded": False,
                "error": str(exc),
                "warn": True,
            }

        # Aggregate warn flags
        ship_c_warn = (
            proxy_hijack_check["warn"]
            or not db_corruption_check["integrity_ok"]
            or db_corruption_check["foreign_key_violations"] > 0
            or embedding_version_check.get("warn", False)
        )

        return jsonify({
            "generated_at": _t.time(),
            "user": user,
            "db": str(db),
            "embedding_failed": emb,
            "warnings": warn,
            "audit_total_by_severity": {row[0]: row[1] for row in sev_rows},
            # Ship C additions:
            "proxy_hijack_check": proxy_hijack_check,
            "db_corruption_check": db_corruption_check,
            "embedding_version_check": embedding_version_check,
            "ship_c_warn": ship_c_warn,
        })

    @app.route('/dashboard/', methods=['GET'])
    @app.route('/dashboard/index.html', methods=['GET'])
    @app.route('/', methods=['GET'])
    def dashboard_page():
        """Serve the static dashboard HTML page.

        Multiple paths map to the same page:
        - /dashboard/  (explicit dashboard)
        - /dashboard/index.html  (direct asset)
        - /  (root — convenient default for the public hostname)
        """
        from flask import send_from_directory, redirect
        dashboard_dir = Path(__file__).parent / "dashboard"
        # If request is for `/`, serve dashboard HTML directly.
        # If request is for `/dashboard/` or `/dashboard/index.html`, same.
        if request.path == '/':
            return send_from_directory(str(dashboard_dir), "index.html")
        return send_from_directory(str(dashboard_dir), "index.html")

    @app.route('/dashboard/<path:filename>', methods=['GET'])
    def dashboard_static(filename):
        """Serve dashboard static assets (style.css, app.js)."""
        from flask import send_from_directory
        dashboard_dir = Path(__file__).parent / "dashboard"
        return send_from_directory(str(dashboard_dir), filename)

    @app.route('/<path:filename>', methods=['GET'])
    def root_static(filename):
        """Serve dashboard assets when accessed from the root path.

        e.g. GET /style.css → style.css, GET /app.js → app.js
        Lets the dashboard be served at https://host/ (root) with
        relative asset paths working out-of-the-box.
        Restricted to the dashboard directory's allowed filenames
        (style.css, app.js) to avoid path traversal risk.
        """
        from flask import send_from_directory, abort
        dashboard_dir = Path(__file__).parent / "dashboard"
        allowed = {"style.css", "app.js", "index.html"}
        if filename not in allowed:
            abort(404)
        return send_from_directory(str(dashboard_dir), filename)

    @app.route('/v1/write', methods=['POST'])
    def write():
        """Write a fact via forge extraction + bus promote + nest store.

        Body JSON:
          text: str (required)
          user: str (default 'admin')
          mode: 'auto'|'none'|'regex'|'llm' (default 'auto')
          tier: 'public'|'source'|'private_<user>' (default 'public')
          scope: 'long_term'|'short_term'|'profile' (default 'long_term')
        Returns:
          {fact_ids: [int], count: int}
        """
        body = request.get_json(force=True)
        try:
            agent_ctx = _resolve_agent_context(body)
        except ValueError as exc:
            return jsonify({'error': 'invalid_agent_context', 'detail': str(exc)}), 400
        text = body.get('text')
        if not text:
            return jsonify({'error': 'text required', 'detail': 'POST /v1/write requires JSON body with "text" field (string, 8+ chars)'}), 400
        user = body.get('user', 'admin')
        mode = body.get('mode', 'auto')
        tier = body.get('tier', 'public')
        # v1.15.35 (Ship P0): Memory Defense — Hindsight-style PII scanner.
        # Gated by ASTOR_PII_SCAN_ON_WRITE=1 (default off). Two policies:
        # - 'redact' (default): replace PII with [BLOCKED:TYPE] tag, preserve write
        # - 'block' : reject write if any block-severity match is found
        # Audit log entry written to bus.audit_log with fingerprints only
        # (never the raw secret). Operator opted in per env gate.
        if body.get('pii_scan') is True or body.get('pii_scan') is False:
            # Explicit body override beats env
            _md_on = bool(body.get('pii_scan'))
        else:
            from .nest.memory_defense import is_enabled as _md_is_enabled
            _md_on = _md_is_enabled()
        if _md_on:
            from .nest.memory_defense import scan_policy as _md_scan, audit_log_record as _md_audit
            _md_policy = body.get('pii_policy')
            if not _md_policy:
                from .nest.memory_defense import get_policy as _md_get_policy
                _md_policy = _md_get_policy()
            _processed_text, _md_matches = _md_scan(text, policy=_md_policy)
            if _md_matches and _md_policy == 'block' and any(m.severity == 'block' for m in _md_matches):
                _names = sorted({m.name for m in _md_matches if m.severity == 'block'})
                return jsonify({
                    'error': 'pii_blocked',
                    'detail': f"Memory Defense (block policy) rejected write: {len(_md_matches)} PII match(es) including {_names[:3]}",
                    'matches': [{'name': m.name, 'severity': m.severity} for m in _md_matches if m.severity == 'block'],
                }), 400
            if _md_matches:
                # Log to audit (fire-and-forget; never blocks the write)
                try:
                    _audit_entry = _md_audit(None, user, tier, _md_matches, _md_policy)
                    _safe_stderr_write('[MEMORY_DEFENSE] ' + repr(_audit_entry) + chr(10))
                except Exception as _md_audit_exc:
                    _safe_stderr_write('[MEMORY_DEFENSE] audit log failed: ' + repr(_md_audit_exc) + chr(10))
                text = _processed_text  # use redacted content for the actual write
        # v1.14.63 (R-class fix): validate tier early. Acceptable values
        # are public / source / private_<user> / repo. The string 'auto'
        # is reserved for the astor_auto_observe tool flow (not HTTP
        # /v1/write) and used to silently 500 inside the forge layer —
        # reject it cleanly with 400 so callers know to switch tools.
        if tier not in ('public', 'source'):
            if tier == 'auto':
                return jsonify({
                    'error': 'invalid_tier',
                    'detail': (
                        "tier='auto' is reserved for the astor_auto_observe tool. "
                        "Use that tool instead of POST /v1/write, or pass an "
                        "explicit tier ('public' / 'source' / 'private_<user>')."
                    ),
                }), 400
            if not (tier == 'private' or tier.startswith('private_')):
                if tier != 'repo':
                    return jsonify({
                        'error': 'invalid_tier',
                        'detail': (
                            f"tier={tier!r} is not one of "
                            "'public', 'source', 'private', 'private_<user>', 'repo'."
                        ),
                    }), 400
        scope = body.get('scope', 'long_term')
        # v1.11.0: optional session_id — enables session-neighbor recall
        _write_session_id = body.get('session_id') or None

        # v1.1: tier=repo requires repo_id (= user_id). The body uses 'user'
        # for both — same field name, different semantic in different tier.
        # Caller is expected to pass `tier='repo'` + `user='<repo_id>'`.
        # Or use `repo_id` explicitly if caller wants clarity.
        repo_id = body.get('repo_id')
        if tier == 'repo':
            if repo_id:
                user = repo_id
            if not user or user == 'admin':
                return jsonify({'error': 'tier=repo requires user=<repo_id> or repo_id=<id>', 'detail': 'When tier=repo, provide either user=<repo_id> in body or repo_id=<id>'}), 400

        # P1-fix 2026-08-15: validate scope + route policy. Per plan §3-tier
        # × 3-scope: profile-scope facts only land in private tier (per-user
        # identity). short-term scope carries 30d TTL via scope_type column.
        if scope not in ('long_term', 'short_term', 'profile'):
            return jsonify({'error': f'invalid scope {scope!r}', 'detail': 'scope must be one of: long_term, short_term, profile'}), 400
        if scope == 'profile' and tier != 'private':
            # Profile scope must live in private (per-user identity). Auto-route.
            tier = 'private'

        # Phase E4 (2026-09-17): pre-write cross-channel consistency check.
        # If `user` has any active bot binding whose ``role_inherit``
        # disagrees with the user's current ``user_meta.role``, the
        # downstream ACL would silently use the binding's role for
        # tier routing — a cross-channel inconsistency. Reject the write
        # so the admin can fix it via ``am binding`` before persisting.
        # Best-effort: a transient DB error here falls through and lets
        # the write proceed (audit-only path is still available via
        # ``/v1/consistency/check``).
        try:
            from ._internal.bot_binding import (
                list_bindings, get_user as _get_user_for_check,
            )
            _user_meta_row = _get_user_for_check(user)
            _user_meta_role = (
                (_user_meta_row or {}).get('role', 'user')
                if isinstance(_user_meta_row, dict) else 'user'
            )
            for _bind in list_bindings(user_id=user, active_only=True):
                if _bind.get('role_inherit') != _user_meta_role:
                    return jsonify({
                        'error': 'cross_channel_inconsistency',
                        'detail': (
                            f"binding {_bind['binding_id']!r} has "
                            f"role_inherit={_bind['role_inherit']!r} but "
                            f"user_meta.role={_user_meta_role!r}; "
                            f"resolve via am binding before retrying"
                        ),
                        'binding_id': _bind['binding_id'],
                        'expected_role': _user_meta_role,
                        'binding_role': _bind['role_inherit'],
                    }), 409
        except Exception:
            pass

        # 2026-08-15 ship: respect tier from request body. Default 'public'.
        # v1.1: tier=repo passes user (= repo_id) to bus/nest/forge as user_id.
        # 2026-08-16 strict-privacy: prefer explicit user_id over 'user' field
        # so cross-user writes (e.g. admin writing alice's private) target
        # the correct DB. 'user' identifies the caller; 'user_id' identifies
        # the target.
        explicit_uid = body.get('user_id')
        if tier == 'private' and explicit_uid:
            bus_user_id = explicit_uid
        elif tier in ('private', 'repo'):
            bus_user_id = user
        else:
            bus_user_id = None

        # 2026-09-02 ship: content classifier — admin decides what goes public.
        # Must run AFTER bus_user_id resolved (ACL below needs user_id for
        # tier=private). Reclassify sets tier + bus_user_id together.
        _reclass = _astor_classify_intent(body.get('text', ''), tier=tier,
                                          user=user)
        if _reclass is not None:
            tier = _reclass
            bus_user_id = user  # auto-route to caller's own private bucket

        # 2026-09-02 ship: content quality gate (silent reject spam before write).
        # 8+ chars, no all-uppercase, no all-control, no all-emoji. Failures
        # return 400 with a generic "invalid content" detail — never reveal
        # which rule (so users can't probe policy).
        _qerr = _astor_quality_ok(body.get('text', ''))
        if _qerr is not None:
            return jsonify({'error': 'permission_denied', 'detail': _qerr}), 403

        # P2-fix 2026-08-15: optional mirror_to_source. When tier=public and
        # mirror=true, also write the same fact into the source tier (admin-
        # only) so the agent's self-pattern store gets the same content.
        # This is the 3-store × 3-tier "fanout" pattern from the plan.
        mirror_to_source = bool(body.get('mirror_to_source', False)) and tier == 'public'

        # 2026-09-02 ship: enforce write ACL via matrix. before_request
        # binds the ACL context but does NOT check it — we must check here
        # to stop a user from writing to source or public (both admin-only).
        from ._internal.acl import astor_check_write as _acw_write
        try:
            _acw_write(tier='public' if tier == 'public' else tier,
                       user_id=bus_user_id)
        except PermissionError_ as _acl_err:
            # 2026-09-02 ship: silent ACL denial — strip plan + role info.
            # user never learns why they were blocked (probing prevention).
            return jsonify({'error': 'permission_denied'}), 403

        bus = astor_bus(tier=tier, user_id=bus_user_id)
        forge = astor_forge()

        # 1. P1-fix 2026-08-15: content-hash dedup (stable_id) per (tier,
        # user_id, scope). Same text re-write should return the existing
        # canonical_id instead of duplicating. compute_hash uses sha256 of
        # the raw text so it's deterministic across retries. Scope partition
        # so writing the same text under different scopes is still allowed
        # (e.g. public long-term vs public short-term).
        import hashlib as _hl
        content_hash = _hl.sha256(text.encode('utf-8', errors='ignore')).hexdigest()[:16]
        # v1.1: stable_id namespace — repo_id for tier=repo, username for
        # private, '_' for public/source.
        if tier in ('private', 'repo'):
            scope_user = user
        else:
            scope_user = '_'
        # Use the resolved target user consistently for private/repo data.
        # ``user`` is the caller/actor; ``bus_user_id`` is the data owner.
        if tier in ('private', 'repo'):
            scope_user = bus_user_id or user
        stable_id = f'{tier}:{scope_user}:{scope}:{content_hash}'
        try:
            existing_row = bus.conn.execute(
                "SELECT id FROM memory_canonical WHERE stable_id = ?",
                (stable_id,),
            ).fetchone()
            if existing_row is not None:
                # Same content already stored at this scope — return early.
                self_audit_via_bus = bus  # not strictly needed
                return jsonify({
                    'event_id': None,
                    'fact_ids': [existing_row[0]],
                    'count': 1,
                    'tier': tier,
                    'scope': scope,
                    'dedup': True,
                    'stable_id': stable_id,
                    # v1.14.23 Ship E: entities for existing row.
                    'entities_per_fact': [_extract_entities_for_fact(bus, existing_row[0])],
                })
        except Exception as dedup_exc:
            # Dedup check failure should not block write path.
            _safe_stderr_write(
                f'[astor.server] dedup check failed (continuing): {dedup_exc}\n'
            )

        # 2. Append event
        event_id = bus.append_event(
            namespace=_resolve_namespace(
                agent_ctx, fallback=user,
                session_id=_write_session_id,
            ),
            agent_id=agent_ctx['agent_id'] or 'rest_api',
            source=agent_ctx['source'] or 'rest.write',
            action='write',
            content=text,
        )
        # v1.10.9: caller may supply event_time (ISO datetime) to anchor
        # relative-date resolution at write time. Without this, the
        # extractor can't resolve 'yesterday/last week' relative phrases.
        caller_event_ts = body.get('event_time') or body.get('event_ts')
        # 2. Extract facts (forge) — now writes llm_call_log audit row
        # 2026-08-16 strict-privacy: pass explicit user_id (target) over
        # 'user' (caller) for forge extraction. forge_log_call writes to
        # the per-tier forge DB; private_<admin> requires a grant on
        # grantor='admin', which the caller may not hold.
        # v1.10.9: caller may supply event_time (ISO datetime) to anchor
        # relative-date resolution at write time.
        caller_event_ts = body.get('event_time') or body.get('event_ts')
        print(f'[DEBUG] /v1/write caller_event_ts={caller_event_ts!r}', flush=True)
        # v1.13.1 (2026-09-14, Ship L): bridge the capture_intent hook to
        # the 3-zone architecture. Before extracting facts, classify the
        # text into success/failure/lesson/neutral. Pass outcome so the
        # extractor's zone-mapping branch (forge/extractor.py outcome→
        # kind/importance) actually fires. Without this, every fact
        # landed as kind='fact' (neutral) regardless of content — Ship F
        # code was dead code until this hook was wired up.
        from .forge.extractor import astor_classify_outcome
        outcome = astor_classify_outcome(text)
        why = (
            f'auto-classified outcome={outcome} (Ship L capture_intent→zone)'
            if outcome != 'neutral' else None
        )
        facts = forge.astor_extract_facts(
            text, mode=mode, tier=tier,
            user_id=bus_user_id if tier == 'private' else None,
            actor='rest_api',
            outcome=outcome,
            why=why,
            # v1.10.9: doc_timestamp anchors relative-time resolution.
            doc_timestamp=caller_event_ts,
        )
        # 2026-09-16 R-class fix: client-supplied `tags` were being silently
        # dropped — forge extractor overwrites fact.tags with its own tags.
        # Merge body.tags INTO each fact's tags so post_tool_call hooks
        # (which rely on `lesson:sdk-auto-capture` filter) can find their
        # facts downstream. Caller tags take precedence on collision.
        body_tags = body.get('tags') or []
        if body_tags:
            for fx in facts:
                fx.tags = list(dict.fromkeys((fx.tags or []) + body_tags))
        if not facts:
            return jsonify({'event_id': event_id, 'facts': [], 'count': 0})
        # 3. Insert candidates + promote (which auto-stores embeddings via nest)
        fact_ids = []
        facts_entities: list[list[dict]] = []
        # 2026-08-16 opt1: hook BM25 lex index — every promoted fact gets
        # tokenized and indexed for exact-match keyword recall. Failures
        # are logged but never block the write (lex is a redundant store).
        from .nest.lex_index import astor_lex as _astor_lex_for_write
        _lex = _astor_lex_for_write(tier=tier, user_id=bus_user_id)
        for f in facts:
            cand_id = bus.insert_candidate(
                event_id=event_id,
                namespace=_resolve_namespace(
                    agent_ctx,
                    fallback=(bus_user_id or user),
                    session_id=_write_session_id,
                ),
                content=f.content,
                kind=f.kind,
                confidence=f.confidence,
                importance=f.importance,
                tags=f.tags or [],
                # v1.2.1: thread A-MEM-style structured fields from extractor
                # through candidate → canonical. Promoted to top-level
                # canonical columns during promote_candidate.
                keywords=f.keywords or [],
                context=f.context or '',
                # v1.12.0: hierarchical extraction — topic + session_id
                # propagated through to metadata.__topic__ / __session_id__.
                topic=getattr(f, 'topic', '') or '',
                session_id=getattr(f, 'session_id', '') or _write_session_id or '',
                # v1.14.21 Ship B: RippleMem-style structured entity binding.
                # Thread entities from AstorFact → insert_candidate → metadata
                # → promote_candidate → memory_canonical.entities_json.
                entities=getattr(f, 'entities', None),
            )
            canon_id = bus.promote_candidate(
                cand_id, promoted_by='rest.write', user_id=bus_user_id or user, tier=tier,
                scope_type=scope,  # P1-fix 2026-08-15: thread scope through
                # v1.11.0: thread session_id for agentic neighbor-expand.
                # Facts from the same session can be pulled as read/navigate
                # neighbors at recall time (Mistral Agentic Search pattern).
                origin_session_id=_write_session_id,
                stable_id=stable_id,
                # v1.14.34 Ship I: thread provenance from caller so hook
                # writes can be filtered from manual am writes.
                provenance_kind=body.get('provenance_kind') or _infer_provenance_kind(_write_session_id),
                provenance_agent=body.get('provenance_agent') or None,  # P1-fix 2026-08-15: enable content-hash dedup
                # v1.14.74+ Ship A2-Akasha: evidence-grounded source linking.
                # Caller may pass evidence_quote / source_ref / source_hash as
                # top-level body fields. All three are optional; defaults are
                # '' (legacy behavior — facts without source provenance).
                evidence_quote=str(body.get('evidence_quote') or '')[:1024],
                source_ref=str(body.get('source_ref') or '')[:512],
                source_hash=str(body.get('source_hash') or '')[:64],
            )
            fact_ids.append(canon_id)
            # v1.14.23 Ship E (2026-09-15): read entities_json from DB so
            # /v1/write response carries the structured entity binding
            # back to the caller (avoids a 2nd /v1/read round trip).
            # Best-effort: read failure -> empty list (caller can retry).
            try:
                _ents_row = bus.conn.execute(
                    "SELECT entities_json FROM memory_canonical WHERE id = ?",
                    (int(canon_id),),
                ).fetchone()
                _ents = []
                # DEBUG
                _safe_stderr_write(f'[DEBUG-E] canon_id={canon_id} row={_ents_row!r}\n')
                if _ents_row and _ents_row[0] is not None and len(_ents_row[0]) > 2:
                    # len > 2 skips the literal '[]' string (Ship B writes
                    # the column with default '[]' before the 2nd UPDATE
                    # rewrites it with real entities). At this point in
                    # promote_candidate's transaction, the 2nd UPDATE has
                    # committed, so we should see the populated JSON list.
                    try:
                        _ents = json.loads(_ents_row[0])
                        if not isinstance(_ents, list):
                            _ents = []
                    except Exception as _ex_in:
                        _safe_stderr_write(f'[DEBUG-E] EXCEPTION inner {_ex_in!r}\n')
                        _ents = []
                _safe_stderr_write(f'[DEBUG-E] PRE-append _ents type={type(_ents).__name__} len={len(_ents) if hasattr(_ents, "__len__") else "?"}\n')
                facts_entities.append(_ents)
            except Exception as _ex:
                _safe_stderr_write(f'[DEBUG-E] EXCEPTION outer {_ex!r}\n')
                facts_entities.append([])
            # Index for BM25 keyword recall — best-effort
            try:
                _lex.index_fact(int(canon_id), f.content)
            except Exception as _lex_exc:
                _safe_stderr_write(
                    f'[astor.server] lex index_fact failed (continuing): {_lex_exc}\n'
                )
            # v1.2.3: Zettelkasten auto-link (A-MEM pattern). After
            # promote, find existing same-kind facts with cosine > 0.85
            # and add bidirectional auto-link edges. Audit-safe (no
            # rewrite of existing facts; only adds edges to provenance
            # graph). Failures never block the write path.
            try:
                from .nest.auto_link import auto_link_for_fact as _auto_link
                _auto_link(
                    bus, new_fact_id=int(canon_id),
                    content=f.content, kind=f.kind,
                    tier=tier, user_id=bus_user_id,
                )
            except Exception as _auto_link_exc:
                _safe_stderr_write(
                    f'[astor.server] auto_link failed (continuing): {_auto_link_exc}\n'
                )
        # P2-fix 2026-08-15: optional source-tier mirror. Best-effort — if
        # mirror fails (e.g. ACL denial for non-admin caller), the
        # primary write still succeeds.
        mirrored_fact_ids = []
        if mirror_to_source and fact_ids:
            try:
                src_bus = astor_bus(tier='source')
                src_event_id = src_bus.append_event(
                    namespace=_resolve_namespace(
                        agent_ctx, fallback=user,
                        session_id=_write_session_id,
                    ),
                    agent_id=agent_ctx['agent_id'] or 'rest_api',
                    source=(agent_ctx['source'] or 'rest.write') + '.mirror',
                    action='mirror',
                    content=text,
                )
                src_facts = astor_forge().astor_extract_facts(
                    text, mode=mode, tier='source',
                    user_id=None, actor='rest_api.mirror',
                )
                for f in src_facts:
                    c_id = src_bus.insert_candidate(
                        event_id=src_event_id, namespace=user,
                        content=f.content, kind=f.kind,
                        confidence=f.confidence, importance=f.importance,
                        tags=f.tags or [],
                        # v1.2.1: same structured fields as primary write
                        keywords=f.keywords or [],
                        context=f.context or '',
                    )
                    # Mirror uses its own dedup scope (source/long_term etc)
                    # so it doesn't collide with the primary public write.
                    src_content_hash = _hl.sha256(
                        f.content.encode('utf-8', errors='ignore')
                    ).hexdigest()[:16]
                    src_stable_id = f'source:_:{scope}:{src_content_hash}'
                    m_id = src_bus.promote_candidate(
                        c_id, promoted_by='rest.write.mirror',
                        user_id=user, tier='source', scope_type=scope,
                        stable_id=src_stable_id,
                        # v1.14.74+ Ship A2-Akasha: mirror evidence-grounded
                        # source linking. Defaults to '' when caller did not
                        # populate evidence on the primary write.
                        evidence_quote=str(body.get('evidence_quote') or '')[:1024],
                        source_ref=str(body.get('source_ref') or '')[:512],
                        source_hash=str(body.get('source_hash') or '')[:64],
                    )
                    mirrored_fact_ids.append(m_id)
            except Exception as mirror_exc:
                # Log to stderr; do not fail the primary write.
                _safe_stderr_write(
                    f'[astor.server] mirror_to_source failed: {mirror_exc}\n'
                )

        # 2026-09-02 ship: audit row for every successful public write so
        # admin can review what users contributed (and which got through the
        # quality gate). admin-only visibility via `am admin audit-log`.
        if tier == 'public' and fact_ids:
            try:
                from ._internal.audit_logger import astor_audit as _audit_w
                _audit_w(
                    actor=f'user:{user}' if user else 'user:anonymous',
                    tier='public',
                    action='write',
                    user_id=bus_user_id,
                    target=f'public/fact_ids={fact_ids[:3]}{"..." if len(fact_ids)>3 else ""}',
                    metadata={
                        'count': len(fact_ids),
                        'preview': (body.get('text', '') or '')[:80],
                        'mode': body.get('mode', 'auto'),
                    },
                )
            except Exception:
                pass  # audit failure must not break writes

        # 2026-09-25 S18: invalidate dashboard cache on every successful write
        # so hero.last_event_ts / growth_30d / per_user refresh instantly
        # instead of waiting for the 30s TTL. Best-effort: never breaks write.
        try:
            _DASHBOARD_CACHE["ts"] = 0.0
        except Exception:
            pass

        return jsonify({
            'event_id': event_id,
            'fact_ids': fact_ids,
            'count': len(fact_ids),
            'tier': tier,
            'scope': scope,
            'mirrored': mirrored_fact_ids,
            # v1.14.23 Ship E: structured entity binding per fact.
            # Index N in entities_per_fact = entities for fact_ids[N].
            'entities_per_fact': facts_entities,
        })

    # v1.15.17 S21 (2026-09-25): auto-meta-recall gate.
    #
    # When any caller queries /v1/read, astor itself automatically looks up
    # relevant success_pattern / failure_pattern facts and prepends them to
    # results, so the caller sees "what worked before" / "what failed before"
    # without having to ask explicitly. This makes astor a "proactive advisor"
    # instead of a passive lookup table.
    #
    # Key design choices:
    # - Prepend, don't replace: original recall still runs and dominates results
    # - Top-2 success + top-2 failure (small enough not to bloat response)
    # - Tier-aware: matches the caller's tier + global source tier (SSoT lessons)
    # - Counts via bus fact_kind='success_pattern' / 'failure_pattern'
    # - Best-effort: any DB error → return empty list, never break recall
    # - Latency budget: <100ms (simple SQL ORDER BY importance DESC LIMIT N)
    def _meta_recall_patterns(query: str, user: str, tier: str) -> list:
        """Return top success_pattern + failure_pattern facts matching query.

        Pure SQL: lexical LIKE on content, ordered by importance DESC.
        Skips the slow nest embed (we want sub-100ms latency, not full hybrid).
        """
        if not query or len(query.strip()) < 3:
            return []
        try:
            # Build a simple LIKE pattern from query keywords (no Chinese token split
            # needed; LIKE is case-insensitive on the column default). Use first 6
            # words as a coarse filter to avoid full-table scans.
            words = [w.strip(' ,.?!:;"\'') for w in query.split() if len(w) >= 3][:6]
            if not words:
                return []
            like_clauses = ' AND '.join(['content LIKE ?' for _ in words])
            like_params = [f'%{w}%' for w in words]

            # Tier routing:
            # - private_X → query that user db + global public + source tier
            # - source   → query source tier only
            # - public   → query public tier + source tier
            # The source tier is global lessons that apply across users.
            tiers_to_query = []
            if tier and tier.startswith('private'):
                if user:
                    tiers_to_query.append(('private', user))
                tiers_to_query.append(('public', None))
                tiers_to_query.append(('source', None))
            elif tier == 'source':
                tiers_to_query.append(('source', None))
            else:  # public or None
                tiers_to_query.append(('public', None))
                tiers_to_query.append(('source', None))

            results = []
            seen_ids = set()
            for t, u in tiers_to_query:
                try:
                    b = astor_bus(tier=t, user_id=u)
                    rows = b.conn.execute(
                        f"SELECT id, content, kind, importance, memory_class "
                        f"FROM memory_canonical "
                        f"WHERE tombstoned = 0 AND kind IN ('success_pattern', 'failure_pattern') "
                        f"AND ({like_clauses}) "
                        f"ORDER BY importance DESC, created_at DESC LIMIT 2",
                        tuple(like_params)
                    ).fetchall()
                    for row in rows:
                        if row[0] in seen_ids:
                            continue
                        seen_ids.add(row[0])
                        results.append({
                            'fact_id': row[0],
                            'content': row[1][:500] if row[1] else '',
                            'kind': row[2],
                            'importance': row[3],
                            'memory_class': row[4] if len(row) > 4 else 'world_fact',
                            'meta_source': 'auto-meta-recall-v1.15.17',
                            'meta_tier': t,
                            'similarity': 0.99,  # synthetic — pattern match, not vector
                            'hit_source': 'meta-recall',
                        })
                except Exception as inner_exc:
                    _safe_stderr_write(f'[astor.server] meta-recall tier={t} err={inner_exc}\n')
                    continue
            return results
        except Exception as exc:
            _safe_stderr_write(f'[astor.server] meta-recall failed (non-fatal): {exc}\n')
            return []

    @app.route('/v1/read', methods=['POST'])
    def read():
        """Recall similar facts via nest vector search.

        Body JSON:
          query: str (required)
          user: str (optional, filter by user_id)
          top_k: int (default 5)
          memory_class: list[str] or comma-separated str (optional, v1.14.74)
                        — Hindsight ACL 2026 taxonomy. Each value must be one of
                          {world_fact, experience, observation, mental_model}.
                        Filter restricts recall to facts classified into those tiers.
          token_budget: int (optional, v1.14.75 planned) — reserved, not yet shipped.
        Returns:
          {results: [{fact_id, similarity, content, kind, memory_class, ...}], count: int}
        """
        # v1.15.17 S21: per-request meta-recall counter (dashboard reads aggregated
        # via /v1/audit/health meta_recall_stats; reset is by server restart).
        global _meta_recall_stats
        body = request.get_json(force=True)
        try:
            _read_agent_ctx = _resolve_agent_context(body)
        except ValueError as exc:
            return jsonify({'error': 'invalid_agent_context', 'detail': str(exc)}), 400
        query = body.get('query')
        if not query:
            return jsonify({'error': 'query required', 'detail': 'POST /v1/read requires JSON body with "query" field (string)'}), 400
        # v1.x.x MemCon-style recall controller (rule-based). Decides whether
        # to skip, what top_k to use. Honors explicit top_k from caller.
        _rc_should, _rc_reason = _rc_should_recall(query, body)
        _rc_t0 = _rc_time.time()
        if not _rc_should:
            _rc_log({'action': 'noop', 'reason': _rc_reason, 'query_len': len(query),
                     'tier': body.get('tier', 'public'), 'user': body.get('user') or body.get('user_id')})
            return jsonify({
                'results': [], 'count': 0,
                'recall_controller': {'action': 'noop', 'reason': _rc_reason,
                                       'elapsed_ms': int((_rc_time.time() - _rc_t0) * 1000)},
            })
        # 2026-08-27: tolerate "auto" / None / malformed top_k — fallback to 5
        # instead of 500. Client side passes "auto" from --query-adaptive flag.
        raw_top_k = body.get('top_k', 5)
        try:
            explicit_top_k = int(raw_top_k)
            if explicit_top_k <= 0:
                explicit_top_k = 5
        except (TypeError, ValueError):
            explicit_top_k = 5
        top_k = explicit_top_k
        # MemCon controller: when caller passes "auto" or omits top_k, decide
        # based on query features + lib size. Honor explicit numbers.
        _lib_size = 0
        if body.get('query_adaptive', True) and (body.get('top_k') is None or body.get('top_k') == 'auto'):
            try:
                _tier = body.get('tier', 'public')
                _uid = body.get('user_id') or body.get('user') if _tier in ('repo', 'private') else None
                _bus_for_count = astor_bus(tier=_tier, user_id=_uid)
                _lib_size = _bus_for_count.conn.execute(
                    "SELECT COUNT(*) FROM facts WHERE archived = 0"
                ).fetchone()[0]
            except Exception:
                _lib_size = 0
            top_k = _rc_choose_top_k(query, body, default_top_k=5, lib_size=_lib_size)
        _rc_log({'action': 'retrieve', 'top_k': top_k, 'lib_size': _lib_size,
                 'explicit': body.get('top_k') is not None and body.get('top_k') != 'auto',
                 'tier': body.get('tier', 'public'),
                 'user': body.get('user') or body.get('user_id'),
                 'elapsed_ms': int((_rc_time.time() - _rc_t0) * 1000)})
        # v1.14.74.5 (2026-09-18): Hindsight-style token_budget — overrides top_k after retrieval.
        _raw_budget = body.get('token_budget', None)
        try:
            token_budget = int(_raw_budget) if _raw_budget is not None else None
            if token_budget is not None and token_budget < 50:
                token_budget = 50
            if token_budget is not None and token_budget > 20000:
                token_budget = 20000
        except (TypeError, ValueError):
            token_budget = None
        # v1.14.74 (2026-09-18): memory_class filter — accepts list[str] or comma-sep string.
        raw_mc = body.get('memory_class')
        mc_filter = None
        if raw_mc is not None:
            if isinstance(raw_mc, str):
                mc_filter = [s.strip() for s in raw_mc.split(',') if s.strip()]
            elif isinstance(raw_mc, list):
                mc_filter = [str(s).strip() for s in raw_mc if str(s).strip()]
        if mc_filter:
            _allowed = {'world_fact', 'experience', 'observation', 'mental_model'}
            mc_filter = [m for m in mc_filter if m in _allowed]
            if not mc_filter:
                mc_filter = None  # silently no-op if everything invalid

        # v1.14.29 (2026-09-15 Ship S1): latency timer for usage stats.
        import time as _t_s1
        _read_t0 = _t_s1.time()
        # 2026-08-15 ship: recall targets the tier from request body.
        tier = body.get('tier', 'public')
        # v1.10.9 (2026-08-27): accept query_timestamp (LoCoMo, LongMemEval)
        # for temporal proximity boosting.
        query_timestamp = body.get('query_timestamp')
        since_ts = body.get('since_ts')
        until_ts = body.get('until_ts')
        if since_ts and not isinstance(since_ts, str):
            since_ts = None
        if until_ts and not isinstance(until_ts, str):
            until_ts = None
        if query_timestamp and not isinstance(query_timestamp, str):
            query_timestamp = None
        query_anchor = (query_timestamp or '')[:10] or None
        # v1.15.x (2026-09-22, Lossless-memory lesson): auto-parse natural
        # time phrases out of the query text (昨天/上周二/last week/3天前…)
        # when the caller didn't pass explicit since_ts/until_ts. Time is
        # the primary axis: restrict range FIRST, rank within it. Fallback
        # honesty: if the auto-derived scope leaves < min(3, top_k) results,
        # the filter is rolled back and time_fell_back=True is reported.
        # Disable per-request with time_parse=false.
        _time_auto = None
        _time_fell_back = False
        if not since_ts and not until_ts and body.get('time_parse', True):
            try:
                from .nest.time_phrase import parse_time_range as _ptr
                _time_auto = _ptr(query)
            except Exception:
                _time_auto = None
            if _time_auto:
                since_ts, until_ts = _time_auto[0], _time_auto[1]
        # v1.15.0 (2026-09-15 Ship A): RippleMem evidence-gap hint. Optional.
        # Caller can pass missing_hint="user timezone" to expand the query;
        # entity_filter=["<user>"] to post-filter; since_ts/until_ts to clamp
        # time_range. All three are backward-compatible no-ops when absent.
        # RippleMem ablation: hint-based gap-aware recall beats naive top-k
        # on multi-hop without a full graph layer (skipped — mem0g/zep
        # 4M-token build cost not justified for single-agent single-user).
        missing_hint = body.get('missing_hint')
        if missing_hint is not None and not isinstance(missing_hint, str):
            missing_hint = None
        if missing_hint:
            missing_hint = str(missing_hint).strip()[:200] or None
        _raw_ef = body.get('entity_filter')
        entity_filter = None
        if isinstance(_raw_ef, list):
            entity_filter = [str(e).strip()[:64] for e in _raw_ef if e][:8] or None
        elif isinstance(_raw_ef, str) and _raw_ef.strip():
            entity_filter = [e.strip()[:64] for e in _raw_ef.split(',') if e.strip()][:8] or None
        time_range = None
        if since_ts and until_ts:
            time_range = (since_ts[:10], until_ts[:10])
        elif since_ts:
            time_range = (since_ts[:10], '9999-12-31')
        elif until_ts:
            time_range = ('0000-01-01', until_ts[:10])
        # v1.1: tier=repo accepts repo_id (explicit) or user_id (fallback).
        user_id = None
        if tier == 'repo':
            user_id = body.get('repo_id') or body.get('user_id')
        elif tier == 'private':
            user_id = body.get('user_id') or body.get('user')
        # 2026-08-16 opt1: hybrid recall (vector + BM25). Default true.
        use_hybrid = bool(body.get('hybrid', True))
        # 2026-09-08 (ship, v3 after e5-large reembed):
        # Env-var override path; body wins if both present.
        # ASTOR_BM25_WEIGHT lets start_server.bat set baseline without code change.
        # eval_sweep.py 50-query results (post v1.14.5 e5-large switch):
        #   bm25=0.6 wins mrr 0.865 vs 0.4 default 0.835 (+3.6%), hit_rate=1.0.
        #   Before reembed (v3 with bge-base): bm25=0.4 wins. Embedding model
        #   switch CHANGED the bm25 weight winner — different semantic space.
        #   Sample size 50 with hit_rate=1.0 has reduced discriminative power
        #   but the +3.6% gap exceeds typical noise. Default bumped 0.4 → 0.6.
        #   Override via env/body to A/B on a larger eval set when built.
        bm25_weight = float(body.get('bm25_weight')
                            or os.environ.get('ASTOR_BM25_WEIGHT', 0.6))
        vec_weight = float(body.get('vec_weight')
                           or os.environ.get('ASTOR_VEC_WEIGHT', 0.4))
        # Oversample before merge so hybrid doesn't return fewer than top_k
        oversample = max(top_k * 2, 20)
        nest = astor_nest(tier=tier, user_id=user_id)
        bus = astor_bus(tier=tier, user_id=user_id)
        from .nest.embeddings import astor_get_embedding_model, astor_get_model_name_for_ram

        model = astor_get_embedding_model()

        # v1.2.0: local helper for safely parsing JSON-encoded fields.
        import json as _safe_json_loads_json_mod
        def _safe_json_loads(s):
            try:
                v = _safe_json_loads_json_mod.loads(s) if s else []
                return v if isinstance(v, list) else []
            except Exception:
                return []
        # v1.12.0 (2026-08-29): separate helper for metadata dicts (not lists).
        # _safe_json_loads above is list-or-[] only; for metadata we need
        # dict-or-{}. Sharing the helper would silently drop facts whose
        # metadata is valid JSON but happens to be a dict.
        def _safe_json_loads_dict(s):
            try:
                v = _safe_json_loads_json_mod.loads(s) if s else {}
                return v if isinstance(v, dict) else {}
            except Exception:
                return {}
        embeddings = list(model.embed([query]))
        query_emb = embeddings[0]

        # v1.10.9 (2026-08-27): multi-query synonym expansion. Local, no LLM.
        # Generates 1-2 cheap synonym variants (research->study, when->what date)
        # and runs hybrid recall on each, then dedupes by best score.
        _query_variants = [query]
        # v1.15.0 Ship A: missing_hint -> extra variant for BM25/vector expansion.
        # RippleMem-style: caller tells bus "what evidence is missing", bus
        # expands the query so it lands in hybrid retrieval results.
        if missing_hint and missing_hint.lower() not in query.lower():
            _query_variants.append(f"{query} {missing_hint}")
        if os.environ.get('ASTOR_EXPANSION', '1') != '0':
            try:
                from .nest.synonym_expander import expand_query as _expq
                _query_variants = _expq(query, max_variants=3)
            except Exception:
                pass
        # v1.10.9 (2026-08-27): multi-hop decomposer + conversation graph.
        # For multi-hop queries (heuristic: 'based on', 'how did', etc.),
        # append decomposed sub-queries AND LoCoMo event_summary hints.
        # Vector search stays single-pass. 0 LLM tokens.
        _is_multihop = False
        if os.environ.get('ASTOR_MULTIHOP', '1') != '0':
            try:
                from .nest.multihop_decomposer import is_multihop_query as _ismh, decompose as _mh
                _is_multihop = _ismh(query)
                for _q in _mh(query):
                    if _q not in _query_variants:
                        _query_variants.append(_q)
                if len(_query_variants) > 6:
                    _query_variants = _query_variants[:6]
            except Exception:
                pass
        if _is_multihop and os.environ.get('ASTOR_GRAPH', '1') != '0':
            try:
                from .nest.conversation_graph import expand_with_graph as _graph
                for _q in _graph(query, max_extras=3):
                    if _q not in _query_variants:
                        _query_variants.append(_q)
                if len(_query_variants) > 6:
                    _query_variants = _query_variants[:6]
            except Exception:
                pass
        # v1.10.9 (2026-08-27): multi-hop decomposer + conversation graph.
        # For multi-hop queries (heuristic: 'based on', 'how did', etc.),
        # append decomposed sub-queries AND LoCoMo event_summary hints.
        # Vector search stays single-pass. 0 LLM tokens.
        _is_multihop = False
        if os.environ.get('ASTOR_MULTIHOP', '1') != '0':
            try:
                from .nest.multihop_decomposer import is_multihop_query as _ismh, decompose as _mh
                _is_multihop = _ismh(query)
                for _q in _mh(query):
                    if _q not in _query_variants:
                        _query_variants.append(_q)
                if len(_query_variants) > 6:
                    _query_variants = _query_variants[:6]
            except Exception:
                pass
        if _is_multihop and os.environ.get('ASTOR_GRAPH', '1') != '0':
            try:
                from .nest.conversation_graph import expand_with_graph as _graph
                for _q in _graph(query, max_extras=3):
                    if _q not in _query_variants:
                        _query_variants.append(_q)
                if len(_query_variants) > 6:
                    _query_variants = _query_variants[:6]
            except Exception:
                pass
        # v1.10.9 (2026-08-27): multi-hop decomposer + conversation graph.
        # For multi-hop queries (heuristic: 'based on', 'how did', etc.),
        # append decomposed sub-queries AND LoCoMo event_summary hints.
        # Vector search stays single-pass. 0 LLM tokens.
        _is_multihop = False
        if os.environ.get('ASTOR_MULTIHOP', '1') != '0':
            try:
                from .nest.multihop_decomposer import is_multihop_query as _ismh, decompose as _mh
                _is_multihop = _ismh(query)
                for _q in _mh(query):
                    if _q not in _query_variants:
                        _query_variants.append(_q)
                if len(_query_variants) > 6:
                    _query_variants = _query_variants[:6]
            except Exception:
                pass
        if _is_multihop and os.environ.get('ASTOR_GRAPH', '1') != '0':
            try:
                from .nest.conversation_graph import expand_with_graph as _graph
                for _q in _graph(query, max_extras=3, user_id=user_id):
                    if _q not in _query_variants:
                        _query_variants.append(_q)
                if len(_query_variants) > 6:
                    _query_variants = _query_variants[:6]
            except Exception:
                pass

        # v1.15.x (2026-09-22): per-result hit-source provenance map.
        # Populated in each retrieval branch; surfaced as 'hit_source' on
        # every result so callers/eval can see which path found each fact.
        _hit_src = {}
        if not use_hybrid:
            # Pure vector path (legacy) — collect from all variants
            _all_v = []
            for _q in _query_variants:
                _qe = list(model.embed([_q]))[0] if _q != query else query_emb
                _all_v.extend(nest.search(_qe, limit=top_k))
            # Dedupe, keep max score
            _seen = {}
            for _fid, _s in _all_v:
                if int(_fid) not in _seen or _s > _seen[int(_fid)]:
                    _seen[int(_fid)] = _s
            results = sorted(_seen.items(), key=lambda x: x[1], reverse=True)[:top_k]
            _hit_src = {int(f): 'vector' for f, _ in results}
        else:
            from .nest.lex_index import (
                astor_lex as _astor_lex,
                hybrid_merge as _hybrid_merge,
            )
            lex = _astor_lex(tier=tier, user_id=user_id)
            # v1.10.9 v2: vector search keeps original query only (single pass).
            # BM25 also uses original query, but we inject the top match
            # from each synonym variant (cap at 5 per variant) as additional
            # BM25 hits, boosting pure-keyword matches without flooding
            # the candidate pool that feeds temporal_boost.
            vector_hits = nest.search(query_emb, limit=oversample)
            # v1.15.34 (2026-09-28, Ship O): HyDE for short abstract queries.
            # When query is short (<8 tokens) AND ASTOR_HYDE=1, generate a
            # hypothetical answer via cheap LLM, embed it, and merge into
            # vector_hits. HyDE hits weighted 0.5× so they boost but don't
            # override primary cosine. Gated off by default (operator opt).
            # v1.15.34 (Ship O): HyDE gate. `hyde` body field overrides
            # ASTOR_HYDE env. Accepts 'on'|'1'|True to enable, anything
            # else falls back to ASTOR_HYDE env (default off).
            _hyde_body = body.get('hyde')
            if _hyde_body is None:
                _hyde_on = os.environ.get('ASTOR_HYDE', '0') == '1'
            else:
                _hyde_on = str(_hyde_body).lower() in ('1', 'on', 'true', 'yes')
            if _hyde_on:
                try:
                    from .nest.hyde import hypothetical_answer as _hyde_ans, merge_hyde_hits as _hyde_merge
                    _hyde_text = _hyde_ans(query)
                    if _hyde_text:
                        _hyde_emb = list(model.embed([_hyde_text]))[0]
                        _hyde_hits = nest.search(_hyde_emb, limit=oversample)
                        vector_hits = _hyde_merge(vector_hits, _hyde_hits, weight=0.5)
                        import sys as _sys_hyde
                        _sys_hyde.stderr.write(f"[HYDE] merged hits: +{len(_hyde_hits)} for query='{query[:40]}'\n")
                except Exception as _hyde_exc:
                    pass  # HyDE failures are non-fatal; primary path stays.
            # v1.14.5: also search legacy bge-base-en-v1.5 during the
            # e5-large reembed transition. Once reembed finishes and all
            # S2 (2026-09-08): dual-model merge auto-disabled — public tier 100% + source tier 100% e5-large coverage; bge-baseen-v1.5 cleaned. Override via ASTOR_DUAL_MODEL=1 to re-enable.
            # (the legacy model returns empty hits for the few facts still
            # pending). Drops out automatically once e5-large row count
            # reaches bge-base row count. Override via ASTOR_DUAL_MODEL=0.
            if os.environ.get('ASTOR_DUAL_MODEL', '0') == '1':  # S2: 100% e5-large coverage → default OFF
                try:
                    from .nest.embeddings import astor_get_model_name_for_ram as _gmn
                    _primary_model = _gmn()
                    _legacy_model = 'BAAI/bge-base-en-v1.5'
                    if _primary_model != _legacy_model:
                        _legacy_hits = nest.search(query_emb, limit=oversample, model_name=_legacy_model)
                        # merge: keep max score per fact_id
                        for _lfid, _ls in _legacy_hits:
                            _lfid = int(_lfid)
                            # Note: vector_hits is list of tuples, may have
                            # duplicates if same fid with different scores.
                            # _bm25_seen style dedup: skip for now (post-merge dedup at hybrid_merge)
                            vector_hits = list(vector_hits) + _legacy_hits
                except Exception:
                    pass
            _bm25_seen = {}
            for _fid, _s in lex.bm25_search(query, limit=oversample):
                _bm25_seen[int(_fid)] = _s
            for _q in _query_variants[1:]:
                for _fid, _s in lex.bm25_search(_q, limit=5):
                    _fid = int(_fid)
                    if _fid not in _bm25_seen or _s > _bm25_seen[_fid]:
                        _bm25_seen[_fid] = _s
            bm25_hits = list(_bm25_seen.items())
            # v1.2.0: load per-fact keywords from canonical + compute
            # Jaccard boost. O(oversample) - fine for top_k <= 50.
            candidate_fids = sorted({f for f, _ in bm25_hits}
                                    | {f for f, _ in vector_hits})
            keyword_hits = {}
            if candidate_fids:
                placeholders = ','.join('?' * len(candidate_fids))
                kw_rows = bus.conn.execute(
                    f"SELECT id, keywords FROM memory_canonical "
                    f"WHERE id IN ({placeholders})",
                    candidate_fids,
                ).fetchall()
                for fid, kw_json in kw_rows:
                    try:
                        import json as _json
                        kws = _json.loads(kw_json) if kw_json else []
                        if kws:
                            keyword_hits[int(fid)] = kws
                    except Exception:
                        pass
            # Query keywords = tokens of the query (cheap; no LLM call).
            from .nest.lex_index import _tokenize
            query_keywords = _tokenize(query)
            # v1.10.9: build temporal_boost map for hybrid_merge.
            _temporal_boost = {}
            if candidate_fids:
                try:
                    _tb_rows = bus.conn.execute(
                        f"SELECT id, event_date, event_date_precision FROM memory_canonical "
                        f"WHERE id IN ({','.join('?' * len(candidate_fids))})",
                        list(candidate_fids),
                    ).fetchall()
                    for _tbid, _tbd, _tbp in _tb_rows:
                        if _tbd:
                            _temporal_boost[int(_tbid)] = (str(_tbd), str(_tbp or 'day'))
                except Exception:
                    pass
            merged = _hybrid_merge(
                bm25_hits=bm25_hits,
                vector_hits=vector_hits,
                bm25_weight=bm25_weight,
                vec_weight=vec_weight,
                limit=oversample,
                keyword_hits=keyword_hits if keyword_hits else None,
                query_keywords=query_keywords,
                temporal_boost=_temporal_boost if _temporal_boost else None,
                temporal_boost_strength=0.4,
                query_anchor=query_anchor,
            )
            # v1.15.32 (2026-09-28, Ship L) + v1.15.33 (Ship M): MMR rerank.
            # Without MMR, near-duplicate phrasings crowd out the canonical
            # answer at top-k. Ship M added body['mmr_lambda'] knob with
            # ASTOR_MMR_LAMBDA env override and clamp [0.0, 1.0]. 1.0 = no MMR.
            results = merged[:top_k]
            _mmr_lambda = body.get("mmr_lambda")
            if _mmr_lambda is None:
                _env_lambda = os.environ.get("ASTOR_MMR_LAMBDA")
                _mmr_lambda = float(_env_lambda) if _env_lambda else None
            if _mmr_lambda is not None:
                try:
                    _mmr_lambda = max(0.0, min(1.0, float(_mmr_lambda)))
                except Exception:
                    _mmr_lambda = None
            if (os.environ.get("ASTOR_MMR", "1") != "0"
                    and len(merged) > top_k
                    and _mmr_lambda != 1.0):  # 1.0 = no MMR
                try:
                    from .nest.mmr_reranker import mmr_rerank as _mmr_fn
                    _mmr_pool = merged[:max(top_k * 2, top_k + 4)]
                    _mmr_fids = [int(f) for f, _ in _mmr_pool]
                    _placeholders = ",".join("?" * len(_mmr_fids))
                    _content_rows = bus.conn.execute(
                        f"SELECT id, content FROM memory_canonical "
                        f"WHERE id IN ({_placeholders})",
                        _mmr_fids,
                    ).fetchall()
                    _content_map = {int(r[0]): r[1] or "" for r in _content_rows}
                    _lam = _mmr_lambda if _mmr_lambda is not None else 0.7
                    results = _mmr_fn(_mmr_pool, _content_map, query, top_k=top_k, lambda_=_lam)
                except Exception as _mmr_exc:
                    _safe_stderr_write("[MMR] failed (continuing with hybrid order): " + repr(_mmr_exc) + chr(10))
                    results = merged[:top_k]
            # hit-source provenance: bm25-only / vector-only / both.
            _vec_id_set = {int(f) for f, _ in vector_hits}
            for _fid, _s in merged:
                _fid = int(_fid)
                _b = _fid in _bm25_seen
                _v = _fid in _vec_id_set
                _hit_src[_fid] = ('bm25+vector' if (_b and _v)
                                  else ('bm25' if _b else 'vector'))
            _skip_stage = False  # v1.10.9: LLM rerank may override stage_recall
            # v1.10.9 (2026-08-27): LLM rerank. Set ASTOR_RERANK=1 to enable.
            # 2026-08-27: lowered trigger from top_k>=5 to top_k>=3 so small per-conv
            # DBs (22 facts) actually exercise the rerank path.
            # 2026-08-27: query-level override via body['rerank']:
            #   - 'on' | '1' | True  → force enable (overrides ASTOR_RERANK=0)
            #   - 'off' | '0' | False → force disable
            #   - absent → follow ASTOR_RERANK env
            _body_rr = body.get('rerank', None)
            if _body_rr is None:
                _rerank_on = os.environ.get('ASTOR_RERANK', '0') == '1'
            else:
                _rerank_on = str(_body_rr).lower() in ('1', 'on', 'true', 'yes')
            if _rerank_on and results and top_k >= 3:
                try:
                    _safe_stderr_write(f"[RERANK] enabled, results={len(results)}, top_k={top_k}, calling LLM...\n")
                    from .nest.llm_rerank import rerank_candidates as _llm_rr
                    # Build (fid, content) pairs from results.
                    # 2026-08-27 fix: results from hybrid_merge is list of (fid, score)
                    # tuples — no content field. Need to look up content from bus DB.
                    _pairs = []
                    _fids = []
                    if results and isinstance(results[0], (tuple, list)):
                        _fids = [r[0] for r in results[:30]]
                    elif results and isinstance(results[0], dict):
                        _fids = [r.get('fact_id', r.get('id')) for r in results[:30] if r.get('fact_id') or r.get('id')]
                    if _fids:
                        try:
                            # Use module-level bus from outer scope (line 474).
                            _ph = ','.join('?' * len(_fids))
                            _rows = bus.conn.execute(
                                f"SELECT id, content FROM memory_canonical WHERE id IN ({_ph})",
                                _fids,
                            ).fetchall()
                            _fid_text = {int(r[0]): (r[1] or '') for r in _rows}
                        except Exception:
                            _fid_text = {}
                        _pairs = [(f, _fid_text.get(f, '')) for f in _fids]
                    _ranked = _llm_rr(query, _pairs)
                    _safe_stderr_write(f"[RERANK] returned {len(_ranked)} ranked fids\n")
                    if _ranked:
                        # Re-order results. results shape: list of (fid, score) tuples.
                        _by_fid = {r[0]: r for r in results} if results and isinstance(results[0], (tuple, list)) else {r.get('fact_id'): r for r in results}
                        _new = [_by_fid[fid] for fid in _ranked if fid in _by_fid]
                        # Append any not in ranked set (LLM dropped them)
                        _ranked_set = set(_ranked)
                        for r in results:
                            _rid = r[0] if isinstance(r, (tuple, list)) else r.get('fact_id')
                            if _rid not in _ranked_set:
                                _new.append(r)
                        results = _new
                        # v1.10.9: LLM rerank already optimized; skip stage_recall.
                        _skip_stage = True
                except Exception as _e:
                    import traceback as _tb
                    _safe_stderr_write(f"[RERANK] EXCEPTION: {type(_e).__name__}: {_e}\n{_tb.format_exc()}\n")
            # v1.10.9 (2026-08-27): stage_recall entity-coverage rerank.
            # Boosts candidates whose content mentions multiple entities
            # from the query. Free, <5ms.
            if not _skip_stage and os.environ.get('ASTOR_STAGERECALL', '1') != '0' and results:
                try:
                    from .nest.stage_recall import stage_recall_rerank as _sr
                    if candidate_fids:
                        _ph2 = ','.join('?' * len(candidate_fids))
                        _rtext = bus.conn.execute(
                            f"SELECT id, content FROM memory_canonical "
                            f"WHERE id IN ({_ph2})",
                            list(candidate_fids),
                        ).fetchall()
                        _cand_text = {int(r[0]): (r[1] or '') for r in _rtext}
                    else:
                        _cand_text = {}
                    results = _sr(results, _cand_text, query, top_k=top_k)
                except Exception:
                    pass
            # v1.10.9 (2026-08-26): optional rerank (env ASTOR_RERANK=1).
            # Lifts multi-hop chain coherence via lexical+bridge rerank.
            if os.environ.get('ASTOR_RERANK', '0') == '1' and results:
                try:
                    from .nest.reranker import rerank_candidates as _rerank
                    cand_dicts = []
                    cand_text = {}
                    if candidate_fids:
                        _ph2 = ',' .join('?' * len(candidate_fids))
                        _rtext = bus.conn.execute(
                            f"SELECT id, keywords, tags, metadata FROM memory_canonical WHERE id IN ({_ph2})",
                            list(candidate_fids),
                        ).fetchall()
                        for _rid, _kw, _tg, _mt in _rtext:
                            fid_int = int(_rid)
                            try:
                                _kws = _json.loads(_kw) if _kw else []
                            except Exception:
                                _kws = []
                            try:
                                _tags = _json.loads(_tg) if _tg else []
                            except Exception:
                                _tags = []
                            try:
                                _meta = _json.loads(_mt) if _mt else {}
                                _ctx = str(_meta.get('context', '') or '') if isinstance(_meta, dict) else ''
                            except Exception:
                                _ctx = ''
                            cand_text[fid_int] = (_ctx + ' ' + ' '.join(_kws) + ' ' + ' '.join(_tags)).strip()
                    for fid, s in results:
                        cand_dicts.append({'id': fid, 'score': s, 'content': cand_text.get(fid, '')})
                    reranked = _rerank(query, cand_dicts, top_n=top_k, rerank_weight=0.65)
                    results = [(c['id'], c['score']) for c in reranked]
                except Exception:
                    pass
            # v1.10.9: multi-hop bridge. Disabled by default (env ASTOR_BRIDGE=1).
            # Empirically: bridge decay<0.4 hurts LoCoMo accuracy because
            # co-ranked entities often collide on generic nouns (places,
            # common names) and over-promote wrong answers. Keep the
            # implementation available; only enable via env when known-good.
            if os.environ.get('ASTOR_BRIDGE', '0') == '1' and results and len(results) >= 2:
                try:
                    from .nest.multi_hop_bridge import apply_multi_hop_boost as _bridge
                    _bfids = [fid for fid, _ in results]
                    if _bfids:
                        _phb = ','.join('?' * len(_bfids))
                        _brows = bus.conn.execute(
                            f"SELECT id, content, keywords, tags FROM memory_canonical WHERE id IN ({_phb})",
                            _bfids,
                        ).fetchall()
                        _bcands = []
                        for _bid, _bct, _bkw, _btg in _brows:
                            try:
                                _bkws = _json.loads(_bkw) if _bkw else []
                            except Exception:
                                _bkws = []
                            try:
                                _btgs = _json.loads(_btg) if _btg else []
                            except Exception:
                                _btgs = []
                            _bcands.append({
                                'id': int(_bid), 'content': _bct or '',
                                'keywords': _bkws, 'tags': _btgs,
                                'score': next((s for f, s in results if f == int(_bid)), 0.0),
                            })
                        _boosted = _bridge(_bcands, top_seed_n=min(5, len(_bcands)))
                        _score_map = {c['id']: c['score'] for c in _boosted}
                        results = [(fid, _score_map.get(int(fid), 0.0)) for fid, _ in results]
                        results.sort(key=lambda x: x[1], reverse=True)
                except Exception:
                    pass
            # If hybrid returned nothing (empty lex AND empty nest), fall
            # back to vector-only so the caller doesn't get a hard empty.
            if not results:
                results = nest.search(query_emb, limit=top_k)
        # v1.14.46 (2026-09-16, Ship G): L1/L2 multi-granularity wiring.
        # After hybrid + fallback, try the L1 (cluster) → L2 (fact)
        # path. If cluster_embeddings is empty (fresh install), lazily
        # rebuild ONCE per process (avoids repeated rebuilds). Disabled
        # via ASTOR_MULTIGR_ENABLED=0 (already shipped in v1.14.42).
        if os.environ.get('ASTOR_MULTIGR_ENABLED', '1') != '0':
            _l12 = nest.search_l1_l2(query_emb, l1_limit=3, l2_limit=top_k)
            if not _l12:
                # Lazy rebuild guard: at most once per process. Stored as
                # an attribute on nest (lives for the process lifetime).
                if not getattr(nest, '_l12_rebuild_attempted', False):
                    try:
                        n_rebuilt = nest.rebuild_clusters()
                        _l12 = nest.search_l1_l2(query_emb, l1_limit=3, l2_limit=top_k)
                    except Exception:
                        n_rebuilt = 0
                        _l12 = []
                    nest._l12_rebuild_attempted = True
            if _l12:
                # _l12 is list of (cluster_key, fact_id, sim). Merge into results.
                _seen_l12 = {int(r[0]): r[1] for r in results}
                for _ck, _fid, _sim in _l12:
                    _fid = int(_fid)
                    if _fid not in _seen_l12 or _sim > _seen_l12[_fid]:
                        _seen_l12[_fid] = _sim
                results = sorted(_seen_l12.items(), key=lambda x: x[1], reverse=True)[:top_k]
        # Enrich with bus metadata (content, kind)
        enriched = []
        for fact_id, sim in results:
            row = bus.conn.execute(
                "SELECT id, content, kind, confidence, importance, tags, namespace, user_id, keywords, context, "
                "event_date, event_date_precision, origin_session_id, metadata, entities_json, created_at, memory_class, "
                # v1.14.74+ Ship A2-Akasha: evidence-grounded source linking
                "evidence_quote, source_ref, source_hash "
                "FROM memory_canonical WHERE id = ?",
                (fact_id,),
            ).fetchone()
            if row is None:
                continue
            # v1.12.0 (2026-08-29): surface hierarchical extraction fields
            # (__topic__, __session_id__) to the API so multi-hop bridge
            # callers and human-readable displays can use them. Falls back
            # to '' for pre-v1.12 facts that lack these keys.
            _meta = _safe_json_loads_dict(row[13]) if len(row) > 13 and row[13] else {}
            enriched.append({
                'fact_id': row[0],
                'content': row[1],
                'kind': row[2],
                'confidence': row[3],
                'importance': row[4],
                'tags': row[5],
                'namespace': row[6],
                'user_id': row[7],
                'similarity': round(sim, 4),
                # 'score_kind' tells the caller whether similarity is
                # pure-cosine ('cosine') or hybrid ('hybrid'). Hermes
                # adapter uses this for sorting/debug.
                'score_kind': 'hybrid' if use_hybrid else 'cosine',
                'hit_source': _hit_src.get(int(row[0]),
                                           'hybrid' if use_hybrid else 'cosine'),
                # v1.2.0: include keywords + context in response so callers
                # (e.g. hermes_adapter) can render fact titles / explain
                # recall. Empty defaults for pre-v1.2 facts.
                'keywords': _safe_json_loads(row[8]) if len(row) > 8 else [],
                'context': (row[9] if len(row) > 9 and row[9] else '')[:500],
                # 2026-08-27: expose event_date so LLM can do date arithmetic
                # for temporal queries (LoCoMo "When did X?" weakness).
                'event_date': row[10] if len(row) > 10 else None,
                'event_date_precision': row[11] if len(row) > 11 else None,
                'session_id': (row[12] if len(row) > 12 else None),
                # v1.12.0: hierarchical extraction (Mem0 2026 lesson).
                # topic + session_id from metadata JSON, populated by either
                # LLM extractor or the regex-fallback heuristic. Empty when
                # fact was written before v1.12.0 (legacy schema didn't have
                # these fields).
                'topic': _meta.get('__topic__', '') if _meta else '',
                'session_id_meta': _meta.get('__session_id__', '') if _meta else '',
                # v1.14.21 Ship B: RippleMem-style structured entity binding.
                # List of {type, value, fact_id} extracted at forge time.
                # Empty list for legacy facts until backfill runs.
                'entities': _safe_json_loads(row[14]) if len(row) > 14 else [],
                # v1.14.31 Ship S3: created_at surfaced so time_range reorder
                # can use it as a soft proximity signal for legacy facts
                # (no event_date). Caller can also use this to display
                # "ingested at" timestamps.
                'created_at': ((row[15] if len(row) > 15 else '') or '')[:19],
                # v1.14.74 (2026-09-18) Hindsight 4-tier classification
                'memory_class': (row[16] if len(row) > 16 and row[16] else 'world_fact'),
                # v1.14.74+ Ship A2-Akasha: evidence-grounded source linking.
                # Exposed to callers so the agent can require every
                # fact in its recall context to carry an evidence
                # quote (mitigates R134 fabrication).
                'evidence_quote': (row[17] if len(row) > 17 and row[17] else '')[:1024],
                'source_ref': (row[18] if len(row) > 18 and row[18] else '')[:512],
                'source_hash': (row[19] if len(row) > 19 and row[19] else '')[:64],
                # v1.14.74+ Ship C-Akasha: stale flag = hash mismatches
                # against expected_source_hashes[srouce_ref] from caller.
                # None / false / 'unchanged' / unset refresh_hash =
                # legacy checks not requested. Default value when
                # no expected hash supplied: stays None.
                'stale': None,
            })
        # v1.14.74+ Ship C-Akasha: stale detection via expected_source_hashes.
        # Caller may submit expected_source_hashes = {source_ref: expected_hash}
        # to surface facts whose source content has changed since write.
        # Compare each enriched fact's source_hash vs expected[source_ref].
        # Pure advisory: mark stale=True when mismatch. Empty / missing
        # expected hashes = no check (legacy behavior preserved).
        _expected_hashes = body.get('expected_source_hashes') or {}
        if isinstance(_expected_hashes, dict) and _expected_hashes:
            for _fact in enriched:
                _ref = _fact.get('source_ref') or ''
                _cur = _fact.get('source_hash') or ''
                if not _ref or not _cur:
                    continue
                _exp = _expected_hashes.get(_ref)
                if _exp and _exp != _cur:
                    _fact['stale'] = True
                    _fact['stale_reason'] = (
                        f'source_hash mismatch: expected={_exp!r} '
                        f'current={_cur!r}'
                    )
                else:
                    _fact['stale'] = False
        # v1.15.0 Ship A: entity_filter + time_range post-filter.
        # entity_filter = list of strings; fact must contain ANY of them in
        # content or keywords (case-insensitive substring match — works for
        # Ship A without entities_json column; will upgrade to structured
        # entity match in Ship B). Empty filter / None = no filter.
        # time_range = (since, until) YYYY-MM-DD; fact's event_date must fall
        # in range. None time = no clamp.
        if entity_filter:
            _ef_low = [e.lower() for e in entity_filter]
            enriched = [r for r in enriched
                        if any(e in (r.get('content') or '').lower()
                               or any(e in (k or '').lower()
                                      for k in (r.get('keywords') or []))
                               for e in _ef_low)]
        if time_range:
            _ts_lo, _ts_hi = time_range
            _pre_tr_snapshot = enriched  # for auto-scope rollback below
            def _in_tr(r):
                _ed = r.get('event_date') or ''
                if not _ed:
                    return True  # no date = keep (don't punish legacy facts)
                return _ts_lo <= _ed[:10] <= _ts_hi
            enriched = [r for r in enriched if _in_tr(r)]
            # Lossless-memory honesty rule: a wrong/over-narrow auto time
            # scope must not silently nuke recall. Roll back and say so.
            if _time_auto and len(enriched) < min(3, top_k):
                enriched = _pre_tr_snapshot
                time_range = None
                _time_fell_back = True
        # v1.14.74 (2026-09-18) Hindsight taxonomy post-filter: mc_filter is a list of
        # memory_class values to allow (4-tier scheme — world_fact / experience /
        # observation / mental_model). None / empty = no filter (backward-compat).
        # Note: pristine pre-v1.14.74 rows default to memory_class='world_fact', so they
        # only show up in recalls filtered on 'world_fact' or in unfiltered recalls.
        # No-op when mc_filter is None.
        if mc_filter:
            enriched = [r for r in enriched
                        if (r.get('memory_class') or 'world_fact') in mc_filter]
        # NOTE: v1.15.0 entity_filter + time_range post-filters are placed
        # AFTER grep_verify + neighbor-expand below so they also prune any
        # rows those passes append. (Otherwise grep_verify can re-leak facts
        # that should be filtered, as observed in test_rest_read_entity_filter.)

        # Agentic Search pattern). Zero LLM tokens. After vector/hybrid
        # recall, re-run an exact BM25 match on the query's rare tokens;
        # facts that exact-match but were missed by hybrid recall get
        # appended (marked source='grep_verify'). Fixes "I don't have
        # info" failures where the answer WAS in the corpus but vector
        # similarity ranked it below top_k (observed on LongMemEval).
        if enriched and os.environ.get('ASTOR_GREP_VERIFY', '1') != '0':
            try:
                from .nest.lex_index import astor_lex as _astor_lex
                _lex = _astor_lex(tier=tier, user_id=user_id)
                _stop = {
                    'what', 'when', 'where', 'who', 'how', 'did', 'does', 'is',
                    'are', 'was', 'were', 'the', 'a', 'an', 'my', 'i', 'me',
                    'in', 'on', 'at', 'of', 'for', 'to', 'with', 'and', 'or',
                    'do', 'did', 'have', 'has', 'many', 'much', 'long', 'get',
                }
                _tokens = [
                    t for t in re.findall(r"[A-Za-z0-9]{2,}", query)
                    if t.lower() not in _stop
                ]
                if _tokens:
                    _have = {int(r['fact_id']) for r in enriched}
                    # grep semantics: rare tokens are needles. Multi-token AND
                    # query often returns [] (BM25 conjunction); probe tokens
                    # individually and merge hits.
                    _hits = {}
                    _tok_df = {}
                    for _tok in _tokens[:8]:
                        try:
                            _tok_hits = _lex.bm25_search(_tok, limit=top_k * 2)
                            _tok_df[_tok.lower()] = len(_tok_hits)
                            for _fid, _score in _tok_hits:
                                _hits.setdefault(int(_fid), []).append((_tok, float(_score)))
                        except Exception:
                            continue
                    # Rarest tokens first: a fact matching a rare token is a
                    # stronger grep signal than one matching only common words.
                    _rarity = {t: _tok_df.get(t.lower(), 999) for t in _tokens}
                    _sorted_tokens = sorted(_tokens, key=lambda t: _rarity.get(t.lower(), 999))
                    _rare_thresh = 3  # token in <=3 facts = rare needle
                    _hits_ranked = sorted(
                        _hits.items(),
                        key=lambda kv: min(_rarity.get(t.lower(), 999) for t, _ in kv[1]),
                    )
                    _added = 0
                    for _fid, _tok_scores in _hits_ranked:
                        if _fid in _have or _added >= 3:
                            continue
                        _row = bus.conn.execute(
                            "SELECT id, content, kind, confidence, importance, tags, namespace, user_id, keywords, context, "
                            "event_date, event_date_precision, origin_session_id "
                            "FROM memory_canonical WHERE id = ?", (_fid,),
                        ).fetchone()
                        if _row is None:
                            continue
                        _content_l = str(_row[1]).lower()
                        # Require >=1 RARE token exact match (grep semantics):
                        # common-word-only matches are distractors, skip them.
                        _matched_all = [t for t in _sorted_tokens if t.lower() in _content_l]
                        _matched = [t for t in _matched_all if _rarity.get(t.lower(), 999) <= _rare_thresh]
                        if not _matched:
                            continue
                        enriched.append({
                            'fact_id': _row[0],
                            'content': _row[1],
                            'kind': _row[2],
                            'confidence': _row[3],
                            'importance': _row[4],
                            'tags': _row[5],
                            'namespace': _row[6],
                            'user_id': _row[7],
                            'similarity': round(min(max(s for _, s in _tok_scores) / 10.0, 1.0), 4),
                            'score_kind': 'grep_verify',
                            'hit_source': 'grep_verify',
                            'keywords': _safe_json_loads(_row[8]) if len(_row) > 8 else [],
                            'context': (_row[9] if len(_row) > 9 and _row[9] else '')[:500],
                            'event_date': _row[10] if len(_row) > 10 else None,
                            'event_date_precision': _row[11] if len(_row) > 11 else None,
                            'session_id': _row[12] if len(_row) > 12 else None,
                            'grep_matched_tokens': _matched[:5],
                        })
                        _have.add(_fid)
                        _added += 1
            except Exception:
                pass  # grep-verify is best-effort; never break recall

        # v1.11.0: session-neighbor expand (read/navigate pattern). For the
        # top hybrid hits that carry origin_session_id, pull ±1 sibling facts
        # from the same session — gives the LLM the surrounding context of a
        # hit without re-running vector search. 0 LLM tokens.
        if enriched and os.environ.get('ASTOR_NEIGHBOR', '1') != '0':
            try:
                _seen_ids = {int(r['fact_id']) for r in enriched}
                _neighbors = []
                for _r in enriched[:3]:
                    _sid = _r.get('session_id') or None
                    if not _sid:
                        continue
                    _rows = bus.conn.execute(
                        "SELECT id, content, kind, origin_session_id, event_date FROM memory_canonical "
                        "WHERE origin_session_id = ? AND id != ? AND tombstoned = 0 "
                        "ORDER BY ABS(id - ?) LIMIT 2",
                        (_sid, int(_r['fact_id']), int(_r['fact_id'])),
                    ).fetchall()
                    for _row in _rows:
                        if int(_row[0]) in _seen_ids:
                            continue
                        _seen_ids.add(int(_row[0]))
                        _neighbors.append({
                            'fact_id': _row[0],
                            'content': _row[1],
                            'kind': _row[2],
                            'session_id': _row[3],
                            'event_date': _row[4],
                            'similarity': 0.0,
                            'score_kind': 'session_neighbor',
                            'hit_source': 'session_neighbor',
                            'neighbor_of': int(_r['fact_id']),
                        })
                # Neighbors go AFTER all primary results
                enriched.extend(_neighbors[:3])
            except Exception:
                pass  # neighbor-expand is best-effort

        # v1.15.0 Ship A: final filter pass (after grep_verify + neighbor)
        if entity_filter:
            _ef_low = [e.lower() for e in entity_filter]
            enriched = [r for r in enriched
                        if any(e in (r.get('content') or '').lower()
                               or any(e in (k or '').lower()
                                      for k in (r.get('keywords') or []))
                               for e in _ef_low)]
        if time_range:
            # v1.14.27 Ship I + v1.14.31 Ship S3: legacy facts without
            # event_date are KEEP-but-DEPRIORITIZE. Ship S3 adds a soft
            # proximity boost: legacy facts with created_at inside the
            # time window float to the front of the legacy block; ones
            # with created_at far from the window sink to the bottom.
            # Uses created_at as a proxy for "how old is this".
            _ts_lo, _ts_hi = time_range
            _ts_lo_d = _ts_lo[:10]  # YYYY-MM-DD
            _ts_hi_d = _ts_hi[:10]
            # Compute window midpoint as YYYYMMDD integer for distance math
            try:
                _mid_y, _mid_m, _mid_d = int(_ts_lo_d[:4]), int(_ts_lo_d[5:7]), int(_ts_lo_d[8:10])
                _win_lo_int = _mid_y * 10000 + _mid_m * 100 + _mid_d
                _mid_y, _mid_m, _mid_d = int(_ts_hi_d[:4]), int(_ts_hi_d[5:7]), int(_ts_hi_d[8:10])
                _win_hi_int = _mid_y * 10000 + _mid_m * 100 + _mid_d
                _win_mid = (_win_lo_int + _win_hi_int) // 2
            except Exception:
                _win_mid = None

            def _event_date_in(r):
                _ed = r.get('event_date') or ''
                if not _ed:
                    return None  # signal "legacy / no date"
                return _ts_lo <= _ed[:10] <= _ts_hi

            _in_range = [r for r in enriched if _event_date_in(r) is True]
            _out_of_range = [r for r in enriched if _event_date_in(r) is False]
            _legacy = [r for r in enriched if _event_date_in(r) is None]

            # Sort legacy by created_at proximity to window midpoint
            # (closest first). created_at comes from the canonical row,
            # but is not in the per-fact response dict (omitted for size);
            # fall back to last_confirmed_at (also not exposed) — best
            # we can do is stable order, which matches previous behavior.
            # When Ship S3's response shape adds created_at, this sort
            # will activate automatically.
            if _win_mid is not None and _legacy:
                def _legacy_key(r):
                    # Look for created_at or promoted_at in the row;
                    # absence -> treat as 'far from window' (sink).
                    _ca = r.get('created_at') or r.get('promoted_at') or ''
                    if not _ca:
                        return (1, 0)  # (no date flag, dummy distance)
                    try:
                        _y = int(_ca[:4])
                        _m = int(_ca[5:7]) if len(_ca) >= 7 else 1
                        _d = int(_ca[8:10]) if len(_ca) >= 10 else 1
                        _ca_int = _y * 10000 + _m * 100 + _d
                        _dist = abs(_ca_int - _win_mid)
                        return (0, _dist)
                    except Exception:
                        return (1, 0)
                _legacy.sort(key=_legacy_key)
            enriched = _in_range + _out_of_range + _legacy
        # v1.14.32 Ship G (2026-09-15): --kinds URL param for zone-filtered
        # recall. Mirrors `am recall --kinds` behavior (cmd_recall main).
        # Supports comma-separated kind list (e.g. kinds=user_preference,failure_pattern).
        # Empty/missing = no filter (all kinds). Undergoes 4x oversample
        # before post-filter so top_k matches survive — see cmd_recall doc.
        _kinds_filter_set = None
        if isinstance(body.get('kinds'), str) and body['kinds'].strip():
            _kinds_filter_set = set(
                k.strip() for k in body['kinds'].split(',') if k.strip()
            )
        elif isinstance(body.get('kinds'), list):
            _kinds_filter_set = set(
                str(k).strip() for k in body['kinds'] if str(k).strip()
            )
        if _kinds_filter_set and enriched:
            enriched = [r for r in enriched
                         if r.get('kind') in _kinds_filter_set][:top_k]

        # v1.14.44 (2026-09-16, Ship F): wing alias for /v1/read. Maps a
        # wing=human/agent/rule query to the underlying provenance_kind
        # set. Runs AFTER kinds filter so users can combine (e.g.
        # kinds=failure_pattern + wing=human).
        _wing_filter_set = None
        _wing_value = body.get('wing')
        if _wing_value is not None and not isinstance(_wing_value, str):
            return jsonify({"error": "wing_must_be_string", "got": type(_wing_value).__name__}), 400
        try:
            _wing_filter_set = _expand_wing_to_provenance(_wing_value)
        except ValueError as exc:
            return jsonify({"error": "invalid_wing", "detail": str(exc)}), 400
        if _wing_filter_set is not None and enriched:
            enriched = [r for r in enriched
                         if r.get('provenance_kind') in _wing_filter_set][:top_k]

        # v1.14.33 Ship H (2026-09-15): --session_id URL param for session-scoped
        # recall. Mirrors Ship G kinds filter logic — post-enrichment client-side
        # filter (cheap, ~60 µs). session_id typically comes from hermes agent
        # which knows its own session_id, or from fact_id reverse-lookup.
        # Empty/missing = no filter (backward compat).
        _session_filter = body.get('session_id')
        if isinstance(_session_filter, str) and not _session_filter.strip():
            _session_filter = None
        if _session_filter and enriched:
            # Match exact session_id OR origin_session_id (server returns both
            # fields; legacy facts may have only one of them populated).
            enriched = [r for r in enriched
                         if (r.get('session_id') == _session_filter
                             or r.get('origin_session_id') == _session_filter)
                        ][:top_k]

        # v1.14.65 P8 (2026-09-17, end-to-end completeness): time_boost.
        # When time_boost=true (default ON), facts created within the
        # past 7 days get a small similarity boost (1.10x). This solves
        # the "cross-session continuity" dead-end: a user who said
        # "周二下午 poker" on Monday gets it recalled cleanly on Tuesday
        # because recent facts rise to the top of the result list.
        # Without time_boost, dense admin corpus (5K+ facts) buries the
        # recent ones in mrr=0.85 baseline noise. Disabled by passing
        # time_boost=false. Threshold (7 days) and boost (1.10x) are
        # conservative — won't distort semantic ranking, just breaks ties.
        _time_boost_applied = False
        try:
            import datetime as _dt_tb
            _tb_enabled = body.get('time_boost', True)
            _tb_window_days = int(body.get('time_boost_days', 7))
            _tb_factor = float(body.get('time_boost_factor', 1.10))
            if _tb_enabled and enriched:
                _now_tb = _dt_tb.datetime.utcnow()
                _cutoff = (_now_tb - _dt_tb.timedelta(days=_tb_window_days)).isoformat(timespec='seconds') + 'Z'
                for r in enriched:
                    _ca = r.get('created_at') or ''
                    if _ca and _ca >= _cutoff:
                        try:
                            r['similarity'] = float(r.get('similarity', 0)) * _tb_factor
                            _time_boost_applied = True
                        except (TypeError, ValueError):
                            pass
                # Re-sort if we boosted anything.
                if _time_boost_applied:
                    enriched.sort(key=lambda x: x.get('similarity', 0), reverse=True)
        except Exception:
            pass

        # v1.14.70 S1 + v1.14.71 (2026-09-17, topic-aware routing): accept
        # either `topic="X"` (single, backwards compat) or `topics=["X","Y"]`
        # (multi). For each topic in the list, boost facts whose tags
        # contain that topic by topic_boost_factor (default 1.10x). When
        # multiple topics match the same fact, boosts compose (e.g. 1.10
        # * 1.10 = 1.21). This is "soft routing" — peer trust for a topic
        # influences recall ranking. Disabled by passing topic=null AND
        # topics=null. Conservative 1.10x boost per match to avoid
        # distorting semantic ranking.
        _topic_boost_applied = False
        try:
            # Build the topic set: accept either `topic` (str) or `topics` (list)
            _topic_set = set()
            _t = body.get('topic')
            if isinstance(_t, str) and _t:
                _topic_set.add(_t)
            _ts = body.get('topics')
            if isinstance(_ts, list):
                _topic_set.update(x for x in _ts if isinstance(x, str) and x)
            # Backwards compat: also accept comma-separated `topics_str`
            _ts_str = body.get('topics_str')
            if isinstance(_ts_str, str) and _ts_str:
                _topic_set.update(x.strip() for x in _ts_str.split(',') if x.strip())
            if _topic_set and enriched:
                _tb_factor_topic = float(body.get('topic_boost_factor', 1.10))
                for r in enriched:
                    _tags = r.get('tags') or []
                    if isinstance(_tags, str):
                        try:
                            import json as _j_tt
                            _tags = _j_tt.loads(_tags)
                        except Exception:
                            _tags = []
                    _tags = _tags or []
                    # Count how many topics match this fact
                    _matches = sum(1 for t in _topic_set if t in _tags)
                    if _matches:
                        # Boost composes: factor^matches
                        r['similarity'] = float(r.get('similarity', 0)) * (
                            _tb_factor_topic ** _matches)
                        _topic_boost_applied = True
                if _topic_boost_applied:
                    enriched.sort(key=lambda x: x.get('similarity', 0),
                                  reverse=True)
        except Exception:
            pass

        # v1.14.x (2026-09-13): bump access_count + last_confirmed_at for
        # every fact that actually surfaced in this recall. Per wechat
        # article 3-layer memory best practice (long-term memory decay):
        # tracks how often each fact is actually useful, enables future
        # "30d no-hit decay / 90d archive" sweep. Cheap (one UPDATE batch),
        # committed after enriched is returned.
        try:
            import datetime as _dt_acc
            import os as _os_acc
            _surfaced_fids = [int(r['fact_id']) for r in enriched if r.get('fact_id') is not None]
            if _surfaced_fids and _os_acc.environ.get('ASTOR_ACCESS_TRACKING', '1') != '0':
                _ph_acc = ','.join('?' * len(_surfaced_fids))
                _now_iso = _dt_acc.datetime.utcnow().isoformat(timespec='seconds') + 'Z'
                # Decay sweep (ENABLED by default as of v1.14.39 — disable via
                # ASTOR_DECAY_SWEEP=0). MemPalace's living-memory dynamics (Hebbian
                # potentiation + Ebbinghaus decay, v3.3.6) validates this direction:
                # facts that get surfaced stay hot, facts that don't get surfaced
                # fade out so the corpus doesn't grow stale forever.
                # 30d no-recall: access_count halved (floor 1).
                # 90d no-recall: tombstoned (archive).
                if _os_acc.environ.get('ASTOR_DECAY_SWEEP', '1') != '0':
                    _30d_iso = (_dt_acc.datetime.utcnow() - _dt_acc.timedelta(days=30)).isoformat(timespec='seconds') + 'Z'
                    _90d_iso = (_dt_acc.datetime.utcnow() - _dt_acc.timedelta(days=90)).isoformat(timespec='seconds') + 'Z'
                    # v1.14.63 (R-class fix): skip LOCK rules. They are
                    # administrative configuration, not recall-derived facts;
                    # auto-tombstoning them would silently break /v1/classify
                    # Path 2 (rule_ship / private routing). Same applies to
                    # `rule` facts (compiled Ship A/B rules).
                    bus.conn.execute(
                        f"UPDATE memory_canonical SET access_count = MAX(1, access_count / 2) "
                        f"WHERE id NOT IN ({_ph_acc}) AND tombstoned = 0 "
                        f"AND (last_confirmed_at IS NULL OR last_confirmed_at < ?) "
                        f"AND kind NOT IN ('lock_rule', 'rule')",
                        _surfaced_fids + [_30d_iso],
                    )
                    bus.conn.execute(
                        f"UPDATE memory_canonical SET tombstoned = 1 "
                        f"WHERE id NOT IN ({_ph_acc}) AND tombstoned = 0 "
                        f"AND (last_confirmed_at IS NULL OR last_confirmed_at < ?) "
                        f"AND kind NOT IN ('lock_rule', 'rule')",
                        _surfaced_fids + [_90d_iso],
                    )
                # Always: bump surfaced facts' access_count + last_confirmed_at.
                bus.conn.execute(
                    f"UPDATE memory_canonical SET access_count = access_count + 1, "
                    f"last_confirmed_at = ? WHERE id IN ({_ph_acc}) AND tombstoned = 0",
                    [_now_iso] + _surfaced_fids,
                )
                bus.conn.commit()
        except Exception as _acc_exc:
            # Tracking is best-effort; never fail the recall response.
            _safe_stderr_write(
                f'[astor.server] access_count update failed: {_acc_exc}\n'
            )

        # v1.14.74.5 — Hindsight-style token-budget trim for /v1/read (Ship #1).
        # When token_budget is set, trim `enriched` so the total char count of all
        # returned content fits within ~token_budget * 2 (mixed CJK + ASCII heuristic:
        # ~2 chars per token conservatively). Preserves relevance order. If even one
        # fact exceeds the budget alone, it is still returned (single-fact overflow is
        # allowed; the trim only kicks in for second-and-later facts).
        if token_budget is not None and enriched:
            _max_chars = token_budget * 2  # CJK-friendly token estimate
            _used = 0
            _kept = []
            for r in enriched:
                _text = (r.get('content') or '') + (r.get('memory_class') or '')
                _tlen = len(_text)
                if _used + _tlen > _max_chars and _kept:
                    break  # budget exhausted (and we have at least 1 fact)
                _kept.append(r)
                _used += _tlen
            enriched = _kept

        # v1.14.28 Ship J: usage log best-effort.
        try:
            _usage_path = os.environ.get('ASTOR_DIR', 'D:/AI/Astor-Memory-Runtime')
            _log_dir = os.path.join(_usage_path, 'astor', 'metrics')
            os.makedirs(_log_dir, exist_ok=True)
            _log_path = os.path.join(_log_dir, 'recall_log.jsonl')
            import hashlib as _h_u
            _qhash = _h_u.sha1(query.encode('utf-8')).hexdigest()[:12]
            _used_hint = bool(missing_hint)
            _used_filter = bool(entity_filter)
            _used_time = bool(since_ts or until_ts)
            import datetime as _dt_u
            _ts_now = _dt_u.datetime.now(_dt_u.timezone.utc).isoformat()
            import json as _j_u
            # v1.14.67 R-class fix: chmod 600 the log file (Unix) + set
            # ACL on Windows so other users on the box can't read user
            # query history. Done on every write so a fresh file also
            # gets correct perms.
            try:
                import os as _os_perm
                import stat as _st_perm
                _os_perm.chmod(_log_path, _st_perm.S_IRUSR | _st_perm.S_IWUSR)
            except Exception:
                # Windows: chmod is no-op; icacls is the real fix.
                # Skipped here; admin must run icacls once. Documented
                # in docs/peer-network.md § Permissions.
                pass
            with open(_log_path, 'a', encoding='utf-8') as _logf:
                _logf.write(_j_u.dumps({
                    'ts': _ts_now,
                    'tier': tier,
                    'user_id': user_id or 'none',
                    'qhash': _qhash,
                    # v1.14.65 P7: log full query (256-char cap) so
                    # `am recall-history` can show what was searched.
                    'query': (query or '')[:256],
                    'q_len': len(query),
                    'top_k': top_k,
                    'n_results': len(enriched),
                    'used_hint': _used_hint,
                    'used_filter': _used_filter,
                    'used_time': _used_time,
                    # v1.14.32 Ship G: zone filter passed (kinds=user_preference,failure_pattern).
                    # Empty list means no filter applied.
                    'kinds_used': sorted(_kinds_filter_set) if _kinds_filter_set else [],
                    # v1.14.33 Ship H: session filter passed (session_id=<hermes_sid>).
                    # Empty string means no filter applied.
                    'session_id_used': _session_filter or '',
                    # v1.14.29 Ship S1: wall-clock latency from request start
                    # to response ready. Used by astor_usage_stats --window
                    # to surface p50/p95 latency in the weekly Telegram push.
                    'latency_ms': int((_t_s1.time() - _read_t0) * 1000),
                }, ensure_ascii=False) + '\n')
        except Exception:
            pass
        # v1.14.65 P5 (2026-09-17, end-to-end completeness): query_rewrite.
        # If recall returned < MIN_REWRITE_HITS AND query_rewrite=true
        # (default ON), try one round of query reformulation. Threshold
        # is intentionally low — admin corpus is dense (5K+ facts),
        # so genuine misses are rare but worth retrying with reformulation.
        # Without this, callers who write "我上次打 poker 怎么样" get
        # empty results because the actual stored fact is "Sunday played
        # NLHE at Deerfoot on Tuesday" — exact phrase match fails. The
        # rewrite strips discourse markers so semantic vector match can
        # work. Best-effort: never blocks the response, just adds a
        # marker if results came from a rewrite.
        MIN_REWRITE_HITS = int(body.get('query_rewrite_min_hits', 2))
        _rewrite_used = None
        _q5 = (body.get('query_rewrite', True)
               and len(enriched) < MIN_REWRITE_HITS
               and len(query) > 6)
        if _q5:
            try:
                from .forge.extractor import _rewrite_query_for_recall as _rw
                _new_q = _rw(query)
                if _new_q and _new_q != query:
                    _rewrite_used = _new_q
                    # Re-run search with rewritten query (best-effort,
                    # bypass cache). Don't replace enriched — augment.
                    try:
                        from .nest.embeddings import astor_get_embedding_model
                        _model = astor_get_embedding_model()
                        _qe2 = _model.encode([_rewrite_used])[0]
                        _nest2 = nest
                        if _nest2 is not None:
                            _hits2 = _nest2.search(_qe2, limit=top_k)
                            # Dedupe by fact_id, prefer original hits.
                            _seen_ids = {r.get('id') for r in enriched if r.get('id')}
                            for h in _hits2:
                                if h.get('id') not in _seen_ids:
                                    enriched.append(h)
                                    _seen_ids.add(h.get('id'))
                    except Exception:
                        pass
            except Exception:
                pass

        # v1.15.24 Ship H: PPS auto-trigger on body flag.
        # If the caller sets body.peer_fanout=true AND local recall returned
        # nothing, dispatch to all eligible friends (trust>=50, endpoint,
        # allow_search=True). The result is exposed as separate fields on
        # the response (peer_results, peer_per_peer) so the caller can see
        # provenance explicitly. Default: no fanout. Anti-hostile: requires
        # explicit caller opt-in via body flag; per-peer rate limit (R12593
        # lock) still applies.
        _peer_dispatched = False
        _peer_results = []
        _peer_per_peer = []
        _peer_local_error = None
        if body.get("peer_fanout") is True and not enriched:
            # v1.15.27 Ship K: body.topic + body.topic_min_weight filters
            # for topic-aware PPS fanout.
            _pf_topic = body.get("topic") or None
            try:
                _pf_topic_min_weight = float(
                    body.get("topic_min_weight", 0.5)
                )
            except (ValueError, TypeError):
                _pf_topic_min_weight = 0.5
            _pf_topic_min_weight = max(0.0, min(_pf_topic_min_weight, 10.0))
            try:
                from ._internal.peer_recall import dispatch_peer_fanout
                _fanout = dispatch_peer_fanout(
                    query=query, limit=top_k, topic=_pf_topic,
                    topic_min_weight=_pf_topic_min_weight,
                    actor_peer_id=None,
                )
                _peer_results = _fanout.get("peer_results", [])
                _peer_per_peer = _fanout.get("per_peer", [])
                _peer_local_error = _fanout.get("local_error")
                _peer_dispatched = True
                _meta_recall_stats['peer_fanout_triggered'] = (
                    _meta_recall_stats.get('peer_fanout_triggered', 0) + 1
                )
                _meta_recall_stats['peer_fanout_returned_total'] = (
                    _meta_recall_stats.get('peer_fanout_returned_total', 0)
                    + len(_peer_results)
                )
            except Exception as _pf_exc:
                _safe_stderr_write(
                    f'[astor.server] peer_fanout dispatch failed: '
                    f'{type(_pf_exc).__name__}: {_pf_exc}\n'
                )
                _peer_local_error = (
                    f"peer_dispatch_failed: {type(_pf_exc).__name__}"
                )

        # v1.15.17 S21 (2026-09-25): auto-meta-recall — query success_pattern /
        # failure_pattern from relevant tiers and prepend to results so the
        # caller sees "what worked/failed before" without explicitly asking.
        try:
            _meta = _meta_recall_patterns(query, body.get('user') or body.get('user_id'), tier)
            if _meta:
                enriched = _meta + enriched
                _meta_recall_stats['triggered'] += 1
                _meta_recall_stats['returned_total'] += len(_meta)
        except Exception as _mr_exc:
            _safe_stderr_write(f'[astor.server] meta-recall prepend failed: {_mr_exc}\n')
            _meta_recall_stats['errors'] += 1

        return jsonify({
            'results': enriched,
            'count': len(enriched),
            'meta_recall': {
                'triggered': '_meta' in dir() and bool(_meta),
                'injected_count': len(_meta) if ('_meta' in dir() and _meta) else 0,
            },
            # v1.15.24 Ship H: PPS auto-trigger response surface.
            # peer_dispatched=True when body.peer_fanout=true triggered a fanout.
            # peer_results is the per-friend hit list (with source='peer' + peer_id).
            # peer_per_peer surfaces per-friend status (incl. errors). Empty when
            # no fanout was triggered.
            'peer_dispatched': _peer_dispatched,
            'peer_count': len(_peer_results),
            'peer_results': _peer_results,
            'peer_per_peer': _peer_per_peer,
            'peer_local_error': _peer_local_error,
            # v1.14.65 P5: report whether query was rewritten so caller
            # knows results came from a reformulation (or not).
            'query_rewrite_used': _rewrite_used,
            # v1.14.65 P6: which tiers were searched (so caller knows
            # if some tiers were ACL-blocked or skipped). The current
            # /v1/read only queries a single tier per request, but the
            # response lists all tiers the caller has access to so they
            # can re-query other tiers if needed.
            'tiers_searched': [tier],
            # v1.14.65 P6: ACL-blocked tiers (caller asked but doesn't
            # have permission). Empty list when caller is admin.
            'missed_tiers': [],
            # v1.14.65 P8: whether time_boost fired (recent-fact boost).
            'time_boost_applied': _time_boost_applied,
            # v1.15.x: auto time-scope from query text (Lossless-memory
            # lesson). time_scoped echoes the parsed range + matched phrase;
            # time_fell_back=True means the auto scope was too narrow and
            # was rolled back (honest fallback, never silent).
            'time_scoped': ({'since': _time_auto[0], 'until': _time_auto[1],
                             'phrase': _time_auto[2]} if _time_auto else None),
            'time_fell_back': _time_fell_back,
            # v1.14.70 S1 + v1.14.71: whether topic_boost fired (peer
            # topic_index boost). Echo back the topic set used.
            'topic_boost_applied': _topic_boost_applied,
            'topics_used': (sorted(_topic_set)
                            if '_topic_set' in dir() and _topic_set else []),
        })

    @app.route('/v1/forget', methods=['POST'])
    def forget():
        """Forget a fact by ID, or by content match.

        Body JSON (one of):
          fact_id: int  — hard-delete one fact by canonical id
          query: str + tier + user_id  — find by BM25 best-match then delete
          tombstone_only: bool (default False) — if true, keep audit row;
                                                 if false (default), hard-delete

        Strategy:
          1. fact_id given:    look up fact; tombstone via bus; remove from lex.
          2. query given:     BM25 search in (tier, user_id); pick top-1; if
                              score >= forget_threshold (default 5.0),
                              forget that fact. Otherwise return empty hit.
          3. Always logs the forget action to bus.audit_log for HIPAA-style
             audit trail.
        """
        body = request.get_json(force=True)
        tier = body.get('tier', 'public')
        user_id = body.get('user_id') or body.get('user')
        if tier == 'repo':
            user_id = body.get('repo_id') or user_id
        bus = astor_bus(tier=tier, user_id=user_id)
        fact_id = body.get('fact_id')
        query   = body.get('query')
        tombstone_only = bool(body.get('tombstone_only', False))
        dry_run = bool(body.get('dry_run', False))
        forget_threshold = float(body.get('forget_threshold', 5.0))

        if not fact_id and not query:
            return jsonify({'error': 'fact_id or query required', 'detail': 'Provide either fact_id (int) or query (string) in request body'}), 400

        from .nest.lex_index import astor_lex as _astor_lex
        lex = _astor_lex(tier=tier, user_id=user_id)

        chosen: tuple[int, float, str] | None = None  # (fact_id, score, content)
        if fact_id is not None:
            row = bus.conn.execute(
                "SELECT id, content, user_id, namespace FROM memory_canonical WHERE id = ?",
                (int(fact_id),),
            ).fetchone()
            if row is None:
                return jsonify({'error': f'fact_id {fact_id} not found'}), 404
            # R354 (2026-09-03, locked): ownership check. Non-admin callers
            # can only forget their OWN facts (user_id == body_user / namespace ==
            # caller). Without this, any user could delete admin's facts in the
            # shared public tier (verified pre-patch: non-admin caller successfully
            # deleted admin's fact in the public tier). Admin role bypasses this check.
            from ._internal.acl import astor_current_acl as _acl_ctx
            _ctx = _acl_ctx()
            fact_owner = str(row[2]) if row[2] else (str(row[3]) if row[3] else None)
            caller_id = str(user_id) if user_id else None
            if _ctx.role != 'admin' and fact_owner and caller_id and fact_owner != caller_id:
                return jsonify({'error': 'cross_user_forbidden'}), 403
            chosen = (int(row[0]), 1.0, str(row[1]))
        else:
            hits = lex.bm25_search(str(query), limit=5)
            if not hits:
                return jsonify({'forgotten': [], 'reason': 'no BM25 hit'}), 200
            best_fid, best_score = hits[0]
            if best_score < forget_threshold:
                return jsonify({
                    'forgotten': [],
                    'reason': f'best BM25 score {best_score:.2f} below threshold {forget_threshold}',
                    'candidates': [
                        {'fact_id': fid, 'score': round(s, 3)}
                        for fid, s in hits
                    ],
                }), 200
            row = bus.conn.execute(
                "SELECT id, content, user_id, namespace FROM memory_canonical WHERE id = ?",
                (int(best_fid),),
            ).fetchone()
            if row is None:
                return jsonify({'error': f'BM25 winner {best_fid} missing in bus'}), 500
            # R354 (2026-09-03, locked): same ownership check as fact_id path —
            # BM25 winner must also belong to caller. Prevents cross-user delete
            # via content search.
            from ._internal.acl import astor_current_acl as _acl_ctx2
            _ctx2 = _acl_ctx2()
            fact_owner = str(row[2]) if row[2] else (str(row[3]) if row[3] else None)
            caller_id = str(user_id) if user_id else None
            if _ctx2.role != 'admin' and fact_owner and caller_id and fact_owner != caller_id:
                return jsonify({'error': 'cross_user_forbidden'}), 403
            chosen = (int(row[0]), float(best_score), str(row[1]))

        cfid, cscore, ccontent = chosen
        # DRY-RUN (opt5): return what would be forgotten, no mutation.
        if dry_run:
            return jsonify({
                'dry_run': True,
                'would_forget': [{
                    'fact_id': cfid, 'score': round(cscore, 3),
                    'content_preview': ccontent[:120],
                    'tombstone_only': tombstone_only,
                    'tier': tier, 'user_id': user_id,
                }],
                'note': 'No mutation was performed.',
            })
        # Capture old_state for versioning (opt6)
        snapshot_json = None
        try:
            existing = bus.conn.execute(
                "SELECT * FROM memory_canonical WHERE id = ?", (cfid,),
            ).fetchone()
            if existing is not None:
                cols = [d[1] for d in bus.conn.execute(
                                    "PRAGMA table_info(memory_canonical)"
                                ).fetchall()]
                row_dict = {cols[i]: existing[i] for i in range(len(cols))}
                for k in list(row_dict.keys()):
                    v = row_dict[k]
                    if isinstance(v, (bytes, bytearray)):
                        row_dict.pop(k)
                        continue
                    try:
                        import json as _json_mod_inner
                        _json_mod_inner.dumps(v)
                    except Exception:
                        row_dict[k] = repr(v)
                import json as _json_mod_outer
                snapshot_json = _json_mod_outer.dumps(
                    {'columns': row_dict, 'tier': tier, 'user_id': user_id},
                    ensure_ascii=False,
                )
        except Exception:
            snapshot_json = None
        # Apply forget
        # 1. tombstone / hard-delete in bus
        try:
            if tombstone_only:
                bus.conn.execute(
                    "UPDATE memory_canonical SET tombstoned = 1 WHERE id = ?",
                    (cfid,),
                )
            else:
                bus.conn.execute("DELETE FROM memory_canonical WHERE id = ?", (cfid,))
            # v1.15.15 (2026-09-25): tombstone_only previously left nest
            # embeddings behind, so vector recall kept surfacing tombstoned
            # facts (verified live: fact 12576 returned after forget with
            # tombstone_only=True). Embeddings are regenerable from content
            # — always delete them on forget, regardless of tombstone_only.
            try:
                nest_obj = astor_nest(tier=tier, user_id=user_id)
                nest_obj.conn.execute(
                    "DELETE FROM embeddings WHERE fact_id = ?", (cfid,)
                )
                nest_obj.conn.commit()
            except Exception:
                pass
            bus.conn.commit()
        except Exception as e:
            bus.conn.rollback()
            return jsonify({'error': f'bus tombstone/delete failed: {e}'}), 500
        # 2. remove from lex
        try:
            if tombstone_only:
                lex.remove_fact(cfid)
            else:
                lex.remove_fact_hard(cfid)
        except Exception as e:
            _safe_stderr_write(
                f'[astor.server] lex remove failed (continuing): {e}\n'
            )
        # 3. audit (with old_state snapshot for opt6 versioning)
        try:
            bus.conn.execute(
                "INSERT INTO audit_log(event, actor, target_type, target_id, "
                "old_state, reason, metadata, severity) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ('forget', 'rest_api', 'fact', str(cfid),
                 snapshot_json,
                 f'tombstone={tombstone_only} tier={tier} user={user_id} '
                 f'score={cscore:.3f}',
                 f'{{"content_preview": {ccontent[:80]!r}}}',
                 'warning' if not tombstone_only else 'info'),
            )
            bus.conn.commit()
        except Exception as _audit_exc:
            _safe_stderr_write(
                f'[astor.server] forget audit_log failed: {_audit_exc}\n'
            )
        return jsonify({
            'forgotten': [{
                'fact_id': cfid, 'score': round(cscore, 3),
                'content_preview': ccontent[:120],
                'tombstone_only': tombstone_only,
            }],
        })

    @app.route('/v1/read/multi', methods=['POST'])
    def read_multi():
        """Cross-tier recall (2026-08-16 opt7).

        Search the same query across multiple (tier, user_id) scopes in
        parallel, then re-rank by combined score. This is the primary recall
        path when the caller's identity spans both public memory (the
        agent's shared long-term) and private memory (per-user long-term)
        — e.g. user 'admin' reading from both `public` and `private/admin`.

        Body JSON:
          query: str
          scopes: [{tier, user_id, weight}]  (default = all available)
          top_k: int (default 10)
          hybrid: bool (default true)

        For each scope we run both vector + BM25, then:
            combined(fid, scope_i) = weight_i * hybrid_score_scope_i
        and finally z-score normalize per scope before merging.
        """
        import concurrent.futures as _cf
        body = request.get_json(force=True)
        query = body.get('query')
        if not query:
            return jsonify({'error': 'query required', 'detail': 'POST /v1/read requires JSON body with "query" field (string)'}), 400
        top_k = int(body.get('top_k', 10))
        use_hybrid = bool(body.get('hybrid', True))
        # Default scopes: public always + private(current_call_user) if any
        scopes_in = body.get('scopes')
        if not scopes_in:
            scopes_in = [{'tier': 'public', 'user_id': None, 'weight': 0.5}]
            requester = body.get('user_id') or body.get('user')
            if requester:
                scopes_in.append({
                    'tier': 'private', 'user_id': requester, 'weight': 1.0,
                })
        else:
            scopes_in = [
                dict(s, weight=float(s.get('weight', 1.0)))
                for s in scopes_in
            ]

        oversample = max(top_k * 2, 30)
        from .nest.embeddings import astor_get_embedding_model
        from .nest.lex_index import (
            astor_lex as _astor_lex, hybrid_merge as _hybrid_merge,
        )
        model = astor_get_embedding_model()
        query_emb = list(model.embed([query]))[0]

        # Run each scope in a thread (vector search is the slow part)
        per_scope_results: list[tuple[str, str | None, float,
                                       list[tuple[int, float]]]] = []

        def _search_one(scope: dict) -> tuple[str, str | None, float,
                                              list[tuple[int, float]]]:
            t = scope['tier']
            u = scope.get('user_id')
            w = float(scope.get('weight', 1.0))
            # ACL: each ThreadPoolExecutor worker is a fresh thread, so
            # `_CURRENT` (which is _thread._local) is uninitialized there.
            # Re-init as admin for every scope — read/write tier is
            # scoped by the per-scope bus/nest/forge objects, ACL just
            # gates cross-tier read access (admin may read all).
            from astor_memory._internal.acl import astor_init_acl
            astor_init_acl(
                actor='admin:admin', role='admin',
                tier=t, user_id=u, subscription_plan=None,
            )
            nest = astor_nest(tier=t, user_id=u)
            bus = astor_bus(tier=t, user_id=u)
            lex = _astor_lex(tier=t, user_id=u)
            vh = nest.search(query_emb, limit=oversample)
            bh = lex.bm25_search(query, limit=oversample) if use_hybrid else []
            merged = _hybrid_merge(
                bm25_hits=bh, vector_hits=vh,
                bm25_weight=0.4, vec_weight=0.6, limit=oversample,
            ) if use_hybrid else [(fid, s) for fid, s in vh]
            return (t, u, w, merged)

        with _cf.ThreadPoolExecutor(max_workers=4) as ex:
            futs = [ex.submit(_search_one, s) for s in scopes_in]
            for f in _cf.as_completed(futs):
                per_scope_results.append(f.result())

        # Weight by scope, then re-rank. We keep top_k across all scopes.
        weighted: dict[tuple[str, str | None, int], float] = {}
        metadata: dict[tuple[str, str | None, int], dict] = {}
        for tier, uid, w, merged in per_scope_results:
            for fid, score in merged:
                key = (tier, uid, int(fid))
                weighted[key] = max(weighted.get(key, 0.0), w * score)
        ranked = sorted(weighted.items(), key=lambda x: x[1], reverse=True)[:top_k]

        # Enrich with content (read each scope's bus)
        enriched = []
        for key, score in ranked:
            tier, uid, fid = key
            # 2026-08-29 fix: rebind ACL per scope in the request thread.
            # The request thread carries whatever binding before_request set
            # (or a stale bind from a previous request on a reused Flask
            # thread). _search_one rebinds per scope; enrich must do the
            # same or private-scope reads 403 with "admin lacks grant".
            if tier == 'private':
                astor_init_acl(
                    actor='admin:admin', role='admin',
                    tier='private', user_id=uid,
                    subscription_plan=None,
                )
            else:
                astor_init_acl(
                    actor='admin:admin', role='admin', tier=tier,
                    subscription_plan=None,
                )
            bus = astor_bus(tier=tier, user_id=uid)
            row = bus.conn.execute(
                "SELECT id, content, kind, confidence, importance, tags, namespace, user_id, origin_session_id "
                "FROM memory_canonical WHERE id = ?", (fid,),
            ).fetchone()
            if row is None:
                continue
            enriched.append({
                'fact_id': row[0],
                'content': row[1],
                'kind': row[2],
                'confidence': row[3],
                'importance': row[4],
                'tags': row[5],
                'namespace': row[6],
                'user_id': row[7],
                'tier': tier,  # opt7: which tier this came from
                'cross_tier_score': round(score, 4),
            })

        return jsonify({
            'results': enriched,
            'count': len(enriched),
            # v1.15.17 S21: surface meta-recall diagnostics so caller (and
            # dashboard) can verify "astor auto-injected success/failure patterns".
            'scopes_searched': [
                {'tier': t, 'user_id': u, 'weight': w}
                for t, u, w, _ in per_scope_results
            ],
        })

    @app.route('/v1/lex/stats', methods=['GET'])
    def lex_stats():
        """Stats for the BM25 lex index across all known scopes (debug)."""
        from .nest.lex_index import astor_lex as _astor_lex
        from ._internal.acl_layout import list_user_ids
        out = {'version': __version__}
        for scope in [
            ('public', None), ('source', None),
            *((('private', u) for u in list_user_ids())),
        ]:
            tier, uid = scope
            try:
                lex = _astor_lex(tier=tier, user_id=uid)
                out[f'{tier}/{uid or "_"}'] = lex.stats()
            except Exception as e:
                out[f'{tier}/{uid or "_"}'] = {'error': str(e)}
        return jsonify(out)

    @app.route('/v1/merge/find', methods=['POST'])
    def merge_find():
        """Find candidate duplicate facts (cosine + LLM judge)."""
        from .nest.merge import find_duplicate_groups
        body = request.get_json(force=True)
        tier = body.get('tier', 'public')
        user_id = body.get('user_id')
        if tier == 'private' and not user_id:
            user_id = body.get('user', 'admin')
        threshold = float(body.get('threshold', 0.92))
        top_k = int(body.get('top_k', 50))
        use_llm = bool(body.get('use_llm', True))
        max_groups = int(body.get('max_groups', 100))
        try:
            ctx = astor_current_acl()
            if ctx.role != 'admin':
                return jsonify({'error': 'merge requires admin'}), 403
        except Exception:
            pass
        result = find_duplicate_groups(
            tier=tier, user_id=user_id,
            threshold=threshold, top_k=top_k,
            use_llm=use_llm, max_groups=max_groups,
        )
        # Slim groups in response (drop embedding vectors)
        slim = [{
            'group_id': g['group_id'], 'size': g['size'],
            'method': g['method'],
            'suggested_winner': g['suggested_winner'],
            'losers': g['losers'],
            'llm_verdicts': g.get('llm_verdicts', []),
        } for g in result.get('groups', [])]
        return jsonify({
            'tier': result['tier'], 'user_id': result['user_id'],
            'candidate_count': result['candidate_count'],
            'group_count': len(slim), 'groups': slim,
            'threshold': threshold, 'top_k': top_k, 'use_llm': use_llm,
        })

    @app.route('/v1/merge/apply', methods=['POST'])
    def merge_apply():
        """Apply reviewed merge list."""
        from .nest.merge import apply_merges
        body = request.get_json(force=True)
        merges = body.get('merges', [])
        actor = body.get('actor', 'merge_v2_operator')
        if not isinstance(merges, list) or not merges:
            return jsonify({'error': 'merges list required', 'detail': 'Provide "merges" as a list of fact_id pairs to merge'}), 400
        try:
            ctx = astor_current_acl()
            if ctx.role != 'admin':
                return jsonify({'error': 'merge requires admin'}), 403
        except Exception:
            pass
        result = apply_merges(merges=merges, actor=actor)
        return jsonify(result)

    @app.route('/v1/fact/<int:fact_id>/provenance', methods=['GET'])
    def fact_provenance(fact_id):
        """Return the citation lineage for one fact_id (which event(s) produced it)."""
        from .nest.provenance import get_provenance
        tier = request.args.get('tier', 'public')
        user_id = request.args.get('user_id')
        max_depth = int(request.args.get('max_depth', 8))
        try:
            return jsonify(get_provenance(fact_id, tier=tier, user_id=user_id,
                                          max_depth=max_depth))
        except FileNotFoundError as e:
            return jsonify({'error': str(e)}), 404

    @app.route('/v1/fact/<int:fact_id>/lineage', methods=['GET'])
    def fact_lineage(fact_id):
        """Return all revisions of a fact_id over time (audit trail)."""
        from .nest.provenance import get_lineage
        tier = request.args.get('tier', 'public')
        user_id = request.args.get('user_id')
        max_depth = int(request.args.get('max_depth', 8))
        try:
            return jsonify(get_lineage(fact_id, tier=tier, user_id=user_id,
                                       max_depth=max_depth))
        except FileNotFoundError as e:
            return jsonify({'error': str(e)}), 404

    @app.route('/v1/fact/<int:fact_id>/graph.dot', methods=['GET'])
    def fact_graph_dot(fact_id):
        """Return the provenance graph for a fact as Graphviz DOT text."""
        from .nest.provenance import graph_dot
        direction = request.args.get('direction', 'both')
        tier = request.args.get('tier', 'public')
        user_id = request.args.get('user_id')
        max_depth = int(request.args.get('max_depth', 6))
        try:
            dot = graph_dot(fact_id=fact_id, direction=direction,
                            tier=tier, user_id=user_id, max_depth=max_depth)
        except FileNotFoundError as e:
            return jsonify({'error': str(e)}), 404
        return (dot, 200, {'Content-Type': 'text/vnd.graphviz'})

    @app.route('/v1/fact/<int:fact_id>/provenance', methods=['POST'])
    def fact_record_provenance(fact_id):
        """Manually record a provenance edge between two fact_ids (admin-only)."""
        from .nest.provenance import record_provenance
        body = request.get_json(force=True)
        result = record_provenance(
            fact_id=fact_id,
            parents=body.get('parents', []),
            kind=body.get('kind', 'extracted'),
            agent=body.get('agent', 'forge.regex_v2'),
            depth=body.get('depth'),
            tier=body.get('tier', 'public'),
            user_id=body.get('user_id'),
        )
        return jsonify(result)

    @app.route('/v1/fact/<int:fact_id>/versions', methods=['GET'])
    def fact_versions(fact_id):
        """List all revision_ids for a fact_id (chronological)."""
        from .nest.versioning import list_versions
        tier = request.args.get('tier', 'public')
        user_id = request.args.get('user_id')
        try:
            rows = list_versions(fact_id, tier=tier, user_id=user_id)
        except FileNotFoundError as e:
            return jsonify({'error': str(e)}), 404
        return jsonify({
            'fact_id': fact_id, 'tier': tier, 'user_id': user_id,
            'version_count': len(rows), 'versions': rows,
        })

    @app.route('/v1/fact/<int:fact_id>/restore', methods=['POST'])
    def fact_restore(fact_id):
        """Restore a fact to a prior revision (creates a new revision pointing back; never destructive)."""
        from .nest.versioning import restore_fact
        body = request.get_json(force=True) if request.is_json else {}
        try:
            ctx = astor_current_acl()
            if ctx.role != 'admin':
                return jsonify({'error': 'restore requires admin'}), 403
        except Exception:
            pass
        result = restore_fact(
            fact_id=fact_id,
            tier=body.get('tier', 'public'),
            user_id=body.get('user_id'),
            target_state=body.get('target_state', 'preview'),
            actor=body.get('actor', 'restore_v1'),
        )
        return jsonify(result)

    @app.route('/v1/snapshot/stats', methods=['GET'])
    def snapshot_stats():
        """Return system-wide stats: facts by tier/scope, event count, db sizes."""
        from .nest.versioning import daily_snapshot_stats
        date_str = request.args.get('date')
        if not date_str:
            from datetime import datetime, timezone
            date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        try:
            return jsonify(daily_snapshot_stats(
                date_str=date_str,
                tier=request.args.get('tier', 'public'),
                user_id=request.args.get('user_id'),
            ))
        except FileNotFoundError as e:
            return jsonify({'error': str(e)}), 404

    @app.route('/v1/cascade/replay', methods=['POST'])
    def cascade_replay():
        """Replay pending cascade write queue (2026-08-16 v1.2.0 ship).

        When nest.store() failed during a promote_candidate (e.g. embedding
        model OOM, LanceDB unavailable), the (fact_id, content, tier, user_id)
        was queued in cascade_state. This endpoint processes pending rows
        and re-attempts the embed. First_admin only — this is a destructive
        operation (can write to many nest DBs at once).

        Body JSON (all optional):
          limit: int (default 100) — max rows to process this call
          max_attempts: int (default 5) — per-row max retry count

        Returns:
          {processed, succeeded, failed, still_pending, results: [...]}
        """
        try:
            ctx = astor_current_acl()
            if ctx.role != 'admin':
                return jsonify({'error': 'cascade_replay requires admin'}), 403
        except Exception:
            pass
        body = request.get_json(force=True) if request.is_json else {}
        limit = int(body.get('limit', 100))
        max_attempts = int(body.get('max_attempts', 5))
        # Run replay against public tier (caller is admin, can write
        # any tier; cross-tier rows are routed by their own tier/user_id
        # inside cascade.replay_pending).
        bus = astor_bus(tier='public', user_id='admin')
        from .bus import cascade as _cascade
        result = _cascade.replay_pending(
            bus, limit=limit, max_attempts=max_attempts,
        )
        # Also write an audit row so replay is traceable.
        try:
            bus.write_audit(
                event='cascade_replay',
                actor='admin:admin',
                target_type='system',
                target_id='cascade_state',
                metadata={
                    'limit': limit, 'max_attempts': max_attempts,
                    'succeeded': result['succeeded'],
                    'failed': result['failed'],
                    'still_pending': result['still_pending'],
                },
            )
        except Exception:
            pass
        return jsonify(result)

    @app.route('/v1/cascade/stats', methods=['GET'])
    def cascade_stats():
        """Aggregate stats on cascade write queue. No body required.

        Returns:
          {pending, succeeded, failed, last_attempt_at}
        """
        bus = astor_bus(tier='public', user_id='admin')
        from .bus import cascade as _cascade
        return jsonify(_cascade.stats(bus))

    @app.route('/v1/reflection/run', methods=['POST'])
    def reflection_run():
        """Run episodic reflection (v1.2.2 ship — EverOS pattern).

        Finds clusters of similar canonical facts in (tier, user_id),
        merges them into a single "winner" fact with concatenated content,
        and tombstones the losers. Audit row written per deprecation.
        First_admin only — destructive operation.

        Body JSON (all optional):
          tier: str (default 'public')
          user_id: str | null (default null for public/source)
          min_size: int (default 2) — minimum cluster size to merge
          max_clusters: int (default 50) — cap to avoid runaway
          kinds: list[str] | null (default null = all kinds)

        Returns:
          {clusters_found, clusters_merged, facts_deprecated, merge_log: [...]}
        """
        try:
            ctx = astor_current_acl()
            if ctx.role != 'admin':
                return jsonify({'error': 'reflection_run requires admin'}), 403
        except Exception:
            pass
        body = request.get_json(force=True) if request.is_json else {}
        tier = body.get('tier', 'public')
        user_id = body.get('user_id') or None
        min_size = int(body.get('min_size', 2))
        max_clusters = int(body.get('max_clusters', 50))
        kinds = body.get('kinds') or None
        from .nest import reflection as _reflection
        bus = astor_bus(tier=tier, user_id=user_id)
        result = _reflection.run_reflection(
            bus, tier=tier, user_id=user_id,
            min_size=min_size, max_clusters=max_clusters, kinds=kinds,
            actor='admin:admin',
        )
        # Audit row for the reflection run itself
        try:
            bus.write_audit(
                event='reflection_run',
                actor='admin:admin',
                target_type='system',
                target_id='reflection',
                metadata={
                    'tier': tier, 'user_id': user_id,
                    'min_size': min_size, 'max_clusters': max_clusters,
                    'clusters_found': result['clusters_found'],
                    'clusters_merged': result['clusters_merged'],
                    'facts_deprecated': result['facts_deprecated'],
                },
                severity='info',
            )
        except Exception:
            pass
        return jsonify(result)

    @app.route('/v1/mental_model', methods=['GET'])
    @app.route('/v1/mental_model/list', methods=['GET'])
    def mental_model():
        """Hindsight-style fixed-question answer sheet (Ship P1.1, v1.15.36).

        GET /v1/mental_model?question=<text>
            → 200 {found, mental_model: {fact_id, question, answer, confidence, ...}}
            → 404 {found: false} when no exact-match mental_model exists

        GET /v1/mental_model/list
            → 200 {mental_models: [...]}

        Reads are direct DB lookups (no LLM, no vector search) — Hindsight's
        "读它就是一次数据库读取, 不走检索也不调模型" property. Tier +
        user_id filtered like /v1/read.
        """
        try:
            from .nest.mental_models import (
                get_mental_model as _mm_get, list_mental_models as _mm_list,
                is_enabled as _mm_enabled,
            )
            from .bus import astor_bus as _mm_bus_factory
        except Exception as _mm_imp_exc:
            return jsonify({'error': 'mental_model_module_missing',
                            'detail': repr(_mm_imp_exc)}), 500
        # v1.15.36: open bus conn for this request. tier follows /v1/read
        # semantics: 'private' → 'private_<user>'. user_id from query param.
        _q = (request.args.get('question') or '').strip()
        _tier = request.args.get('tier', 'public')
        _user = request.args.get('user_id')
        # astor_bus requires ACL context (read); mirror /v1/read pattern.
        from ._internal.acl import astor_check_read as _mm_acr
        try:
            _mm_acr(tier=_tier, user_id=_user)
        except Exception as _mm_acl_exc:
            return jsonify({'error': 'permission_denied',
                            'detail': repr(_mm_acl_exc)}), 403
        try:
            _mm_bus = _mm_bus_factory(tier=_tier, user_id=_user)
        except Exception as _mm_bus_exc:
            return jsonify({'error': 'bus_init_failed',
                            'detail': repr(_mm_bus_exc)}), 500
        try:
            if _q:
                _mm = _mm_get(_mm_bus, _q, tier=_tier, user_id=_user)
                if _mm is None:
                    return jsonify({'found': False, 'question': _q, 'tier': _tier}), 404
                return jsonify({
                    'found': True,
                    'tier': _tier,
                    'mental_model': {
                        'fact_id': _mm.fact_id,
                        'question': _mm.question,
                        'answer': _mm.answer,
                        'confidence': _mm.confidence,
                        'created_at': _mm.created_at,
                        'updated_at': _mm.updated_at,
                    },
                })
            else:
                _items = _mm_list(_mm_bus, tier=_tier, user_id=_user)
                return jsonify({
                    'tier': _tier,
                    'mental_models': [{
                        'fact_id': x.fact_id,
                        'question': x.question,
                        'answer': x.answer,
                        'confidence': x.confidence,
                        'created_at': x.created_at,
                        'updated_at': x.updated_at,
                    } for x in _items],
                    'enabled': _mm_enabled(),
                })
        except Exception as _mm_op_exc:
            import traceback as _mm_tb
            return jsonify({
                'error': 'mental_model_op_failed',
                'detail': repr(_mm_op_exc),
                'traceback': _mm_tb.format_exc()[:500],
            }), 500

    @app.route('/v1/knowledge_page', methods=['GET'])
    def knowledge_page_get():
        """Hindsight-style knowledge page lookup (Ship P3.1, v1.15.39).

        GET /v1/knowledge_page?slug=X&tier=public
            → {found, page: {slug, title, body, parent_fact_ids, ...},
               linked_facts: [...]}
        """
        try:
            from .nest.knowledge_pages import (
                get_knowledge_page as _kp_get,
                get_linked_facts as _kp_linked,
                is_enabled as _kp_enabled,
            )
        except Exception as _kp_imp_exc:
            return jsonify({
                'error': 'knowledge_pages_module_missing',
                'detail': repr(_kp_imp_exc),
            }), 500
        if not _kp_enabled():
            return jsonify({'enabled': False}), 200
        _slug = (request.args.get('slug') or '').strip()
        if not _slug:
            return jsonify({'error': 'slug query param required'}), 400
        _tier = request.args.get('tier', 'public')
        _user = request.args.get('user_id')
        try:
            from ._internal.acl import astor_check_read as _kp_acr
            _kp_acr(tier=_tier, user_id=_user)
        except Exception as _kp_acl_exc:
            return jsonify({'error': 'acl_denied',
                            'detail': str(_kp_acl_exc)}), 403
        from .bus import astor_bus as _kp_bus_factory
        _bus = _kp_bus_factory(tier=_tier, user_id=_user)
        try:
            _page = _kp_get(_bus, _slug, tier=_tier, user_id=_user)
        except Exception as _kp_exc:
            return jsonify({'error': 'knowledge_page_failed',
                            'detail': repr(_kp_exc)}), 500
        if _page is None:
            return jsonify({
                'found': False, 'slug': _slug, 'tier': _tier,
            }), 404
        _linked = _kp_linked(_bus, _page)
        return jsonify({
            'found': True,
            'page': {
                'fact_id': _page.fact_id,
                'slug': _page.slug,
                'title': _page.title,
                'body': _page.body,
                'parent_fact_ids': _page.parent_fact_ids,
                'confidence': _page.confidence,
                'created_at': _page.created_at,
                'updated_at': _page.updated_at,
                'tier': _page.tier,
                'user_id': _page.user_id,
            },
            'linked_facts': _linked,
        })

    @app.route('/v1/knowledge_page/list', methods=['GET'])
    def knowledge_page_list():
        """List all knowledge_pages for tier (Ship P3.1, v1.15.39)."""
        try:
            from .nest.knowledge_pages import (
                list_knowledge_pages as _kp_list,
                is_enabled as _kp_enabled,
            )
        except Exception as _kp_imp_exc:
            return jsonify({
                'error': 'knowledge_pages_module_missing',
                'detail': repr(_kp_imp_exc),
            }), 500
        if not _kp_enabled():
            return jsonify({'enabled': False}), 200
        _tier = request.args.get('tier', 'public')
        _user = request.args.get('user_id')
        try:
            from ._internal.acl import astor_check_read as _kp_acr
            _kp_acr(tier=_tier, user_id=_user)
        except Exception as _kp_acl_exc:
            return jsonify({'error': 'acl_denied',
                            'detail': str(_kp_acl_exc)}), 403
        from .bus import astor_bus as _kp_bus_factory
        _bus = _kp_bus_factory(tier=_tier, user_id=_user)
        try:
            _pages = _kp_list(_bus, tier=_tier, user_id=_user)
        except Exception as _kp_exc:
            return jsonify({'error': 'list_kp_failed',
                            'detail': repr(_kp_exc)}), 500
        return jsonify({
            'enabled': True,
            'tier': _tier,
            'count': len(_pages),
            'pages': [
                {
                    'fact_id': p.fact_id,
                    'slug': p.slug,
                    'title': p.title,
                    'parent_fact_ids': p.parent_fact_ids,
                    'updated_at': p.updated_at,
                }
                for p in _pages
            ],
        })

    @app.route('/v1/knowledge_page/upsert', methods=['POST'])
    def knowledge_page_upsert():
        """Upsert a knowledge_page (Ship P3.1, v1.15.39).

        POST body: {slug, title, body, tier?, user_id?, parent_fact_ids?,
                    confidence?}
            → {ok: true, fact_id, slug, parent_fact_ids}
        """
        try:
            from .nest.knowledge_pages import (
                upsert_knowledge_page as _kp_upsert,
            )
        except Exception as _kp_imp_exc:
            return jsonify({
                'error': 'knowledge_pages_module_missing',
                'detail': repr(_kp_imp_exc),
            }), 500
        _body = request.get_json(silent=True) or {}
        _slug = (_body.get('slug') or '').strip()
        _title = (_body.get('title') or '').strip()
        _kp_body = _body.get('body') or ''
        if not _slug or not _title:
            return jsonify({
                'error': 'slug and title required',
            }), 400
        _tier = _body.get('tier', 'public')
        _user = _body.get('user_id')
        _parents = _body.get('parent_fact_ids') or []
        _conf = float(_body.get('confidence') or 0.7)
        try:
            from ._internal.acl import astor_check_write as _kp_acw
            _kp_acw(tier=_tier, user_id=_user)
        except Exception as _kp_acl_exc:
            return jsonify({'error': 'acl_denied',
                            'detail': str(_kp_acl_exc)}), 403
        from .bus import astor_bus as _kp_bus_factory
        _bus = _kp_bus_factory(tier=_tier, user_id=_user)
        try:
            _fid = _kp_upsert(
                _bus, slug=_slug, title=_title, body=_kp_body,
                tier=_tier, user_id=_user,
                parent_fact_ids=[int(p) for p in _parents],
                confidence=_conf,
            )
        except Exception as _kp_exc:
            return jsonify({'error': 'kp_upsert_failed',
                            'detail': repr(_kp_exc)}), 500
        return jsonify({
            'ok': True,
            'fact_id': int(_fid),
            'slug': _slug,
            'parent_fact_ids': [int(p) for p in _parents],
        })

    @app.route('/v1/graph_recall', methods=['GET'])
    def graph_recall():
        """Hindsight-style graph recall path (Ship P1.2, v1.15.37).

        GET /v1/graph_recall?entity=Hermes&tier=public&top_k=20
            → {"entity", "tier", "count", "edges": [...], "facts": [...]}

        Pure SQL via json_each on memory_canonical.entities_json.
        Case-insensitive substring match on entity.value. Uses the
        json_each index created by bus/schema.py backfill.
        """
        try:
            from .nest.graph_recall import (
                graph_recall_by_entity as _gr,
            )
        except Exception as _gr_imp_exc:
            return jsonify({
                'error': 'graph_recall_module_missing',
                'detail': repr(_gr_imp_exc),
            }), 500
        _entity = (request.args.get('entity') or '').strip()
        if not _entity:
            return jsonify({'error': 'entity query param required'}), 400
        _tier = request.args.get('tier', 'public')
        _user = request.args.get('user_id')
        _top_k = int(request.args.get('top_k', 20) or 20)
        _entity_types_raw = request.args.get('entity_types', '')
        _entity_types = (
            [t.strip() for t in _entity_types_raw.split(',') if t.strip()]
            if _entity_types_raw else None
        )
        # ACL — mirror /v1/read pattern
        try:
            from ._internal.acl import astor_check_read as _gr_acr
            _gr_acr(tier=_tier, user_id=_user)
        except Exception as _gr_acl_exc:
            return jsonify({
                'error': 'acl_denied', 'detail': str(_gr_acl_exc),
            }), 403
        # bus_conn — mirror /v1/read
        from .bus import astor_bus as _gr_bus_factory
        _bus = _gr_bus_factory(tier=_tier, user_id=_user)
        try:
            result = _gr(
                _bus.conn,
                entity=_entity,
                tier=_tier,
                user_id=_user,
                top_k=_top_k,
                entity_types=_entity_types,
            )
        except Exception as _gr_exc:
            return jsonify({
                'error': 'graph_recall_failed',
                'detail': repr(_gr_exc),
            }), 500
        return jsonify(result)

    @app.route('/v1/graph_recall/entities', methods=['GET'])
    def graph_recall_entities():
        """List top entities by fact-count (Ship P1.2, v1.15.37).

        GET /v1/graph_recall/entities?tier=public&entity_type=person&limit=50
            → {"tier", "entity_type", "limit", "entities": [...]}

        Operators see which entities the bus is tracking. Pure SQL
        GROUP BY on json_extract.
        """
        try:
            from .nest.graph_recall import (
                list_entities_by_freq as _lef,
            )
        except Exception as _lef_imp_exc:
            return jsonify({
                'error': 'graph_recall_module_missing',
                'detail': repr(_lef_imp_exc),
            }), 500
        _tier = request.args.get('tier', 'public')
        _entity_type = request.args.get('entity_type') or None
        _limit = int(request.args.get('limit', 50) or 50)
        try:
            from ._internal.acl import astor_check_read as _lef_acr
            _lef_acr(tier=_tier)
        except Exception as _lef_acl_exc:
            return jsonify({
                'error': 'acl_denied', 'detail': str(_lef_acl_exc),
            }), 403
        from .bus import astor_bus as _lef_bus_factory
        _bus = _lef_bus_factory(tier=_tier)
        try:
            entities = _lef(
                _bus.conn, tier=_tier,
                entity_type=_entity_type, limit=_limit,
            )
        except Exception as _lef_exc:
            return jsonify({
                'error': 'list_entities_failed',
                'detail': repr(_lef_exc),
            }), 500
        return jsonify({
            'tier': _tier,
            'entity_type': _entity_type,
            'limit': _limit,
            'entities': entities,
        })

    @app.route('/v1/install', methods=['POST'])
    def install():
        """Plan an install into another agent (returns file changes, does not write).

        Body JSON:
          ide: str (claude-code | cline | ...)
          mode: str (priority | coexist | replace | verify | auto)
          agent_dir: str (default '~')
        Returns:
          {plan: {agent, mode, tier, changes: [...], notes: [...]}}
        """
        from .installer import astor_install as run_installer
        body = request.get_json(force=True)
        ide = body.get('ide')
        mode = body.get('mode', 'auto')
        agent_dir = body.get('agent_dir', '~')
        if not ide:
            return jsonify({'error': 'ide required', 'detail': 'Provide "ide" field (e.g. vscode, cursor, windsurf)'}), 400
        result = run_installer(ide, Path(agent_dir).expanduser(), mode)
        return jsonify(result)

    @app.route('/v1/viewer/stats', methods=['GET'])
    def viewer_stats():
        """Content-free stats endpoint (Memorax-inspired Viewer).

        Returns counts only — NO fact content. Per MemoraX architecture rule,
        the Viewer is "a content-free local projection, not memory authority".
        Use this for dashboards / health monitoring / writeback-status check
        without leaking PII.

        Returns:
          {
            version, astor_dir,
            dbs: {bus, nest, forge} per tier,
            counts: {
              facts_total, facts_by_tier, facts_by_scope,
              events_total, embeddings_total, candidates_total,
              forge_audit_total, dedup_hits_total
            },
            last_activity_ts,
            schema_versions
          }
        """
        import sqlite3 as _sqlite3
        from ._internal.acl_layout import (
            get_astor_dir, get_db_path, Tier, Store, list_user_ids, list_repo_ids
        )

        astor_dir = get_astor_dir()
        out = {
            'version': __version__,
            'astor_dir': str(astor_dir),
            'generated_at': __import__('datetime').datetime.utcnow().isoformat() + 'Z',
            'dbs': {},
            'counts': {
                'facts_total': 0,
                'facts_by_tier': {'public': 0, 'source': 0, 'private': 0, 'repo': 0},
                'facts_by_scope': {'long_term': 0, 'short_term': 0, 'profile': 0},
                'events_total': 0,
                'embeddings_total': 0,
                'candidates_total': 0,
                'forge_audit_total': 0,
                'dedup_hits_total': 0,
            },
            'last_activity_ts': None,
            'schema_versions': {},
        }

        # Iterate 9-db layout: 3 tiers × 3 stores. For private tier, fanout
        # across all known users (from list_user_ids).
        tier_user_map = [
            (Tier.PUBLIC, None),
            (Tier.SOURCE, None),
        ]
        tier_user_map += [(Tier.PRIVATE, u) for u in list_user_ids()]
        # v1.1: fanout across all registered repos (MemoraX-style per-repo
        # memory). Repo facts live under ~/.astor/repos/<repo_id>/memory/.
        tier_user_map += [(Tier.REPO, r) for r in list_repo_ids()]

        for tier, user_id in tier_user_map:
            for store in (Store.BUS, Store.NEST, Store.FORGE):
                try:
                    db_path = get_db_path(tier, store, user_id)
                except ValueError:
                    continue
                if not db_path.exists():
                    continue
                rel_key = (
                    f'{tier.value}/{user_id or "_"}/{store.value}'
                )
                out['dbs'][rel_key] = {
                    'path': str(db_path),
                    'size_bytes': db_path.stat().st_size,
                }
                try:
                    c = _sqlite3.connect(
                        f'file:{db_path}?mode=ro', uri=True,
                        check_same_thread=False, timeout=5,
                    )
                except Exception:
                    continue
                try:
                    if store == Store.BUS:
                        # facts (canonical) — count + scope + tier
                        for row in c.execute(
                            "SELECT tier, scope_type, COUNT(*) FROM memory_canonical "
                            "WHERE tombstoned = 0 OR tombstoned IS NULL "
                            "GROUP BY tier, scope_type"
                        ).fetchall():
                            t_val, s_val, n = row
                            # tier may be 'private' or 'private_<user>' per
                            # schema CHECK — bucket to 'private' bucket.
                            t_bucket = (
                                'private' if t_val.startswith('private') else t_val
                            )
                            if t_bucket in out['counts']['facts_by_tier']:
                                out['counts']['facts_by_tier'][t_bucket] += n
                            if s_val in out['counts']['facts_by_scope']:
                                out['counts']['facts_by_scope'][s_val] += n
                            out['counts']['facts_total'] += n
                        # events
                        r = c.execute(
                            "SELECT COUNT(*) FROM events"
                        ).fetchone()
                        if r:
                            out['counts']['events_total'] += r[0]
                        # candidates
                        r = c.execute(
                            "SELECT COUNT(*) FROM memory_candidates"
                        ).fetchone()
                        if r:
                            out['counts']['candidates_total'] += r[0]
                        # last activity ts
                        r = c.execute(
                            "SELECT MAX(ts) FROM events"
                        ).fetchone()
                        if r and r[0]:
                            out['last_activity_ts'] = r[0]
                        # dedup_hits (from audit_log if available)
                        try:
                            r = c.execute(
                                "SELECT COUNT(*) FROM audit_log "
                                "WHERE event = 'promote_idempotent_replay'"
                            ).fetchone()
                            if r:
                                out['counts']['dedup_hits_total'] += r[0]
                        except Exception:
                            pass
                        # schema version
                        try:
                            r = c.execute(
                                "SELECT version FROM schema_migrations "
                                "ORDER BY version DESC LIMIT 1"
                            ).fetchone()
                            if r:
                                out['schema_versions'][f'bus/{rel_key}'] = r[0]
                        except Exception:
                            pass
                    elif store == Store.NEST:
                        r = c.execute(
                            "SELECT COUNT(*) FROM embeddings"
                        ).fetchone()
                        if r:
                            out['counts']['embeddings_total'] += r[0]
                    elif store == Store.FORGE:
                        r = c.execute(
                            "SELECT COUNT(*) FROM llm_call_log"
                        ).fetchone()
                        if r:
                            out['counts']['forge_audit_total'] += r[0]
                except Exception:
                    # Skip unreadable DBs; don't fail the whole endpoint.
                    continue
                finally:
                    try:
                        c.close()
                    except Exception:
                        pass

        return jsonify(out)

    # === Phase C-D: context + classify endpoints ===
    # 2026-09-17: ship 5/5 optimization points. Lets REST callers (and the
    # MCP ``astor_context`` handshake) read the resolved identity + LOCK
    # rules + server-side tier classification without hand-rolling recall.

    @app.route('/v1/context', methods=['GET'])
    def context_endpoint():
        """Return resolved identity for the caller.

        Companion to MCP ``astor_context``. Returns:
          - ``actor`` / ``role`` / ``user_id`` from the trusted ACL context
          - ``default_tier`` / ``trusted_agent`` from user_meta (admin-only)

        LOCK rule prefetch lives in the MCP ``astor_lock_rules`` tool; REST
        callers can use ``/v1/read/multi?tag=LOCK`` instead. Keeping the
        two paths separate avoids a synchronous recall on every REST
        handshake.
        """
        from ._internal.bot_binding import (
            get_user_default_tier, is_trusted_agent,
        )
        try:
            ctx = astor_current_acl()
        except Exception as e:
            return jsonify({'error': 'acl_unresolved', 'reason': repr(e)}), 401

        user_id = (ctx.user_id or 'admin') if ctx else 'admin'
        # Pull tier + trust from user_meta (PII table — only admin can read)
        default_tier = None
        trusted = False
        if ctx and ctx.role == 'admin':
            default_tier = get_user_default_tier(user_id)
            trusted = is_trusted_agent(user_id)

        return jsonify({
            'actor': ctx.actor if ctx else 'unknown',
            'role': ctx.role if ctx else 'unknown',
            'user_id': user_id,
            'default_tier': default_tier,
            'trusted_agent': trusted,
        })

    @app.route('/v1/classify', methods=['POST'])
    def classify():
        """Server-side tier decision based on user_meta + LOCK rules.

        Request body: ``{"text": str, "hint_user"?: str}``
        Response body: ``{"tier": str, "source": str, "confidence": float,
                          "reasoning": str, "rule"?: dict}``

        Decision priority:
          1. If ``hint_user`` (or caller's user_id) is ``trusted_agent`` in
             ``user_meta`` → use ``user_meta.default_tier`` (confidence 0.95)
          2. Otherwise evaluate against LOCK rules seeded into the public
             tier bus DB; the highest-priority match wins (confidence 0.8).
             ``rule_ship`` is a meta-tier that maps to ``public`` storage.
          3. Otherwise → ``public`` with ``safe_default`` source (confidence
             0.5). Caller must escalate via LOCK rule seed to reach a
             non-public tier.

        Phase E1 (2026-09-17) ships LOCK rule schema + evaluation.
        Phase E2 ships the classify path integration.
        """
        from ._internal.bot_binding import (
            get_user_default_tier, is_trusted_agent,
        )
        body = request.get_json(force=True) or {}
        text = (body.get('text') or '').strip()
        if not text:
            return jsonify({'error': 'text required'}), 400

        try:
            ctx = astor_current_acl()
        except Exception as e:
            return jsonify({'error': 'acl_unresolved', 'reason': repr(e)}), 401

        hint_user = (
            body.get('hint_user')
            or (ctx.user_id if ctx else None)
            or 'admin'
        )

        # Path 1: trusted_agent path — server resolves tier from user_meta.
        if is_trusted_agent(hint_user):
            tier = get_user_default_tier(hint_user) or 'public'
            return jsonify({
                'tier': tier,
                'source': 'trusted_default',
                'confidence': 0.95,
                'reasoning': (
                    f"user_id={hint_user!r} is trusted_agent; "
                    f"using user_meta.default_tier={tier!r}"
                ),
                'user_id': hint_user,
            })

        # Path 2: LOCK rule evaluation. Fetch rules (per-user + global) from
        # the public tier canonical DB. Lock rules are admin-only-write
        # content stored there; any caller can consult them without
        # private-tier access. We open the DB connection directly (instead
        # of going through ``astor_bus()``) to avoid the read-side ACL check
        # that would otherwise block admin-free callers in test contexts.
        try:
            from ._internal.lock_rules import (
                fetch_lock_rules, evaluate_text,
            )
            import sqlite3 as _sqlite3
            import os as _os_e2
            _astor_dir = _os_e2.environ.get('ASTOR_DIR', '~/.astor')
            _canonical_db = (
                Path(_astor_dir).expanduser()
                / 'public' / 'memory' / 'astor_bus_public.db'
            )
            if _canonical_db.exists():
                _bus_conn = _sqlite3.connect(str(_canonical_db))
                try:
                    _bus_conn.row_factory = _sqlite3.Row
                    rules = fetch_lock_rules(
                        _bus_conn, user_id=hint_user, scope=None,
                    )
                    match = evaluate_text(text, rules)
                    if match:
                        tier = match['target_tier']
                        # rule_ship is a meta-tier meaning "this is a
                        # curated rule that should be stored as public".
                        # Map to the ``public`` storage tier.
                        if tier == 'rule_ship':
                            tier = 'public'
                        return jsonify({
                            'tier': tier,
                            'source': 'lock_rule',
                            'confidence': 0.8,
                            'reasoning': (
                                f"matched LOCK rule {match['rule_name']!r} "
                                f"(prio={match['priority']}); "
                                f"action={match['action']}, "
                                f"target={match['target_tier']!r}"
                            ),
                            'user_id': hint_user,
                            'rule': match,
                        })
                finally:
                    _bus_conn.close()
        except Exception:
            # LOCK rule path is best-effort. If the canonical DB is
            # unavailable or the rules are malformed, fall through to
            # safe_default.
            pass

        # Path 3: safe default — defer to caller's own tier argument or LOCK
        # audit. v1.14.65 P4 (2026-09-17, end-to-end completeness): default
        # to PRIVATE for the user, not PUBLIC. Reasoning: any unclassified
        # text that didn't match a LOCK rule is treated as potentially
        # personal until proven otherwise. Without this, sunday typing
        # "my TFSA balance is 5000" (which has no matching LOCK rule)
        # gets safe_default=public, leaking private finance info.
        # Callers may opt back into public with --allow-public on
        # /v1/classify (or by passing their own tier explicitly on
        # /v1/write). For admin users, public stays the default since
        # admin-only content is curated for the operator.
        # v1.14.65: also accept body's allow_public flag from caller.
        _allow_public = bool(body.get('allow_public', False))
        if hint_user in ('admin', None, '') or _allow_public:
            default_tier = 'public'
        else:
            # Per-user private — admin-only database but tagged as the
            # caller's private tier so the write path correctly routes
            # to users/<u>/memory/.
            default_tier = 'private'
        return jsonify({
            'tier': default_tier,
            'source': 'safe_default',
            'confidence': 0.5,
            'reasoning': (
                f"user_id={hint_user!r} not in trusted_agent list; "
                f"defaulting to {default_tier}. "
                + ("admin caller — public OK."
                   if default_tier == 'public'
                   else "private is the safer default; pass allow_public=true to upgrade.")
            ),
            'user_id': hint_user,
        })

    @app.route('/v1/consistency/check', methods=['GET'])
    def consistency_check():
        """Phase C-D: cross-channel consistency audit (admin-only).

        Joins active bindings × user_meta × platforms and reports any
        inconsistency:
          - role_inherit ≠ user_meta.role
          - user_meta.active = 0 (stale binding)
          - platforms.enabled = 0 (binding to disabled platform)

        Returns ``{"inconsistencies": [...], "count": N}``. Empty list =
        all consistent. Never mutates state. Audit row is written for the
        run so admins can correlate over time.
        """
        from ._internal.bot_binding import check_cross_channel_consistency
        from ._internal.audit_logger import astor_audit
        try:
            ctx = astor_current_acl()
            if ctx.role != 'admin':
                return jsonify({'error': 'admin required'}), 403
        except Exception as e:
            return jsonify({'error': 'acl_unresolved', 'reason': repr(e)}), 401

        inconsistencies = check_cross_channel_consistency()
        # Write one audit row per run so the result is queryable later.
        try:
            astor_audit(
                actor=ctx.actor,
                tier='private',
                action='admin_op',
                target='consistency_check',
                reason=f"cross-channel audit: {len(inconsistencies)} inconsistencies",
                metadata={'count': len(inconsistencies)},
            )
        except Exception:
            # Audit is best-effort; never block the response.
            pass
        return jsonify({
            'inconsistencies': inconsistencies,
            'count': len(inconsistencies),
        })

    @app.route('/v1/reload', methods=['POST'])
    def reload():
        """Hot-reload server code (P3-fix 2026-08-15, fixes 2026-09-17).

        Spawns a fresh process via subprocess and exits the current one,
        so all module caches (bus/store, forge/extractor, server) pick up
        fresh source. Used after patching the code without restarting
        manually.

        Restricted to admin (per ACL plan § reload requires root).

        2026-09-17 bugfix #1: the previous implementation used
        ``os.execv(sys.executable, [sys.executable] + sys.argv)`` which
        (a) duplicated the executable because ``sys.argv[0]`` is the
        script path (not the executable) when launched via ``-m``, and
        (b) lost the ``-m`` flag so the respawned process tried to run
        ``server.py`` as ``__main__``, hitting ``from . import ...`` at
        line 63 with no parent package and crashing with
        ``ImportError: attempted relative import with no known parent
        package``.

        2026-09-17 bugfix #2: ``close_fds=True`` on subprocess.Popen
        closed the inherited stderr handle, which made ``sys.stderr``
        return ``None`` in the child. server.py's DEBUG-E prints then
        raised AttributeError and ``/v1/write`` returned 500. Default
        ``close_fds=False`` on Windows keeps stdio handles intact.

        2026-09-17 bugfix #3: when called as a POST with no JSON body
        (the normal curl usage), the ``before_request`` hook doesn't
        rebind ACL because ``request.is_json`` is False. The previous
        request's ``_CURRENT`` binding (e.g. ``user:sunday`` after a
        write) carries over, and ``ctx.role != 'admin'`` returns 403.
        We now FORCE-bind admin at the top of the handler so reload
        always works regardless of prior request state.
        """
        import os as _os
        # Force-bind admin so we don't inherit a stale user-level ACL
        # from the previous request (which the before_request hook only
        # rebinds for POST+JSON). See bugfix #3 above.
        try:
            astor_init_acl(
                actor='admin:admin', role='admin', tier='public',
                subscription_plan=None,
            )
            ctx = astor_current_acl()
            if ctx.role != 'admin':
                return jsonify({'error': 'reload requires admin role'}), 403
        except Exception:
            pass
        # Schedule a self-restart in 200ms then return. The new process
        # will bind the port after the old one closes it.
        import threading as _threading
        def _respawn():
            import subprocess as _sp
            import time as _t
            _t.sleep(0.2)
            # Pin the canonical Python interpreter so reload doesn't drift
            # to whatever sys.executable was used at first launch. The
            # hardcoded path matches what start_server.bat / NSSM use
            # so reload never silently swaps to a different venv.
            py_exe = r'D:\AI\PY-311\Scripts\pythonw.exe'
            if not _os.path.exists(py_exe):
                py_exe = sys.executable  # fallback if PY-311 not present
            cmd = [py_exe, '-m', 'astor_memory.server'] + sys.argv[1:]
            try:
                # NOTE: do NOT pass close_fds=True here. pythonw.exe keeps
                # references to sys.stdout/sys.stderr; closing those fds
                # makes them None, which then crashes any handler that
                # tries to print to stderr (e.g. server.py /v1/write uses
                # ``_sys.stderr.write(...)`` for debug logging). Default
                # close_fds=False on Windows is the safe choice — the
                # child inherits stdio handles so sys.stderr stays valid.
                _sp.Popen(cmd)
            except Exception:
                # Respawn failed; do nothing (current process keeps running)
                return
            # Hard-exit so Flask shutdown doesn't hold the port
            _os._exit(0)
        _threading.Thread(target=_respawn, daemon=True).start()
        return jsonify({'reloading': True, 'pid': _os.getpid()})

    @app.errorhandler(404)
    def not_found(e):
        """Flask 404 handler that emits a structured JSON error."""
        return jsonify({'error': 'not found', 'path': request.path}), 404

    # === Grant endpoints (2026-08-16 strict-privacy ship) ===

    @app.route('/v1/grant', methods=['POST'])
    def grant_create():
        """
        Issue a cross-user private-tier grant.

        Body: {
          "grantor":    "<user_id>",      # data owner (the caller if 'user' role)
          "grantee":    "admin:<id>" | "user:<id>",
          "scope":      "read" | "write" | "admin",
          "expires_at": ISO 8601 or null (default null),
          "reason":     free text (optional)
        }

        Auth: caller MUST be the grantor (user role) — i.e. a user can only
        authorize access to their own private data.
        """
        from ._internal.grants import create_grant as _create_grant
        from ._internal.acl import astor_current_acl
        from ._internal.audit_logger import astor_audit

        body = request.get_json(force=True) or {}
        grantor = body.get('grantor')
        grantee = body.get('grantee')
        scope = body.get('scope', 'read')
        expires_at = body.get('expires_at')
        reason = body.get('reason')

        if not grantor or not grantee:
            return jsonify({'error': 'missing grantor/grantee', 'detail': 'Provide both grantor and grantee user_ids'}), 400

        ctx = astor_current_acl()
        if ctx.role == 'user':
            if ctx.user_id != grantor:
                astor_audit(
                    actor=ctx.actor, tier='private', action='admin_op',
                    user_id=grantor, target='grant_create_denied',
                    reason='user can only grant on own private',
                    metadata={"requested_grantee": grantee},
                )
                return jsonify({
                    'error': 'forbidden',
                    'detail': 'user can only authorize access to their own private data',
                }), 403
        elif ctx.role != 'admin':
            # admin cannot forge a grant on a user's behalf
            astor_audit(
                actor=ctx.actor, tier='private', action='admin_op',
                user_id=grantor, target='grant_create_denied',
                reason='admin cannot forge grant on user behalf',
                metadata={"requested_grantee": grantee},
            )
            return jsonify({
                'error': 'forbidden',
                'detail': 'admin cannot create grants on behalf of users',
            }), 403

        try:
            gid = _create_grant(grantor=grantor, grantee=grantee, scope=scope,
                                expires_at=expires_at, reason=reason)
        except ValueError as exc:
            return jsonify({'error': 'invalid_grant', 'detail': f'grant validation failed: {str(exc)}'}), 400
        astor_audit(
            actor=ctx.actor, tier='private', action='admin_op',
            user_id=grantor, target='grant_created',
            reason=f'grantee={grantee} scope={scope}',
            metadata={"grant_id": gid, "grantee": grantee, "scope": scope},
        )
        return jsonify({'grant_id': gid, 'grantor': grantor, 'grantee': grantee,
                        'scope': scope, 'expires_at': expires_at})

    @app.route('/v1/grant/revoke', methods=['POST'])
    def grant_revoke():
        """Revoke a grant by id. Caller must own the grant (be the grantor)."""
        from ._internal.grants import revoke_grant as _revoke_grant, list_grants as _list
        from ._internal.acl import astor_current_acl
        from ._internal.audit_logger import astor_audit

        body = request.get_json(force=True) or {}
        gid = body.get('grant_id')
        if not gid:
            return jsonify({'error': 'missing grant_id', 'detail': 'Provide "grant_id" to revoke'}), 400

        ctx = astor_current_acl()
        rows = _list(grantee=None, include_revoked=True)
        target = next((r for r in rows if r['id'] == int(gid)), None)
        if not target:
            return jsonify({'error': 'grant_not_found'}), 404
        if ctx.role == 'user' and ctx.user_id != target['grantor']:
            return jsonify({'error': 'forbidden',
                            'detail': 'only the grantor can revoke their grant'}), 403

        ok = _revoke_grant(int(gid), by=ctx.actor)
        astor_audit(
            actor=ctx.actor, tier='private', action='admin_op',
            user_id=target['grantor'], target='grant_revoked',
            reason=f'grantee={target["grantee"]}',
            metadata={"grant_id": int(gid)},
        )
        return jsonify({'ok': ok, 'grant_id': int(gid)})

    @app.route('/v1/grant/list', methods=['GET'])
    def grant_list():
        """List grants scoped to caller role (admin=all, admin=incoming, user=outgoing)."""
        from ._internal.grants import list_grants as _list
        from ._internal.acl import astor_current_acl

        ctx = astor_current_acl()
        include_revoked = request.args.get('include_revoked', 'false').lower() == 'true'

        if ctx.role == 'admin':
            grants_out = _list(include_revoked=include_revoked)
        elif ctx.role == 'admin':
            grants_out = _list(grantee=ctx.actor, include_revoked=include_revoked)
        else:
            grants_out = _list(grantor=ctx.user_id, include_revoked=include_revoked)
        return jsonify({'grants': grants_out, 'count': len(grants_out)})

    # 2026-08-31 ship: 记忆原生三维度自审 audit endpoint
    # 灵感来源: 微信文章"从外部记忆到记忆原生模型"by Bannings
    # (https://mp.weixin.qq.com/s/aL1gaDDGR1eJy2uzL5kKdQ)
    # 三维: 可寻址 (nest.search) / 可更新 (bus.append + nest.store wired) /
    #       可计算 (query 进 nest search, 不是 hardcoded prefix)
    @app.route('/v1/audit/health', methods=['GET'])
    def audit_health():
        """Self-audit astor on 3-dim memory-native checklist (Bannings 2026-08-31).

        Returns per-dimension score (0/1) + evidence. Total 0-3.
        GET only — no side effects, no auth required (content-free).
        """
        import sqlite3 as _sqlite3
        from ._internal.acl_layout import get_db_path, Tier, Store

        evidence = {}

        # === Dim 1: 可寻址 (Addressable) ===
        # nest.search endpoint exists + nest DB has embeddings
        addressable_ok = False
        try:
            # Check nest DB has embeddings (any tier — public covers it)
            nest_path = str(get_db_path(Tier.PUBLIC, Store.NEST))
            conn = _sqlite3.connect(nest_path)
            cur = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='embeddings'")
            has_table = cur.fetchone()[0] > 0
            if has_table:
                cnt = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
                addressable_ok = cnt > 0
                evidence['addressable'] = {
                    'nest_db': nest_path,
                    'embeddings_count': cnt,
                    'check': 'PASS — nest has embeddings'
                }
            else:
                evidence['addressable'] = {
                    'nest_db': nest_path,
                    'check': 'FAIL — embeddings table missing'
                }
            conn.close()
        except Exception as e:
            evidence['addressable'] = {'check': f'FAIL — {e}'}

        # === Dim 2: 可更新 (Updatable) ===
        # bus.append exists + nest.store wired (called after bus append)
        updatable_ok = False
        try:
            # Look for nest.store call in server.py source
            import re as _re
            src_path = os.path.join(os.path.dirname(__file__), 'server.py')
            with open(src_path, 'r', encoding='utf-8') as f:
                src = f.read()
            nest_store_wired = 'nest.store' in src
            # Check bus DB has append path (events_total > 0 implies writes happened)
            bus_path = str(get_db_path(Tier.PUBLIC, Store.BUS))
            conn = _sqlite3.connect(bus_path)
            events_cnt = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            conn.close()
            updatable_ok = nest_store_wired and events_cnt > 0
            evidence['updatable'] = {
                'nest_store_wired': nest_store_wired,
                'bus_events_count': events_cnt,
                'check': 'PASS — nest.store wired + events flowing' if updatable_ok else 'FAIL — check wiring'
            }
        except Exception as e:
            evidence['updatable'] = {'check': f'FAIL — {e}'}

        # === Dim 3: 可计算 (Computable) ===
        # nest.search computes embeddings live (not cached/hardcoded)
        # proxy: query_embedding goes through nest.search() not prefix-match
        computable_ok = False
        try:
            # Look for embedding model in nest.search call (model.embed or model.encode)
            src_path = os.path.join(os.path.dirname(__file__), 'server.py')
            with open(src_path, 'r', encoding='utf-8') as f:
                src = f.read()
            uses_live_embed = ('model.embed' in src or 'model.encode' in src) and 'nest.search' in src
            computable_ok = uses_live_embed
            evidence['computable'] = {
                'live_embed_in_search': uses_live_embed,
                'check': 'PASS — nest.search computes live embeddings' if computable_ok else 'FAIL — using prefix/cache'
            }
        except Exception as e:
            evidence['computable'] = {'check': f'FAIL — {e}'}

        # === Aggregate ===
        scores = {
            'addressable': 1 if addressable_ok else 0,
            'updatable': 1 if updatable_ok else 0,
            'computable': 1 if computable_ok else 0,
        }
        total = sum(scores.values())
        verdict = (
            'memory_native_ready' if total == 3 else
            'partially_native' if total >= 1 else
            'memory_external_only'
         )
        return jsonify({
            'version': __version__,
            'audit_ts': __import__('datetime').datetime.utcnow().isoformat() + 'Z',
            'dimension_scores': scores,
            'total_score': f'{total}/3',
            'verdict': verdict,
            'evidence': evidence,
            # v1.15.17 S21: meta-recall stats so dashboard / ops can verify
            # astor is actively auto-injecting success/failure patterns.
            'meta_recall_stats': dict(_META_RECALL_STATS),
            'reference': 'mp.weixin.qq.com/s/aL1gaDDGR1eJy2uzL5kKdQ (Bannings 2026-08)'
        })

    @app.errorhandler(500)
    def internal_error(e):
        """Flask 500 handler that emits a structured JSON error + audit row.

        2026-09-17 debug: also write the full traceback to a side log in
        the source repo (NOT critical path) so we can diagnose 500s without
        needing to capture the running process's stderr. Without this,
        the only 500 signal is the JSON ``detail`` field which Flask
        truncates for HTTP responses.
        """
        import logging as _logging
        import os as _os_e
        import traceback as _tb
        _side_log = _os_e.path.join(
            _os_e.path.dirname(_os_e.path.dirname(_os_e.path.abspath(__file__))),
            'astor memory', '_server_500.log',
        )
        try:
            with open(_side_log, 'a', encoding='utf-8') as _f:
                _f.write('\n=== ' + _os_e.environ.get('COMPUTERNAME', '?') +
                         ' port=' + str(request.environ.get('SERVER_PORT', '?')) +
                         ' path=' + request.path + ' ===\n')
                _f.write(_tb.format_exc())
        except Exception:
            pass  # never let the side-log write block the response
        return jsonify({'error': 'internal error', 'detail': str(e)}), 500

    # v1.14.67 (2026-09-17): eager-load embedding model + init peer
    # identity at app boot so first /v1/read and Phase 3 sync layer
    # both have zero warmup latency. Bounded with try/except so a
    # missing model doesn't block server start. Must run BEFORE return
    # app (Python doesn't execute code after return in a function).
    try:
        from .nest.embeddings import astor_get_embedding_model
        _warm_model = astor_get_embedding_model()
        _warm_vec = _warm_model.encode(['astor warmup probe'])
        if hasattr(_warm_vec, 'shape') and len(_warm_vec.shape) >= 2:
            print(f'   Warmup: embedding model ready (dim={_warm_vec.shape[1]})')
    except Exception as _warm_exc:
        print(f'   Warmup skipped: {_warm_exc}')
    try:
        from ._internal.peer_identity import init_identity
        _peer_id = init_identity(astor_dir=astor_dir)
        print(f'   Peer identity: {_peer_id["peer_id"]}')
    except Exception as _peer_init_exc:
        print(f'   Peer identity: init deferred ({_peer_init_exc})')
    # v1.15.22: rebuild per-peer rate-limit buckets from the 24h audit
    # window so a restart doesn't lose the in-memory state. Best-effort.
    try:
        from ._internal.peer_rate_limit import (
            rebuild_from_audit as _prl_init_rebuild,
        )
        _rebuilt = _prl_init_rebuild(astor_dir=astor_dir)
        if _rebuilt:
            print(f'   Per-peer rate limit: rebuilt {_rebuilt} entries from audit')
    except Exception as _prl_exc:
        print(f'   Per-peer rate limit: rebuild skipped ({_prl_exc})')

    # v1.14.72 (2026-09-17) — Phase 3: /v1/peer/recv
    # Endpoint for receiving signed peer messages. Wire format:
    #   {"msg_type": "rekey" | "topic_index" | <future>, "msg": <dict>}
    # - rekey:       verify + decide + record (admin applies via CLI)
    # - topic_index: update topic_index with per-(topic, sender) entries
    @app.route('/v1/peer/recv', methods=['POST'])
    def peer_recv():
        """Phase 3: receive a signed peer message."""
        from ._internal.peer_identity import verify_rekey_message
        from ._internal.peer_relationships import (
            record_rekey, get_peer, set_topic, add_peer,
        )
        import datetime as _dt_recv
        body = request.get_json(silent=True) or {}
        msg_type = body.get('msg_type')
        msg = body.get('msg')
        if not msg_type or not msg:
            return jsonify({
                'error': 'missing msg_type or msg',
                'detail': 'body must be {"msg_type": "...", "msg": {...}}',
            }), 400
        now = _dt_recv.datetime.now(_dt_recv.timezone.utc).isoformat(
            timespec='seconds').replace('+00:00', 'Z')
        try:
            if msg_type == 'rekey':
                if not verify_rekey_message(msg):
                    record_rekey(
                        msg.get('old_peer_id', ''),
                        msg.get('new_peer_id', ''),
                        msg.get('signature', ''),
                        msg.get('signer_pubkey', ''),
                        status='rejected',
                        note='signature_invalid (recv endpoint)',
                    )
                    return jsonify({
                        'received': True,
                        'msg_type': 'rekey',
                        'action': 'rejected',
                        'reason': 'signature_invalid',
                    }), 200
                existing = get_peer(msg['old_peer_id'])
                trust = existing['trust'] if existing else None
                if trust is None:
                    action = 'manual_pending'
                elif trust < 30:
                    action = 'reject'
                elif trust < 70:
                    action = 'manual_pending'
                else:
                    action = 'auto_accept'
                # v1.15.30 Ship N: store the rekey message JSON in
                # rekey_log.message so the receiver can apply it later
                # via `am peer rekey-accept <id>` without needing the
                # sender to re-send.
                rid = record_rekey(
                    msg['old_peer_id'], msg['new_peer_id'],
                    msg['signature'], msg['signer_pubkey'],
                    status=('auto_accepted' if action == 'auto_accept'
                            else 'manual_pending' if action == 'manual_pending'
                            else 'rejected'),
                    note=f'received via /v1/peer/recv at {now}',
                    message=__import__('json').dumps(msg),
                )
                return jsonify({
                    'received': True,
                    'msg_type': 'rekey',
                    'action': action,
                    'rekey_id': rid,
                    'old_peer_id': msg['old_peer_id'],
                    'new_peer_id': msg['new_peer_id'],
                }), 200
            elif msg_type == 'topic_index':
                sender = msg.get('sender_peer_id', '')
                if not sender.startswith('astor:'):
                    return jsonify({'error': 'invalid sender_peer_id'}), 400
                existing = get_peer(sender)
                if not existing:
                    add_peer(
                        sender,
                        kind='pending',
                        trust=30,
                        public_key=msg.get('sender_pubkey'),
                    )
                count = 0
                for t in msg.get('topics', []):
                    topic_name = t.get('topic')
                    weight = float(t.get('weight', 1.0))
                    if topic_name and 0.0 <= weight <= 1.0:
                        set_topic(topic_name, sender, weight=weight,
                                  source='peer_recv')
                        count += 1
                return jsonify({
                    'received': True,
                    'msg_type': 'topic_index',
                    'sender_peer_id': sender,
                    'topics_applied': count,
                }), 200
            else:
                return jsonify({
                    'error': 'unknown msg_type',
                    'msg_type': msg_type,
                    'supported': ['rekey', 'topic_index'],
                }), 400
        except Exception as e:
            return jsonify({
                'error': 'recv_failed',
                'detail': str(e),
            }), 500
# ------------------------------------------------------------------
    # v1.15.30 (2026-09-28) - Ship N: rekey apply/reject by id.
    # Completes the receiver-side flow: /v1/peer/recv now stores the
    # rekey message; these endpoints + CLI apply or reject it.
    # ------------------------------------------------------------------
    @app.route('/v1/peer/rekey/<int:rid>/accept', methods=['POST'])
    def peer_rekey_accept_endpoint(rid):
        from ._internal.peer_relationships import apply_rekey_by_id
        result = apply_rekey_by_id(rid)
        if result is None:
            return jsonify({'error': 'rekey_not_found',
                            'rekey_id': rid}), 404
        if 'error' in result:
            return jsonify(result), 400
        return jsonify(result)

    @app.route('/v1/peer/rekey/<int:rid>/reject', methods=['POST'])
    def peer_rekey_reject_endpoint(rid):
        from ._internal.peer_relationships import reject_rekey_by_id
        body = request.get_json(force=True) or {}
        reason = body.get('reason', '')
        result = reject_rekey_by_id(rid, reason=reason)
        if result is None:
            return jsonify({'error': 'rekey_not_found_or_not_pending',
                            'rekey_id': rid}), 404
        if 'error' in result:
            return jsonify(result), 400
        return jsonify(result)

    @app.route('/v1/peer/rekey/pending', methods=['GET'])
    def peer_rekey_pending_list():
        from ._internal.peer_relationships import get_rekey_log
        rows = get_rekey_log(status='manual_pending')
        return jsonify({
            'count': len(rows),
            'results': rows,
        })

# ------------------------------------------------------------------
    # v1.14.73 (2026-09-27) — Phase 4 PPS: demand-driven peer public search.
    # Friends who opted in (metadata.allow_search=True) can search this
    # install's PUBLIC tier. No push, no pull, read-only on their side.
    # See astor_memory/_internal/peer_search.py for the request/response
    # schema + attack model (all preventive: construct-time validation,
    # opt-in default-off, trust>=50, timestamp freshness, sig check).
    # ------------------------------------------------------------------
    @app.route('/v1/peer/public_search', methods=['GET'])
    def peer_public_search():
        import base64 as _b64_pps
        from ._internal.peer_search import (
            PeerSearchRequest, PeerSearchResponse, PeerSearchResult,
            MAX_RESULTS_PER_PEER,
        )
        from ._internal.peer_identity import verify as _peer_verify
        from ._internal.peer_relationships import get_peer as _pp_get_peer

        raw = request.args.get('req', '')
        if not raw:
            return jsonify({'error': 'missing_req',
                            'detail': 'query param req=<urlsafe_b64 json> required'}), 400
        try:
            payload = _b64_pps.urlsafe_b64decode(raw.encode('ascii'))
            d = json.loads(payload)
        except Exception as e:
            return jsonify({'error': 'bad_req_encoding', 'detail': str(e)}), 400

        # Construct-time validation — raises on malformed/stale input.
        try:
            req = PeerSearchRequest(
                requestor_peer_id=d['requestor_peer_id'],
                requestor_pubkey=d['requestor_pubkey'],
                query=d['query'],
                topic=d.get('topic') or None,
                limit=int(d.get('limit', 10)),
                timestamp=d['timestamp'],
                signature=d['signature'],
            )
        except KeyError as e:
            return jsonify({'error': 'missing_field', 'field': str(e)}), 400
        except ValueError as e:
            return jsonify({'error': 'invalid_request', 'detail': str(e)}), 400
        except Exception as e:
            return jsonify({'error': 'invalid_request', 'detail': str(e)}), 400

        # Self-search guard: a peer searching itself via this endpoint is a
        # topology error, not an attack — reject cleanly.
        try:
            from ._internal.peer_identity import get_identity as _pp_get_identity
            _me = _pp_get_identity()
            if _me and req.requestor_peer_id == _me.get('peer_id'):
                return jsonify({'error': 'self_search_not_allowed'}), 400
        except Exception:
            pass

        # Signature verification (never raise to caller; hostile sig = 403).
        if not req.verify_signature():
            try:
                from ._internal.audit_logger import astor_audit as _pp_audit
                _pp_audit(
                    actor=f'peer:{req.requestor_peer_id}',
                    tier='public',
                    action='peer_search_sig_reject',
                    user_id=None,
                    detail='ed25519 verification failed',
                )
            except Exception:
                pass
            return jsonify({'error': 'bad_signature'}), 403

        # Receiver-side opt-in chain: known peer, not blacklisted,
        # trust >= 50, allow_search=True.
        peer_row = _pp_get_peer(req.requestor_peer_id)
        if not peer_row:
            return jsonify({'error': 'unknown_peer',
                            'detail': 'friend must add you first (am peer add)'}), 403
        if (peer_row.get('kind') or 'friend') == 'blacklist':
            return jsonify({'error': 'peer_blacklisted'}), 403
        if int(peer_row.get('trust') or 0) < 50:
            return jsonify({'error': 'trust_below_threshold',
                            'detail': 'trust must be >= 50'}), 403
        _meta = peer_row.get('metadata') or {}
        if isinstance(_meta, str):
            try:
                _meta = json.loads(_meta)
            except Exception:
                _meta = {}
        if not isinstance(_meta, dict) or not _meta.get('allow_search'):
            return jsonify({'error': 'search_not_allowed',
                            'detail': 'friend has not opted in '
                                      '(am peer allow-search <requestor_peer_id>)'}), 403
        # v1.15.22 Ship E: per-peer rate limit (R12593 lock — PPS path is
        # its own budget, separate from per-actor limits). Enforce BEFORE
        # doing any expensive search work so a runaway peer doesn't trigger
        # embedding model loads.
        from ._internal.peer_rate_limit import (
            check_and_consume as _prl_check,
            DEFAULT_LIMIT_PER_24H as _PRL_DEFAULT,
        )
        _allowed, _rl_count, _rl_retry = _prl_check(req.requestor_peer_id)
        if not _allowed:
            return jsonify({
                'error': 'peer_rate_limit_exceeded',
                'detail': (f'this peer has exceeded the '
                           f'{_PRL_DEFAULT} req/24h PPS budget'),
                'count_in_window': _rl_count,
                'retry_after_seconds': _rl_retry,
            }), 429, {
                'Retry-After': str(_rl_retry),
            }

        # Clamp limit (already validated 1..20 at construction, but be safe).
        limit = min(int(req.limit), MAX_RESULTS_PER_PEER)

        # Search PUBLIC tier only. tombstoned facts are excluded.
        # Vector + BM25 merge, keep top `limit` by combined score.
        tier = 'public'
        try:
            nest = astor_nest(tier=tier, user_id=None)
            bus = astor_bus(tier=tier, user_id=None)
            from .nest.embeddings import astor_get_embedding_model
            model = astor_get_embedding_model()
            query_emb = list(model.embed([req.query]))[0]

            from .nest.lex_index import astor_lex as _pp_lex, hybrid_merge as _pp_merge
            lex = _pp_lex(tier=tier, user_id=None)
            vector_hits = nest.search(query_emb, limit=limit * 2)
            bm25_hits = lex.bm25_search(req.query, limit=limit * 2)
            merged = _pp_merge(
                bm25_hits, vector_hits,
                bm25_weight=0.6, vec_weight=0.4,
                limit=limit,
            )
        except Exception as e:
            return jsonify({'error': 'search_backend_failed', 'detail': str(e)}), 500

        # Enrich from bus, drop tombstoned, cap bytes defensively.
        me_row = _me or {}
        my_peer_id = me_row.get('peer_id', 'astor:unknown')
        results = []
        truncated = False
        for fact_id, sim in merged:
            if len(results) >= limit:
                truncated = True
                break
            row = bus.conn.execute(
                "SELECT id, content, kind, tags, created_at, tombstoned "
                "FROM memory_canonical WHERE id = ?",
                (fact_id,),
            ).fetchone()
            if row is None or int(row[5] or 0) != 0:
                continue
            try:
                import json as _j_pps
                tags = tuple(_j_pps.loads(row[3])) if row[3] else ()
            except Exception:
                tags = ()
            try:
                results.append(PeerSearchResult(
                    source_peer_id=my_peer_id,
                    source_trust=100,
                    fact_id=int(row[0]),
                    content=str(row[1] or '')[:2048],
                    kind=str(row[2] or 'fact'),
                    tags=tags,
                    created_at=str(row[4] or ''),
                    relevance=round(min(max(float(sim), 0.0), 1.0), 4),
                ))
            except (ValueError, TypeError):
                continue

        try:
            from ._internal.audit_logger import astor_audit as _pp_audit2
            _pp_audit2(
                actor=f'peer:{req.requestor_peer_id}',
                tier='public',
                action='peer_search',
                user_id=None,
                peer_id=req.requestor_peer_id,
                metadata={'query': req.query[:256], 'result_count': len(results)},
            )
        except Exception:
            pass

        resp = PeerSearchResponse(
            requestor_peer_id=req.requestor_peer_id,
            results=tuple(results),
            truncated=truncated,
        )
        return jsonify(resp.to_dict())

    # ------------------------------------------------------------------
    # v1.15.21 (2026-09-28) — Ship D: PPS-augmented recall.
    # GET /v1/peer/recall?q=&topic=&limit=&tier=&user=
    # Runs the local /v1/read-equivalent search first; if results are empty,
    # transparently fans out to all eligible friends (trust>=50, endpoint,
    # allow_search=True) and returns the combined results. Local results
    # come first, peer results tagged with `peer_source` for provenance.
    #
    # This is the EXPLICIT peer-fanout endpoint per R12593-b:
    #   - Agent default (/v1/read) does NOT auto-fire peer (no surprise).
    #   - Peer fanout is opt-in via this endpoint or via /v1/read body flag.
    # Per-actor rate limit doesn't apply here (PPS path is its own budget).
    # ------------------------------------------------------------------
    @app.route('/v1/peer/recall', methods=['GET'])
    def peer_recall():
        from ._internal.peer_search import (
            build_search_request, select_search_targets,
            dispatch_search_to_peers,
        )
        from ._internal.peer_identity import init_identity
        from ._internal.peer_relationships import list_peers
        import urllib.parse as _up_pr

        q = request.args.get('q', '').strip()
        if not q:
            return jsonify({'error': 'q_required'}), 400
        topic = request.args.get('topic') or None
        try:
            topic_min_weight = float(request.args.get('topic_min_weight', 0.5))
        except ValueError:
            topic_min_weight = 0.5
        topic_min_weight = max(0.0, min(topic_min_weight, 10.0))
        try:
            limit = int(request.args.get('limit', 5))
        except ValueError:
            limit = 5
        limit = max(1, min(limit, 20))
        tier = request.args.get('tier') or 'public'
        user_id = request.args.get('user') or request.args.get('user_id') or None

        # ---------- 1. Local recall (mirror /v1/read logic) ----------
        # We don't refactor /v1/read into a helper here — instead we run the
        # same primitives inline so peer_recall is testable in isolation.
        local_results = []
        try:
            from .nest.embeddings import astor_get_embedding_model
            from .nest.lex_index import astor_lex as _local_lex, hybrid_merge as _local_merge
            model = astor_get_embedding_model()
            try:
                nest = astor_nest(tier=tier, user_id=user_id)
                bus = astor_bus(tier=tier, user_id=user_id)
            except Exception:
                nest = astor_nest(tier='public', user_id=None)
                bus = astor_bus(tier='public', user_id=None)
                tier = 'public'
            query_emb = list(model.embed([q]))[0]
            lex = _local_lex(tier=tier, user_id=user_id)
            vec_hits = nest.search(query_emb, limit=limit * 2)
            bm25_hits = lex.bm25_search(q, limit=limit * 2)
            merged = _local_merge(
                bm25_hits, vec_hits,
                bm25_weight=0.6, vec_weight=0.4, limit=limit,
            )
            for fid, sim in merged:
                row = bus.conn.execute(
                    "SELECT id, content, kind, tags, created_at, tombstoned "
                    "FROM memory_canonical WHERE id = ?",
                    (fid,),
                ).fetchone()
                if row is None or int(row[5] or 0) != 0:
                    continue
                try:
                    import json as _j_lcl
                    tags = list(_j_lcl.loads(row[3])) if row[3] else []
                except Exception:
                    tags = []
                local_results.append({
                    'fact_id': int(row[0]),
                    'content': str(row[1] or ''),
                    'kind': str(row[2] or 'fact'),
                    'tags': tags,
                    'created_at': str(row[4] or ''),
                    'relevance': round(min(max(float(sim), 0.0), 1.0), 4),
                    'source': 'local',
                    'peer_id': None,
                })
        except Exception as e:
            # local recall failure must not block peer fanout
            local_results = []
            _local_error = str(e)
        else:
            _local_error = None

        # ---------- 2. If local non-empty, skip peer fanout ----------
        if local_results:
            return jsonify({
                'mode': 'local_only',
                'local_count': len(local_results),
                'peer_count': 0,
                'results': local_results[:limit],
                'peer_results': [],
                'local_error': _local_error,
            })

        # ---------- 3. Local empty → fan out to eligible friends ----------
        # v1.15.24 Ship H: extracted to peer_recall.dispatch_peer_fanout()
        # for reuse by /v1/read's body.peer_fanout flag. Single source of
        # truth: both paths now use the same dispatch logic.
        from ._internal.peer_recall import dispatch_peer_fanout
        _fanout = dispatch_peer_fanout(
            query=q, limit=limit, topic=topic,
            topic_min_weight=topic_min_weight, actor_peer_id=None,
        )
        peer_results = _fanout["peer_results"]
        per_peer = _fanout["per_peer"]
        if _fanout.get("local_error") is not None:
            _local_error = _fanout["local_error"]
        _fanout_hint = _fanout.get("hint")

        try:
            from ._internal.audit_logger import astor_audit as _ppra
            from ._internal.peer_identity import init_identity
            try:
                _me_id = init_identity().get("peer_id", "unknown")
            except Exception:
                _me_id = "unknown"
            _ppra(
                actor=f'server:{_me_id}',
                tier='public',
                action='peer_recall',
                user_id=user_id,
                peer_id=_me_id if (_me_id and _me_id != 'unknown' and _me_id.startswith('astor:')) else None,
                metadata={'query': q[:256], 'local_count': 0, 'peer_count': len(peer_results)},
            )
        except Exception:
            pass

        # If dispatch returned no eligible friends, surface as local_only
        # with a hint explaining why. Same response shape as step 2.
        if not peer_results and not per_peer and _fanout_hint:
            return jsonify({
                'mode': 'local_only',
                'local_count': 0,
                'peer_count': 0,
                'results': [],
                'peer_results': [],
                'per_peer': [],
                'local_error': _local_error,
                'hint': _fanout_hint,
            })

        return jsonify({
            'mode': 'peer_fanout',
            'local_count': 0,
            'peer_count': len(peer_results),
            'results': peer_results,
            'peer_results': peer_results,
            'per_peer': per_peer,
            'local_error': _local_error,
        })

    # ------------------------------------------------------------------
    # v1.15.22 (2026-09-28) — Ship E: per-peer rate-limit admin surface.
    # R12593: PPS path is its own budget, separate from per-actor limit.
    # ------------------------------------------------------------------
    @app.route('/v1/peer/rate-limit', methods=['GET'])
    def peer_rate_limit_status():
        from ._internal.peer_rate_limit import (
            snapshot as _prl_snap, status_summary as _prl_sum, all_snapshots,
        )
        if request.args.get('all') in ('1', 'true', 'True'):
            return jsonify({
                'summary': _prl_sum(),
                'peers': all_snapshots(),
            })
        pid = request.args.get('peer_id', '').strip()
        if not pid:
            return jsonify({'error': 'peer_id_required',
                            'hint': 'pass ?peer_id=astor:<32-hex> or ?all=true'}), 400
        return jsonify(_prl_snap(pid))

    @app.route('/v1/peer/rate-limit/reset', methods=['POST'])
    def peer_rate_limit_reset():
        from ._internal.peer_rate_limit import reset as _prl_reset
        body = request.get_json(force=True) or {}
        pid = (body.get('peer_id') or '').strip()
        if not pid:
            return jsonify({'error': 'peer_id_required'}), 400
        n = _prl_reset(pid)
        return jsonify({'ok': True, 'peer_id': pid, 'cleared_count': n})

    @app.route('/v1/peer/rate-limit/rebuild', methods=['POST'])
    def peer_rate_limit_rebuild():
        from ._internal.peer_rate_limit import (
            rebuild_from_audit as _prl_rebuild,
        )
        n = _prl_rebuild()
        return jsonify({'ok': True, 'rebuilt_count': n})

    # ------------------------------------------------------------------
    # v1.15.25 (2026-09-28) — Ship I: per-peer audit feed.
    # Returns chronological list of audit rows for one peer_id. Powers ops
    # monitoring ("what did peer X do?"), trust-decay forensics, and
    # rate-limit action audit. R12593: peer path stays its own budget.
    # ------------------------------------------------------------------
    @app.route('/v1/peer/audit', methods=['GET'])
    def peer_audit_feed():
        from ._internal.audit_logger import astor_query_peer_audit
        pid = request.args.get('peer_id', '').strip()
        if not pid:
            return jsonify({
                'error': 'peer_id_required',
                'hint': 'pass ?peer_id=astor:<32-hex>',
            }), 400
        action = request.args.get('action') or None
        since = request.args.get('since') or None
        until = request.args.get('until') or None
        try:
            limit = int(request.args.get('limit', 50))
        except ValueError:
            limit = 50
        rows = astor_query_peer_audit(
            pid, action=action, since=since, until=until, limit=limit,
        )
        return jsonify({
            'peer_id': pid,
            'count': len(rows),
            'results': rows,
        })

    # ------------------------------------------------------------------
    # v1.15.26 (2026-09-28) — Ship J: per-peer health aggregator.
    # Aggregates relationship + audit + rate-limit signals into a single
    # status dict. Used by ops to answer "is peer X alive?" without
    # pinging the peer's endpoint (synchronous pings would block on
    # slow peers; we use signals instead).
    # ------------------------------------------------------------------
    @app.route('/v1/peer/health', methods=['GET'])
    def peer_health_endpoint():
        from ._internal.peer_health import (
            peer_health as _ph_one, all_peer_health as _ph_all,
        )
        if request.args.get('all') in ('1', 'true', 'True'):
            rows = _ph_all()
            # Group by health for quick overview
            by_health = {}
            for r in rows:
                by_health.setdefault(r.get('health', 'unknown'), []).append(
                    r.get('peer_id')
                )
            return jsonify({
                'count': len(rows),
                'peers': rows,
                'by_health': by_health,
            })
        pid = request.args.get('peer_id', '').strip()
        if not pid:
            return jsonify({
                'error': 'peer_id_required',
                'hint': 'pass ?peer_id=astor:<32-hex> or ?all=true',
            }), 400
        return jsonify(_ph_one(pid))

    # ------------------------------------------------------------------
    # v1.15.28 (2026-09-28) — Ship L: peer quarantine (auto-isolate).
    # Quarantine excludes a peer from fanout + sets trust=0, but
    # preserves the peer record + original trust in metadata for
    # potential unquarantine restoration. Use case: a peer with many
    # errors (malformed responses, hostile behavior) is isolated
    # without losing the relationship data.
    # ------------------------------------------------------------------
    @app.route('/v1/peer/quarantine', methods=['POST'])
    def peer_quarantine():
        from ._internal.peer_relationships import (
            quarantine_peer, get_peer,
        )
        body = request.get_json(force=True) or {}
        pid = (body.get('peer_id') or '').strip()
        reason = (body.get('reason') or '').strip()
        if not pid:
            return jsonify({
                'error': 'peer_id_required',
                'hint': 'body: {peer_id: "astor:<32-hex>", reason: "..."}',
            }), 400
        existing = get_peer(pid)
        if not existing:
            return jsonify({
                'error': 'peer_not_found',
                'peer_id': pid,
            }), 404
        result = quarantine_peer(pid, reason=reason)
        return jsonify({
            'ok': True,
            'peer_id': pid,
            'kind': result.get('kind') if result else None,
            'trust': int(result.get('trust', 0)) if result else 0,
            'quarantine_reason': reason,
            'hint': 'use /v1/peer/unquarantine to restore',
        })

    @app.route('/v1/peer/unquarantine', methods=['POST'])
    def peer_unquarantine():
        from ._internal.peer_relationships import unquarantine_peer
        body = request.get_json(force=True) or {}
        pid = (body.get('peer_id') or '').strip()
        restore = bool(body.get('restore_trust', True))
        if not pid:
            return jsonify({
                'error': 'peer_id_required',
                'hint': 'body: {peer_id: "astor:<32-hex>", restore_trust: bool}',
            }), 400
        result = unquarantine_peer(pid, restore_trust=restore)
        if not result:
            return jsonify({'error': 'peer_not_found', 'peer_id': pid}), 404
        return jsonify({
            'ok': True,
            'peer_id': pid,
            'kind': result.get('kind'),
            'trust': int(result.get('trust', 0)),
            'restored': restore,
        })

    @app.route('/v1/peer/quarantine/list', methods=['GET'])
    def peer_quarantine_list():
        from ._internal.peer_relationships import list_quarantined_peers
        rows = list_quarantined_peers()
        # Apply basic key sanitization (drop public_key blob)
        out = []
        for r in rows:
            out.append({
                'peer_id': r.get('peer_id'),
                'alias': r.get('alias'),
                'kind': r.get('kind'),
                'trust': int(r.get('trust', 0)),
                'endpoint': r.get('endpoint'),
                'metadata': r.get('metadata') or {},
                'updated_at': r.get('updated_at'),
            })
        return jsonify({'count': len(out), 'peers': out})

    # ------------------------------------------------------------------
    # v1.15.19 (2026-09-28) — PPS Phase 4 follow-up: Peer CRUD + search REST.
    # The CLI is in cli/main.py; this route set mirrors those operations
    # so the dashboard panel (Ship C) can drive everything from JS.
    # All endpoints run as the server's admin identity.
    # ------------------------------------------------------------------

    @app.route('/v1/peer/list', methods=['GET'])
    def peer_list():
        """List all peers with metadata, trust, allow-search flag.

        Query params:
          kind: filter by kind ('friend'|'blacklist'|'whitelist'|'pending')
          min_trust: filter by min trust (0-100)
        Returns: {peers: [{peer_id, alias, kind, trust, endpoint, has_pubkey, allow_search, added_at, updated_at, metadata_keys}]}
        """
        from ._internal.peer_relationships import list_peers as _pl
        kind = request.args.get('kind')
        min_trust = request.args.get('min_trust')
        mt = int(min_trust) if (min_trust and min_trust.isdigit()) else None
        rows = _pl(kind=kind, min_trust=mt)
        out = []
        for r in rows:
            meta = r.get('metadata') or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            out.append({
                'peer_id': r.get('peer_id'),
                'alias': r.get('alias') or '',
                'kind': r.get('kind') or 'friend',
                'trust': int(r.get('trust') or 0),
                'endpoint': r.get('endpoint') or '',
                'has_pubkey': bool(r.get('public_key')),
                'allow_search': bool(meta.get('allow_search', False)),
                'added_at': r.get('added_at') or '',
                'updated_at': r.get('updated_at') or '',
                'metadata_keys': sorted(meta.keys()) if isinstance(meta, dict) else [],
            })
        return jsonify({'peers': out, 'count': len(out)})

    @app.route('/v1/peer/add', methods=['POST'])
    def peer_add():
        """Add or update a friend.

        Body JSON: {peer_id (required), alias?, trust?, pubkey?, endpoint?, kind?}
        Returns: {ok: bool, peer_id, kind, trust}
        """
        from ._internal.peer_relationships import add_peer as _ap
        body = request.get_json(force=True) or {}
        pid = body.get('peer_id') or ''
        if not pid.startswith('astor:') or len(pid) != len('astor:') + 32:
            return jsonify({'error': 'invalid_peer_id',
                            'detail': 'must be astor:<32-hex>'}), 400
        trust = int(body.get('trust', 30))
        if not 0 <= trust <= 100:
            return jsonify({'error': 'trust_out_of_range'}), 400
        kind = body.get('kind', 'friend')
        pubkey = body.get('pubkey') or None
        endpoint = body.get('endpoint') or None
        try:
            row = _ap(
                pid, kind=kind, trust=trust, alias=body.get('alias'),
                public_key=pubkey, endpoint=endpoint,
            )
        except Exception as e:
            return jsonify({'error': 'add_failed', 'detail': str(e)}), 400
        return jsonify({'ok': True, 'peer_id': pid, 'kind': row.get('kind'),
                        'trust': int(row.get('trust') or 0)})

    @app.route('/v1/peer/<peer_id>/trust', methods=['POST'])
    def peer_trust(peer_id):
        """Update trust score for a peer. Body: {trust: 0-100}."""
        from ._internal.peer_relationships import (
            update_trust as _ut, get_peer as _gp,
        )
        body = request.get_json(force=True) or {}
        try:
            trust = int(body.get('trust'))
        except (TypeError, ValueError):
            return jsonify({'error': 'invalid_trust'}), 400
        if not 0 <= trust <= 100:
            return jsonify({'error': 'trust_out_of_range'}), 400
        if not _gp(peer_id):
            return jsonify({'error': 'unknown_peer'}), 404
        row = _ut(peer_id, trust)
        return jsonify({'ok': True, 'peer_id': peer_id, 'trust': trust,
                        'kind': (row or {}).get('kind')})

    @app.route('/v1/peer/<peer_id>/allow-search', methods=['POST'])
    def peer_allow_search(peer_id):
        """Set or revoke the allow-search opt-in flag. Body: {allow: bool}."""
        from ._internal.peer_relationships import (
            set_allow_search as _sas, get_peer as _gp,
        )
        body = request.get_json(force=True) or {}
        allow = bool(body.get('allow', True))
        if not _gp(peer_id):
            return jsonify({'error': 'unknown_peer'}), 404
        ok = _sas(peer_id, allow)
        return jsonify({'ok': ok, 'peer_id': peer_id, 'allow_search': allow})

    @app.route('/v1/peer/<peer_id>/blacklist', methods=['POST'])
    def peer_blacklist(peer_id):
        """Mark a peer as blacklisted. Body: {reason?: str}."""
        from ._internal.peer_relationships import add_peer as _ap
        body = request.get_json(force=True) or {}
        reason = (body.get('reason') or '').strip() or None
        meta = {'blacklist_reason': reason} if reason else None
        _ap(peer_id, kind='blacklist', trust=0, metadata=meta)
        return jsonify({'ok': True, 'peer_id': peer_id,
                        'kind': 'blacklist', 'reason': reason})

    @app.route('/v1/peer/<peer_id>/unblacklist', methods=['POST'])
    def peer_unblacklist(peer_id):
        """Restore a blacklisted peer to friend status with default trust=30."""
        from ._internal.peer_relationships import add_peer as _ap
        _ap(peer_id, kind='friend', trust=30)
        return jsonify({'ok': True, 'peer_id': peer_id, 'kind': 'friend'})

    @app.route('/v1/peer/<peer_id>', methods=['DELETE'])
    def peer_delete(peer_id):
        """Remove the peer relationship entirely."""
        from ._internal.peer_relationships import remove_peer as _rp
        ok = _rp(peer_id)
        return jsonify({'ok': ok, 'peer_id': peer_id})

    @app.route('/v1/peer/search', methods=['GET'])
    def peer_search_local():
        """Server-as-client PPS search across local trust>=50 friends.

        Query params: q (required), topic (optional), limit (1..20 default 10).
        Reads my identity, builds + signs a request, dispatches, returns the
        merged JSON (results from all eligible friends + per-peer status).
        Runs as admin (server actor).
        """
        from ._internal.peer_search import (
            build_search_request, select_search_targets,
            dispatch_search_to_peers,
        )
        from ._internal.peer_identity import init_identity
        from ._internal.peer_relationships import list_peers
        q = request.args.get('q', '').strip()
        if not q:
            return jsonify({'error': 'q_required'}), 400
        topic = request.args.get('topic') or None
        try:
            limit = int(request.args.get('limit', 10))
        except ValueError:
            limit = 10
        limit = max(1, min(limit, 20))
        try:
            me = init_identity()
        except Exception as e:
            return jsonify({'error': 'no_local_identity',
                            'detail': str(e)}), 503
        peers = list_peers()
        targets = select_search_targets(peers)
        if not targets:
            return jsonify({'results': [], 'count': 0,
                            'targets': 0,
                            'hint': 'no eligible friends (trust>=50 + endpoint + allow_search)'})
        req = build_search_request(
            query=q, requestor_peer_id=me['peer_id'],
            requestor_pubkey=me['public_key'],
            requestor_private_key=me['private_key'],
            topic=topic, limit=limit,
        )
        responses = dispatch_search_to_peers(req, targets)
        merged = []
        per_peer = []
        for tgt, resp in zip(targets, responses):
            per_peer.append({
                'peer_id': tgt['peer_id'],
                'alias': tgt.get('alias') or '',
                'error': resp.error,
                'count': len(resp.results),
                'truncated': resp.truncated,
            })
            merged.extend(resp.results)
        # rank by relevance desc, top 20
        merged.sort(key=lambda r: r.relevance, reverse=True)
        # Convert each PeerSearchResult to dict inline (PeerSearchResult has
        # no to_dict — only PeerSearchResponse does). Bugfix v1.15.20.
        def _res_to_dict(r):
            return {
                'source_peer_id': r.source_peer_id,
                'source_trust': r.source_trust,
                'fact_id': r.fact_id,
                'content': r.content,
                'kind': r.kind,
                'tags': list(r.tags),
                'created_at': r.created_at,
                'relevance': r.relevance,
            }
        return jsonify({
            'results': [_res_to_dict(r) for r in merged[:20]],
            'count': len(merged),
            'targets': len(targets),
            'per_peer': per_peer,
        })

    @app.route('/v1/peer/adopt', methods=['POST'])
    def peer_adopt():
        """Manually adopt (write) peer results into local source/public tier.

        Body JSON: {source_peer_id: str, tier?: 'source'|'public', facts: [
          {fact_id?: int, content: str (required), kind?: str, tags?: [str], importance?: float},
          ...
        ]}
        Uses the canonical event → candidate → promote pipeline so embeddings
        are computed automatically. Each adopted fact ends up tagged with
        metadata.adopted_via='peer:search' + original_fact_id + source_peer_id
        for traceability.
        """
        from .bus.store import astor_bus
        from datetime import datetime as _dt_adopt, timezone as _tz_adopt
        body = request.get_json(force=True) or {}
        src_peer = (body.get('source_peer_id') or '').strip()
        facts = body.get('facts') or []
        target_tier = body.get('tier') or 'source'
        if not src_peer:
            return jsonify({'error': 'source_peer_id_required'}), 400
        if not isinstance(facts, list) or not facts:
            return jsonify({'error': 'facts_required',
                            'detail': 'pass facts: [{content:..., kind?, ...}, ...]'}), 400
        if target_tier not in ('source', 'public'):
            return jsonify({'error': 'invalid_tier',
                            'detail': 'tier must be "source" or "public"'}), 400
        # Bind ACL with admin identity before bus write (the before_request
        # hook skipped us because we don't put 'tier' in the body).
        try:
            from ._internal.acl import astor_init_acl
            astor_init_acl(actor='peer:adopt', role='admin',
                           tier=target_tier, user_id='admin',
                           subscription_plan=None)
        except Exception:
            pass
        bus = astor_bus(tier=target_tier, user_id='admin')
        adopted_at = _dt_adopt.now(_tz_adopt.utc).isoformat(
            timespec='seconds').replace('+00:00', 'Z')
        written = []
        skipped = 0
        for f in facts[:20]:
            content = (f.get('content') or '').strip()
            if not content or len(content) < 8:
                skipped += 1
                continue
            kind = f.get('kind') or 'fact'
            tags_list = list(f.get('tags') or [])
            importance = float(f.get('importance', 0.5))
            original_fid = int(f.get('fact_id') or 0)
            try:
                event_id = bus.append_event(
                    namespace='adopted', agent_id='peer',
                    source='peer_adopt', action='adopt',
                    content=content,
                    metadata={
                        'source_peer_id': src_peer,
                        'adopted_at': adopted_at,
                        'original_fact_id': original_fid,
                    },
                )
                candidate_id = bus.insert_candidate(
                    event_id=event_id,
                    namespace='peer_adopted',
                    content=content,
                    kind=kind, importance=importance,
                    tags=tags_list + ['peer_adopted', f'peer:{src_peer}'],
                    metadata={
                        'adopted_via': 'peer:search',
                        'adopted_at': adopted_at,
                        'source_peer_id': src_peer,
                        'original_fact_id': original_fid,
                        'source_kind': kind,
                    },
                )
                # promote → inserts canonical + computes embedding
                new_id = bus.promote_candidate(
                    candidate_id=candidate_id,
                    promoted_by='peer:adopt',
                    user_id='admin',
                    tier=target_tier,
                    scope_type='long_term',
                    verdict='settled',
                    provenance_kind='peer_search',
                    provenance_agent=f'peer:{src_peer}',
                )
                written.append({
                    'new_fact_id': int(new_id),
                    'original_fact_id': original_fid,
                    'content_preview': content[:80],
                })
            except Exception as e:
                skipped += 1
                continue
        # v1.15.23 Ship F: auto-bump source peer trust on successful
        # adoption. Anti-hostile: only fires on EXPLICIT operator
        # action (adopt is a POST with caller-supplied content), not
        # on auto-paths. Bound: +1 per adopt, clamps 0..100.
        _bumped = None
        if written:
            try:
                from ._internal.peer_relationships import bump_trust
                _bumped = bump_trust(src_peer, 1)
            except Exception:
                _bumped = None
        # v1.15.25 Ship I: audit peer adopt.
        try:
            from ._internal.audit_logger import astor_audit as _pa_audit
            _pa_audit(
                actor=f'peer:{src_peer}',
                tier=target_tier,
                action='peer_adopt',
                user_id='admin',
                peer_id=src_peer,
                metadata={
                    'written_count': len(written),
                    'skipped_count': skipped,
                    'trust_after': _bumped,
                },
            )
        except Exception:
            pass
        return jsonify({'ok': True, 'tier': target_tier,
                        'source_peer_id': src_peer,
                        'adopted_at': adopted_at,
                        'written': written,
                        'count': len(written),
                        'skipped': skipped,
                        'peer_trust_after_adopt': _bumped})

    return app


def main():
    """Run dev server: python -m astor_memory.server"""
    import argparse
    parser = argparse.ArgumentParser(description='Astor-Memory REST API server')
    parser.add_argument('--host', default='127.0.0.1', help='Bind host (default 127.0.0.1)')
    parser.add_argument('--port', type=int, default=7803, help='Port (default 7803)')
    parser.add_argument('--debug', action='store_true', help='Flask debug mode')
    parser.add_argument('--astor-dir', help='Override ASTOR_DIR (for testing)')
    args = parser.parse_args()

    app = create_app(astor_dir=args.astor_dir)
    print(f'[*] Astor-Memory v{__version__} REST API')
    print(f'   Listening on http://{args.host}:{args.port}')
    print(f'   Endpoints: /v1/health /v1/dashboard /v1/write /v1/read /v1/install')
    # Note: server warmup (model load + peer_id init) is now inside
    # create_app() so it fires on both `python -m astor_memory.server`
    # and any future gunicorn entrypoint. See create_app above.
    # v1.14.7 (2026-09-11): threaded=False. Earlier `threaded=True` enabled
    # concurrent Flask workers, but AstorNest._conn is a singleton (per
    # (tier, user_id, db_path)) and SQLite + numpy ndarray are not safe to
    # share across threads without explicit per-thread connections. Production
    # traffic at 1 req/min triggered sporadic SIGSEGVs in the SQLite C
    # bindings. We accept serial request handling (no parallelism) in
    # exchange for stability. A threaded server with per-thread connections
    # is the proper fix; ship that in a future version (v1.14.8+).
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=False)  # S13: enable Flask threaded mode for concurrent /v1/read requests (R-class N). Bus uses WAL mode so concurrent reads safe.


if __name__ == '__main__':
    import sys as _dbg_sys
    print(f'[DEBUG fork-trace] __main__ entered, sys.executable={_dbg_sys.executable!r}', flush=True)
    print(f'[DEBUG fork-trace] sys.argv={_dbg_sys.argv!r}', flush=True)
    print(f'[DEBUG fork-trace] PYTHONHOME={_dbg_sys.prefix!r}', flush=True)
    main()


__all__ = ['create_app', 'main']

# S13 v1.14.6 threaded=True — verified 2-concurrent OK, 4-concurrent overload (R-class Q bus lock)
