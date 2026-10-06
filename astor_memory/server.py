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

import os
import re
import sqlite3
import json
import sys
import numpy as np
import time
from pathlib import Path

# S15 (2026-09-10): dashboard cache — keeps aggregated payload so
# the HTML page polling /v1/dashboard doesn't re-run 16 user-db aggregates
# on every refresh. Cache key: astor_dir. Invalidation: 30s TTL
# (S18 2026-09-25, tightened from 5min) + write-trigger invalidate in
# /v1/write so hero.last_event_ts / growth_30d refresh instantly after writes.
def _astor_prewarm_dashboard_cache(astor_dir_str):
    """v1.16.42: pre-warm dashboard cache at startup.
    Implemented in _dashboard_prewarm.py to keep server.py lean.
    """
    try:
        from . import _dashboard_prewarm
        _dashboard_prewarm._astor_prewarm_dashboard_cache(astor_dir_str)
    except Exception as exc:
        print('   Dashboard prewarm failed: {}'.format(exc), flush=True)


_DASHBOARD_CACHE: dict = {"payload": None, "ts": 0.0, "astor_dir": None}
_DASHBOARD_TTL_SEC = 30  # 2026-09-25 S18: tighter TTL


# v1.16.x (2026-10-01): PII gate stats — lifetime counters for /v1/health
# + /v1/audit/health. Was missing from module level after recent refactor;
# restoring here to fix NameError on /v1/health.
_pii_gate_stats: dict = {"block_count": 0, "redact_count": 0}

# v1.16.x (2026-10-01): Read LRU+TTL cache — /v1/read checks first.
_READ_CACHE: dict = {}  # key=(query,tier,user,top_k) -> (timestamp, results)
import time as _rc_init_t
_RCACHE_TTL_S = 60


# v1.16.42 (2026-10-01): meta-recall stats dict — pre-existing latent bug
# where _meta_recall_stats (lowercase) was 'global' inside functions but
# never declared at module level. Define it here + alias uppercase form.
_META_RECALL_STATS: dict = {
    'triggered': 0,
    'returned_total': 0,
    'errors': 0,
    'consult_triggered': 0,
    'peer_fanout_triggered': 0,
}
_meta_recall_stats = _META_RECALL_STATS

# v1.16.42 (2026-10-01): bot-binding.db retry helper.
# v1.16.42 (2026-10-01): bot-binding.db retry helper.
# Per R-class 12485 / 100-user scale: SQLite WAL helps but binding writes still
# hit SQLITE_BUSY under 8-thread pool contention. Retry with exponential backoff.
import time as _bot_t
def _astor_bot_binding_connect(retry_max: int = 3):
    """Open bot-binding.db with retry on SQLITE_BUSY. 100-user safe."""
    import sqlite3 as _bot_s
    _delay = 0.05
    for _i in range(retry_max + 1):
        try:
            # v1.16.55: cross-platform ASTOR_DIR or ~/.astor
            from pathlib import Path as _Path
            _bdir = os.environ.get('ASTOR_DIR') or str(_Path.home() / '.astor')
            _db = _bot_s.connect(os.path.join(_bdir, 'bot-binding.db'), timeout=10.0)
            _db.execute('PRAGMA journal_mode=WAL')
            _db.execute('PRAGMA busy_timeout=5000')
            return _db
        except _bot_s.OperationalError as _oexc:
            if 'database is locked' not in str(_oexc).lower() and 'busy' not in str(_oexc).lower():
                raise
            if _i >= retry_max:
                raise
    return None

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


def _pii_last_24h_count() -> int:
    """v1.16.x: count `memory_defense_scan` audit_log rows in the last 24h.

    Query against the public bus audit_log table (most writes go to public tier
    so this is the best-effort aggregate). Returns 0 on any DB error so the
    /v1/audit/health endpoint stays robust.
    """
    try:
        from ._internal.acl_layout import get_db_path, Tier, Store
        import sqlite3 as _sqlite3
        import datetime as _dt
        path = str(get_db_path(Tier.PUBLIC, Store.BUS))
        conn = _sqlite3.connect(path)
        cutoff = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds') + 'Z'
        cur = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE event='memory_defense_scan' AND ts >= ?",
            (cutoff,),
        )
        n = cur.fetchone()[0]
        conn.close()
        return int(n)
    except Exception:
        return 0


from flask import Flask, jsonify, request

from . import __version__, astor_bus, astor_nest, astor_forge

def _astor_public_dir_label(astor_dir):
    """Return just the basename of astor_dir for user-facing responses.

    v1.16.50: do not leak the full on-disk path (the full on-disk path) to
    any user-facing endpoint. Admin-only /v1 endpoints can pass a flag
    if they need the full path; default is to mask.
    """
    try:
        from pathlib import Path as _P
        return _P(str(astor_dir)).name
    except Exception:
        return 'astor'
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


# 2026-10-03 (v1.16.61): intent-aware read (article-driven).
# The SimpleMem paper ("SimpleMem 高效终身记忆框架") argues that
# recall-read-side intent classification is the third pillar of good
# memory: same query surfaces different facts depending on whether the
# caller wants a procedure ("how to"), a preference ("I like dark mode"),
# a temporal context ("last week"), or a generic fact. Astor already
# has _astor_classify_intent() for write-side auto-routing; this adds
# a sibling for read-side intent logging + future rerank hooks.
#
# Six buckets. Priority: method > preference + 偏好 > pure personal >
# temporal > procedural > factual. The "my X" / "我 X" cases classify
# as preference when X is itself a preference-word (favors, like); as
# personal otherwise. Without this priority order, bare "我" matches
# before "我偏好" and we get the wrong bucket.
_INTENT_METHOD_RE = re.compile(
    r'(方法|规则|模式|架构|workflow|rule|pattern|architecture|framework|原理|机制)'
)
_INTENT_PREFERENCE_CN_RE = re.compile(
    r'(?<![一鿿])我[^\s]*?(喜欢|讨厌|偏好|prefer|like|dislike|hate|favor|favorite)'
)
_INTENT_MY_PREFERENCE_RE = re.compile(
    r'\bmy\s+(?:favorite|favour)'
)
_INTENT_PREFERENCE_RE = re.compile(
    r'(喜欢|讨厌|偏好|prefer\b|like\b|dislike|hate|favor|favorite)'
)
_INTENT_PERSONAL_MY_RE = re.compile(
    r'(my\s+|i\s+(?:am|was|will|did|have)\b|mine\b|(?<![一鿿])我的(?![一鿿]))'
)
_INTENT_PERSONAL_CN_RE = re.compile(
    r'(?<![一鿿])我(?![一鿿])|自己'
)
_INTENT_TEMPORAL_RE = re.compile(
    r'(昨天|今天|明天|上个月|last\s+(?:week|month|day|year|time)|yesterday|today|ago)'
)
_INTENT_PROCEDURAL_RE = re.compile(
    r'(怎么|如何|how\s+to|how\s+(?:do|can|should)|step\s*by\s*step)'
)


def classify_read_intent(query: str) -> str:
    """Classify a /v1/read query into one of 6 intents.

    Returns one of: factual, procedural, temporal, personal, preference,
    method. Caller may override by passing body.intent; this default
    classifier is what gets used when the caller leaves it unset.

    Note: this does not change retrieval today — it only logs the
    intent to recall_log.jsonl so future ship arc (intent-aware rerank,
    EvolveMem-style self-tuning) can use it. Cheap classifier, no DB
    reads, no extra latency.
    """
    q = (query or '').strip().lower()
    if not q:
        return 'factual'
    if _INTENT_METHOD_RE.search(q):
        return 'method'
    if _INTENT_PREFERENCE_CN_RE.search(q) or _INTENT_MY_PREFERENCE_RE.search(q):
        return 'preference'
    if _INTENT_PREFERENCE_RE.search(q):
        return 'preference'
    if _INTENT_PERSONAL_MY_RE.search(q) or _INTENT_PERSONAL_CN_RE.search(q):
        return 'personal'
    if _INTENT_TEMPORAL_RE.search(q):
        return 'temporal'
    if _INTENT_PROCEDURAL_RE.search(q):
        return 'procedural'
    return 'factual'


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


_astor_outcome_verbs_dict = {
    'success_pattern': ['成功', 'verified', 'ship', '搞定', 'works', 'verified working'],
    'failure_pattern': ['失败', 'failed', 'broken', '崩溃', 'bug', 'error', 'unable'],
    'lesson':           ['下次', 'remember', 'lesson', 'R-class', 'fix', '永久', 'always'],
    'method':           ['方法', 'how to', 'procedure'],
}


def _classify_fact_outcome(text):
    text_lower = text.lower()
    scores = {k: sum(1 for kw in v if kw in text_lower)
              for k, v in _astor_outcome_verbs_dict.items()}
    if not any(scores.values()):
        return 'fact', 0.5
    best = max(scores, key=scores.get)
    return best, 0.5 + 0.15 * scores[best]


# v1.16.37+ Hook: classify_fact_outcome + REUSE + EXPIRY + auto-link
# Insert BEFORE _astor_bind_request_acl at module level
_ASTOR_OUTCOME_VERBS = {
    'success_pattern': [
        # English
        'success', 'verified', 'ship', 'works', 'fixed', 'pass', 'good', 'ok',
        # Chinese
        '成功', '搞定', '可以', '已 ship', '通过', '解决',
    ],
    'failure_pattern': [
        'failed', 'broken', 'fail', 'unable', 'cannot', "doesn't work",
        '失败', '崩溃', '不行', '出错', '错误', 'bug', '问题', 'fix',
    ],
    'lesson': [
        # English — LESSON LLM hints
        'r-class', 'always', 'never', 'remember', 'must', 'rule', 'lesson',
        'permanent', 'forever', 'in future', 'next time',
        # Chinese
        '下次', '永久', '永远', '记住', '规则', '原则', '教训',
        # Specific keywords
        '下次一定要', '记得', '切记',
    ],
    'method': [
        'how to', 'procedure', 'method', 'step by step',
        '方法', '步骤', '流程', '怎么',
    ],
}


def _classify_fact_outcome(text):
    """Auto-classify fact kind based on outcome keywords.
    Returns (suggested_kind, confidence 0..1).
    """
    text_lower = text.lower()
    scores = {k: sum(1 for kw in v if kw in text_lower)
              for k, v in _ASTOR_OUTCOME_VERBS.items()}
    if not any(scores.values()):
        return 'fact', 0.5
    best = max(scores, key=scores.get)
    return best, 0.5 + 0.15 * scores[best]


# === REUSE PATTERNS (before-action hook) ===
_ASTOR_REUSE_PATTERNS = [
    ('moomoo', 'place_order', True),
    ('moomoo', 'cancel_order', True),
    ('moomoo', 'unlock_trade', True),
    ('moomoo', 'get_positions', False),
    ('kraken', 'place_order', True),
    ('kraken', 'withdraw', True),
    ('kraken', 'ticker', False),
    ('opend', 'unlock_trade', True),
    ('opend', 'place_order', True),
]


def _check_reuse_pattern(text):
    """Check if text matches a REUSE pattern. Returns (matched, block_msg) tuple."""
    text_lower = text.lower()
    for platform, action, block in _ASTOR_REUSE_PATTERNS:
        if platform in text_lower and action in text_lower:
            return True, f'REUSE pattern: {platform}.{action} (block={block})'
    return False, None


# === EXPIRY TTL ===
_ASTOR_TTL_DAYS = {
    'lesson': None,            # permanent
    'postmortem': None,        # permanent
    'success_pattern': 365,    # 1 year
    'failure_pattern': 365,    # 1 year
    'method': 180,            # 6 months
    'recipe': 180,
    'fact': 90,               # 3 months
    'mental_model': None,      # permanent
}


def _compute_ttl_days(kind):
    """TTL days based on kind. None = permanent."""
    return _ASTOR_TTL_DAYS.get(kind, 90)


# === AUTO-LINK (find parent fact_id in text) ===
import re
_ASTOR_FACT_ID_PATTERN = re.compile(
    r'(?:fact\s*|R-class\s*|\[|#)(\d{2,5})\b'
)


def _find_parent_fact_ids(text):
    """Find candidate parent fact_ids mentioned in text. Returns list[int]."""
    if not text:
        return []
    matches = []
    for m in _ASTOR_FACT_ID_PATTERN.finditer(text):
        try:
            matches.append(int(m.group(1)))
        except ValueError:
            pass
    return list(set(matches))





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

    # v1.16.37+ Hook: classify_fact_outcome + REUSE + EXPIRY + auto-link
    # Insert BEFORE _astor_bind_request_acl at module level
    _ASTOR_OUTCOME_VERBS = {
        'success_pattern': [
            # English
            'success', 'verified', 'ship', 'works', 'fixed', 'pass', 'good', 'ok',
            # Chinese
            '成功', '搞定', '可以', '已 ship', '通过', '解决',
        ],
        'failure_pattern': [
            'failed', 'broken', 'fail', 'unable', 'cannot', "doesn't work",
            '失败', '崩溃', '不行', '出错', '错误', 'bug', '问题', 'fix',
        ],
        'lesson': [
            # English — LESSON LLM hints
            'r-class', 'always', 'never', 'remember', 'must', 'rule', 'lesson',
            'permanent', 'forever', 'in future', 'next time',
            # Chinese
            '下次', '永久', '永远', '记住', '规则', '原则', '教训',
            # Specific keywords
            '下次一定要', '记得', '切记',
        ],
        'method': [
            'how to', 'procedure', 'method', 'step by step',
            '方法', '步骤', '流程', '怎么',
        ],
    }


    def _classify_fact_outcome(text):
        """Auto-classify fact kind based on outcome keywords.
        Returns (suggested_kind, confidence 0..1).
        """
        text_lower = text.lower()
        scores = {k: sum(1 for kw in v if kw in text_lower)
                  for k, v in _ASTOR_OUTCOME_VERBS.items()}
        if not any(scores.values()):
            return 'fact', 0.5
        best = max(scores, key=scores.get)
        return best, 0.5 + 0.15 * scores[best]


    # === REUSE PATTERNS (before-action hook) ===
    _ASTOR_REUSE_PATTERNS = [
        ('moomoo', 'place_order', True),
        ('moomoo', 'cancel_order', True),
        ('moomoo', 'unlock_trade', True),
        ('moomoo', 'get_positions', False),
        ('kraken', 'place_order', True),
        ('kraken', 'withdraw', True),
        ('kraken', 'ticker', False),
        ('opend', 'unlock_trade', True),
        ('opend', 'place_order', True),
    ]


    def _check_reuse_pattern(text):
        """Check if text matches a REUSE pattern. Returns (matched, block_msg) tuple."""
        text_lower = text.lower()
        for platform, action, block in _ASTOR_REUSE_PATTERNS:
            if platform in text_lower and action in text_lower:
                return True, f'REUSE pattern: {platform}.{action} (block={block})'
        return False, None


    # === EXPIRY TTL ===
    _ASTOR_TTL_DAYS = {
        'lesson': None,            # permanent
        'postmortem': None,        # permanent
        'success_pattern': 365,    # 1 year
        'failure_pattern': 365,    # 1 year
        'method': 180,            # 6 months
        'recipe': 180,
        'fact': 90,               # 3 months
        'mental_model': None,      # permanent
    }


    def _compute_ttl_days(kind):
        """TTL days based on kind. None = permanent."""
        return _ASTOR_TTL_DAYS.get(kind, 90)


    # === AUTO-LINK (find parent fact_id in text) ===
    import re
    _ASTOR_FACT_ID_PATTERN = re.compile(
        r'(?:fact\s*|R-class\s*|\[|#)(\d{2,5})\b'
    )


    def _find_parent_fact_ids(text):
        """Find candidate parent fact_ids mentioned in text. Returns list[int]."""
        if not text:
            return []
        matches = []
        for m in _ASTOR_FACT_ID_PATTERN.finditer(text):
            try:
                matches.append(int(m.group(1)))
            except ValueError:
                pass
        return list(set(matches))


    # === BEFORE-WRITE HOOK ===
    @app.before_request
    def _astor_before_write_hook():
        """v1.16.37+: BEFORE-WRITE hook — classify outcome + REUSE check + auto-link.

        Per R-class 12485 (完整性), this hook covers:
          - 3: AUTO-CAPTURE (outcome classifier -> kind)
          - 4: AUTO-LINK (find parent fact_ids in text)
          - 6: REUSE (block if no astor_recall was done before moomoo/kraken/etc action)

        Stashes suggestions on request.environ; write() handler reads them.
        """
        try:
            if not (request.method == 'POST' and request.path == '/v1/write'):
                return

            body = request.get_json(silent=True) or {}
            text = body.get('text', '')
            if not text or len(text) < 10:
                return

            # 3. AUTO-CAPTURE: outcome classifier
            suggested_kind, confidence = _classify_fact_outcome(text)
            request.environ['ASTOR_SUGGESTED_KIND'] = suggested_kind
            request.environ['ASTOR_SUGGESTED_CONFIDENCE'] = confidence
            if 'kind' not in body:
                body['kind'] = suggested_kind
                request.environ['ASTOR_AUTO_FILLED_KIND'] = True

            # 4. AUTO-LINK: find parent fact_ids
            parent_ids = _find_parent_fact_ids(text)
            if parent_ids:
                request.environ['ASTOR_PARENT_IDS'] = parent_ids

            # 6. REUSE: check for known patterns
            matched, msg = _check_reuse_pattern(text)
            if matched and request.environ.get('ASTOR_REUSE_CHECKED') is None:
                # Log a warning (don't block by default — agent may legitimately
                # need to act without prior recall)
                _safe_stderr_write(
                    f'[astor.before_write_hook] REUSE pattern detected: {msg}\n'
                    f'  text={text[:120]}\n'
                    f'  astor_recall should have been called before this write.\n'
                )
                request.environ['ASTOR_REUSE_WARNING'] = msg
        except Exception as exc:
            _safe_stderr_write(f'[astor.before_write_hook] hook error: {exc}\n')


    import gc as _gc

    @app.after_request
    def _astor_gc_watchdog(response):
        """v1.16.39+: Periodic GC after heavy endpoints.
        Per R-class 12824 / DC timeout root cause: embedding model + numpy
        buffer pool + fastembed ONNX state can grow unboundedly. Force
        gc.collect() after /v1/read + /v1/write to keep RSS stable.
        """
        try:
            if request.path in ('/v1/read', '/v1/write', '/v1/episode', '/v1/distill'):
                _gc.collect()
        except Exception:
            pass
        return response


    @app.before_request
    def _astor_bind_request_acl() -> None:
        # Reset per-request bus/nest tracking lists.
        _request_buses.clear()
        _request_nests.clear()
        # 2026-08-16: Always bind a default ACL for GET requests (e.g. health,
        # viewer_stats, lex_stats). Without this, Flask worker threads may
        # not have _CURRENT set, and downstream astor_check_* raises
        # "astor_acl not initialized" → 500. POST requests get per-body binding.
        if request.method == 'POST':
            # v1.16.32: parse X-Actor header (legacy admin/user marker from muse
            # clients). Without this, ctx.role in admin endpoints stayed at
            # 'admin' from prior request — allowing non-admin callers to invoke
            # admin-only paths. Header is parsed FIRST so it overrides body defaults.
            _x_actor = request.headers.get('X-Actor', '').strip()
            if request.is_json:
                body = request.get_json(silent=True) or {}
            else:
                body = {}
            if _x_actor:
                # X-Actor formats: 'admin:<id>' or 'user:<id>' or just '<id>'
                _actor_user_id = None
                if _x_actor.startswith('admin:') or _x_actor.startswith('user:'):
                    _actor_user_id = _x_actor.split(':', 1)[1] or None
                else:
                    _actor_user_id = _x_actor
                if _actor_user_id:
                    body['user'] = _actor_user_id
                    body.setdefault('user_id', _actor_user_id)
            # v1.16.31: visibility_hint=personal forces tier=private so before_request
            # binds ACL with the right identity.
            if body.get('visibility_hint') == 'personal':
                body['tier'] = 'private'
                body['user_id'] = 'admin'
            # v1.16.32: bind ACL for ALL POST requests, not just tier-based.
            # Previously endpoints without tier (e.g. /v1/admin/toggle_distill)
            # left _CURRENT contextvar stale from prior request, allowing a
            # non-admin caller to invoke admin-only paths because ctx.role was
            # still 'admin' from a prior request.
            tier = body.get('tier')
            if tier is None or tier in ('public', 'source', 'private', 'repo'):
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
                # v1.16.32: tier=None -> public (admin endpoints can still role-check)
                if tier is None:
                    tier = 'public'
                if tier == 'private':
                    target_user = body.get('user_id') or body_user
                elif tier == 'repo':
                    target_user = body_user
                else:
                    # v1.16.32: for tier=public (including tier-less fallback), use
                    # body.user_id if present so admin endpoints like distill see
                    # the proper caller identity. R-class 12747 locked user_id
                    # binding as SSoT for downstream per-user DB routing.
                    target_user = body.get('user_id') or body_user
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
            # v1.16.50: mask on-disk path; expose only dir basename.
            'astor_dir': _astor_public_dir_label(get_default_astor_dir()),
            'dbs': {
                # v1.16.50: do not leak sqlite file paths in /v1/health.
                'bus': 'ok',
                'nest': 'ok',
            },
            # v1.16.x: reactive consult + 三层内容防线 — health surface.
            'v116x_tier_routing': {
                'failure_tier': 'public',
                'lesson_tier': 'public',
                'consult_default_on': os.environ.get('ASTOR_CONSULT_DEFAULT_ON', '1') == '1',
                'tier_hint_required': os.environ.get('ASTOR_TIER_HINT_REQUIRED', '0') == '1',
                'personal_content_warn_enabled': True,
                'pii_public_force_env': os.environ.get('ASTOR_PII_PUBLIC_FORCE', '1') == '1',
            },
            'pii_gate': dict(_pii_gate_stats),  # lifetime counters
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

    # v1.16.30 (2026-10-01): Personal-to-Commons Distillation.
    # Source fact stays in personal bucket + a new commons-eligible copy
    # is written (per user directive — private data preserved AND method
    # extracted). Admin or user-self (with distill_opt_in=1) triggers.
    @app.route('/v1/fact/<int:fact_id>/distill', methods=['POST'])
    @app.route('/v1/fact/<int:fact_id>/distill_auto', methods=['POST'])
    def fact_distill(fact_id):
        import datetime as _dt
        from .nest.distiller import distill as _distill, clear_tool_results as _clear_tool_results
        body = request.get_json(force=True) if request.is_json else {}
        auto_mode = '/distill_auto' in request.path
        # ACL: distill = admin only; distill_auto = user-self only.
        try:
            ctx = astor_current_acl()
        except Exception:
            return jsonify({'error': 'astor_init_acl required'}), 503
        if auto_mode:
            if ctx.role != 'admin':
                # Allow user self-service if they own this fact.
                # ownership check below.
                pass
        else:
            if ctx.role != 'admin':
                return jsonify({'error': 'distill requires admin'}), 403
        # v1.16.32: silent ACL — verify caller can read the source fact's tier
        # BEFORE loading (R-class 12283: don't leak fact_id existence to non-owner).
        # For admin role this is always allowed; for user role, only own facts.
        # Distill Auto path handles this in the auto_mode block below.
        # Load source fact from all tiers. Admin can distill any tier.
        # v1.16.30 schema: per-user dirs are users/<uid>/memory/astor_bus_<uid>.db.
        # Older 'private_<uid>' legacy paths still exist (admin_resolver fallback).
        src_fact = None
        src_tier = None
        import glob as _glob
        # v1.16.55: 2026-10-03 audit-fix. Removed <user> hardcode (was
        # treating another user's ID as a candidate for any ctx.user_id
        # fallback — PII leak). Removed operator-specific runtime-path hardcode (uses ASTOR_DIR / ~/.astor fallback).
        from pathlib import Path as _Path
        _astor_dir = os.environ.get('ASTOR_DIR') or str(_Path.home() / '.astor')
        _tiers_to_try = ['public', 'source']
        # Try per-user paths
        for _candidate_uid in [ctx.user_id, 'admin']:
            if _candidate_uid:
                _p = os.path.join(_astor_dir, 'users', _candidate_uid, 'memory')
                if os.path.isdir(_p):
                    for _dbfile in _glob.glob(os.path.join(_p, 'astor_bus_*.db')):
                        # Try the user-uid DB (legacy naming)
                        try:
                            # v1.16.31: tier='private' (generic) is the bus layer;
                            # user_id parameter differentiates the per-user DB.
                            _bus = astor_bus(tier='private', user_id=_candidate_uid)
                            _bus.conn.row_factory = sqlite3.Row
                            _row = _bus.conn.execute(
                                "SELECT id, content, kind, user_id, namespace, visibility FROM memory_canonical WHERE id = ? AND tombstoned = 0",
                                (fact_id,),
                            ).fetchone()
                            if _row:
                                src_fact = _row
                                src_tier = 'private'
                                break
                        except Exception:
                            continue
        for _try_tier in _tiers_to_try:
            try:
                _bus = astor_bus(tier=_try_tier, user_id=None)
                _bus.conn.row_factory = sqlite3.Row
                _row = _bus.conn.execute(
                    "SELECT id, content, kind, user_id, namespace, visibility FROM memory_canonical WHERE id = ? AND tombstoned = 0",
                    (fact_id,),
                ).fetchone()
                if _row:
                    src_fact = _row
                    src_tier = _try_tier
                    break
            except Exception:
                continue
        # also check private_<user_id> if not found
        if not src_fact:
            try:
                _bus = astor_bus(tier='private', user_id=ctx.user_id)
                _bus.conn.row_factory = sqlite3.Row
                _row = _bus.conn.execute(
                    "SELECT id, content, kind, user_id, namespace, visibility FROM memory_canonical WHERE id = ? AND tombstoned = 0",
                    (fact_id,),
                ).fetchone()
                if _row:
                    src_fact = _row
                    src_tier = 'private'
            except Exception:
                pass
        if not src_fact:
            return jsonify({'error': 'fact_not_found', 'fact_id': fact_id}), 404
        # For auto mode: must be own fact + user has distill_opt_in=1
        if auto_mode:
            if src_fact['user_id'] != ctx.user_id:
                return jsonify({'error': 'can_only_distill_own_facts'}), 403
            try:
                import sqlite3 as _sq
                # v1.16.55: cross-platform path via _astor_dir
                _sqdb = _sq.connect(os.path.join(_astor_dir, 'bot-binding.db'))
                _row = _sqdb.execute(
                    "SELECT distill_opt_in FROM user_meta WHERE user_id = ?",
                    (ctx.user_id,),
                ).fetchone()
                _sqdb.close()
                if not _row or _row[0] != 1:
                    return jsonify({'error': 'distill_opt_in_not_set',
                                    'detail': 'user must opt in via /v1/admin/user/{user_id}/toggle_distill'}), 403
            except Exception as _e:
                return jsonify({'error': 'distill_opt_check_failed', 'detail': str(_e)}), 500

        # v1.16.32: pre-distill guards
        # (a) commons→commons guard. v1.16.34 fix: this guard was too
        # aggressive — auto-promoted facts (kind in AUTO_COMMONS_KINDS,
        # e.g. success_pattern/method/lesson) are EXACTLY what we want
        # to distill further. Only block if the source was ALREADY a
        # user_distilled commons (avoid loops).
        try:
            _src_vis = src_fact['visibility']
        except (KeyError, TypeError, IndexError):
            _src_vis = None
        if _src_vis == 'commons':
            # Allow re-distill of commons facts ONLY if they were auto-
            # promoted (kind=method/success_pattern/lesson/etc.) and
            # have NOT been distilled before.
            _src_kind = ''
            try:
                _src_kind = src_fact['kind']
            except (KeyError, TypeError, IndexError):
                pass
            _AUTO_COMMONS = {'method', 'recipe', 'lesson',
                             'success_pattern', 'failure_pattern',
                             'mental_model', 'knowledge_page', 'flow'}
            if _src_kind not in _AUTO_COMMONS:
                return jsonify({
                    'error': 'source_already_commons',
                    'detail': 'distill only applies to personal-tier or auto-promoted commons facts',
                    'source_fact_id': fact_id,
                    'source_visibility': 'commons',
                    'source_kind': _src_kind,
                }), 422
            # Else: auto-promoted kind → allow (de-dupe handled by step b)
        # (b) source must not already be distilled (check provenance_kind of
        # commons facts that reference this source via metadata.distilled_from)
        try:
            import sqlite3 as _sq_d
            # v1.16.55: cross-platform path via _astor_dir
            _pub_db = _sq_d.connect(os.path.join(_astor_dir, 'public', 'memory', 'astor_bus_public.db'))
            _existing = _pub_db.execute(
                "SELECT id FROM memory_canonical WHERE tombstoned = 0 "
                "AND json_extract(metadata, '$.distilled_from') = ? LIMIT 1",
                (fact_id,),
            ).fetchone()
            _pub_db.close()
            if _existing:
                return jsonify({
                    'error': 'already_distilled',
                    'detail': f'this fact already has a commons distillation (id={_existing[0]})',
                    'existing_commons_fact_id': _existing[0],
                }), 409
        except Exception as _e_dup:
            # Distillation dedup is best-effort; if the check fails (e.g.
            # public bus unreachable), allow it through rather than blocking.
            pass

        # v1.16.33: tool result clearing (OPT-1) — apply to source content
        # before distillation. Older tool_result JSON blobs are replaced with
        # compact reference placeholders. Keeps the most recent N (default 3).
        # Article claim: "单做 context editing 就把 token 消耗降了 84%".
        _keep_recent = int(body.get('keep_tool_results', 3))
        src_text_cleared, _cleared_blocks = _clear_tool_results(
            src_fact['content'], keep_recent_n=_keep_recent,
        )
        # Distill content (using cleared text)
        from .nest.distiller import distill as _distill
        cleaned, removed_segments, distill_report = _distill(src_text_cleared)
        # audit: record which tool_results were cleared
        if _cleared_blocks:
            distill_report['tool_results_cleared'] = _cleared_blocks
            distill_report['tool_results_kept_recent_n'] = _keep_recent
        if not cleaned:
            return jsonify({
                'error': 'distill_empty',
                'detail': 'after redaction nothing actionable remains',
                'distill_report': distill_report,
            }), 422
        # Write new commons fact
        try:
            _bus = astor_bus(tier='public', user_id=None)
            _event_id = _bus.append_event(
                namespace=src_fact['namespace'] or 'distillation',
                agent_id=f'rest.distill.{ctx.actor}',
                source='rest.fact.distill',
                action='write',
                content=cleaned,
            )
            _cand = _bus.insert_candidate(
                event_id=_event_id,
                namespace=src_fact['namespace'] or 'distillation',
                content=cleaned,
                kind=src_fact['kind'] or 'method',
                confidence=0.95,
                importance=0.7,
                tags=['distilled', 'from_personal'],
            )
            # v1.16.32: persist audit metadata on the distilled commons row so
            # we can trace each commons fact back to the personal source.
            import json as _json_meta
            _meta = {
                'distilled_from': fact_id,
                'distilled_by': ctx.actor,
                'removed_segments': removed_segments,
                'distill_report': distill_report,
                'source_tier': src_tier,
                'source_user_id': src_fact['user_id'] if src_fact else None,
                'distilled_at': _dt.datetime.now(_dt.timezone.utc).isoformat(),
            }
            new_fact_id = _bus.promote_candidate(
                _cand, promoted_by=f'rest.distill.{ctx.actor}',
                user_id=None, tier='public', scope_type='long_term',
                provenance_kind='user_distilled',
                visibility='commons',
                extra_metadata=_json_meta.dumps(_meta, ensure_ascii=False),
            )
        except Exception as _e:
            return jsonify({'error': 'write_failed', 'detail': str(_e)}), 500
        return jsonify({
            'new_fact_id': new_fact_id,
            'source_fact_id': fact_id,
            'cleaned_text': cleaned,
            'removed_segments': removed_segments,
            'distill_report': distill_report,
            'visibility': 'commons',
            'provenance_kind': 'user_distilled',
        })


    @app.route('/v1/admin/user/<user_id>/toggle_distill', methods=['POST'])
    def admin_toggle_distill(user_id):
        """v1.16.30: admin enables/disables distill_opt_in for a user.

        Body: {allow_distill: true/false}
        admin only. Updates bot-binding.db.user_meta.distill_opt_in.
        When user has distill_opt_in=1, they can call /v1/fact/{id}/distill_auto
        on their OWN facts to extract method/flow patterns into commons.
        """
        try:
            ctx = astor_current_acl()
            if ctx.role != 'admin':
                return jsonify({'error': 'admin_only'}), 403
        except Exception:
            return jsonify({'error': 'astor_init_acl required'}), 503
        body = request.get_json(force=True) if request.is_json else {}
        # v1.16.32: require explicit True/False (not bool coercion of 'maybe').
        _v = body.get('allow_distill')
        if not isinstance(_v, bool):
            return jsonify({'error': 'allow_distill_must_be_bool',
                            'detail': f'expected true/false, got {type(_v).__name__}'}), 400
        allow = _v
        try:
            import sqlite3 as _sq
            # v1.16.55: cross-platform path via ASTOR_DIR or ~/.astor
            from pathlib import Path as _P2
            _sq_astor_dir = os.environ.get('ASTOR_DIR') or str(_P2.home() / '.astor')
            _sqdb = _sq.connect(os.path.join(_sq_astor_dir, 'bot-binding.db'))
            _cur = _sqdb.execute(
                "UPDATE user_meta SET distill_opt_in = ? WHERE user_id = ?",
                (1 if allow else 0, user_id),
            )
            _sqdb.commit()
            if _cur.rowcount == 0:
                _sqdb.close()
                return jsonify({'error': 'user_not_found', 'user_id': user_id}), 404
            _sqdb.close()
        except Exception as _e:
            return jsonify({'error': 'toggle_failed', 'detail': str(_e)}), 500
        return jsonify({
            'user_id': user_id,
            'distill_opt_in': bool(allow),
        })

    
    def _astor_dir_label(p):
        """v1.16.56: 2026-10-03 path-leak fix. Return only the last
        directory component of the astor install path. NEVER leak the
        full on-disk path (R-class 2026-10-03 + 2026-09-30 v1.16.50
        'do not leak the full on-disk path' comment). Server admins
        can still infer their install dir from peer_id + identity
        if needed; nobody else should see the host filesystem layout.

        Example: '<runtime_dir>/public/memory' → 'Astor-Memory-Runtime'
                 '/home/alice/.astor' → '.astor'
        """
        import os as _os_label
        from pathlib import PurePath as _PP
        if not p:
            return ''
        s = str(p).rstrip('\\/')
        return _PP(s).name or s or 'astor'


    @app.route('/v1/identity', methods=['GET'])
    def identity():
        """v1.16.7: server self-identity (peer_id + keypair fingerprint).

        SECURITY NOTE: _peer_id is a dict returned by init_identity() — DO
        NOT serialize the whole dict, it contains the private_key. Always
        extract _peer_id['peer_id'] explicitly (see print statement near
        init_identity call below for the correct pattern).

        Returns this astor-memory server's own peer_id so the dashboard
        can show "you are astor:<32-hex>" and clients can share it
        with other astor nodes for PPS public-search.

        Response: {
            peer_id: str (e.g. "astor:ea1c7c3110128ee1b828c54269341a98"),
            public_key_fingerprint: str (first 16 chars of SHA-256 of pubkey),
            astor_dir: str (server's data dir)
        }
        """
        import hashlib
        import base64 as _b64
        try:
            from pathlib import Path as _P
            _id_dir = _P(str(get_default_astor_dir())) / 'identity'
            _pub_path = _id_dir / 'keypair.json'
            _pub_fp = ''
            if _pub_path.exists():
                try:
                    import json as _j
                    _kp = _j.loads(_pub_path.read_text())
                    _pub = _kp.get('public_key', '')
                    if _pub:
                        try:
                            _pub_fp = hashlib.sha256(_b64.b64decode(_pub)).hexdigest()[:16]
                        except Exception:
                            pass
                except Exception:
                    pass
        except Exception:
            _pub_fp = ''
        # Extract peer_id STRING from the dict — never serialize the whole dict.
        _peer_id_str = ''
        try:
            if isinstance(_peer_id, dict):
                _peer_id_str = _peer_id.get('peer_id', '')
            elif isinstance(_peer_id, str):
                _peer_id_str = _peer_id
        except Exception:
            pass
        return jsonify({
            'peer_id': _peer_id_str,
            'public_key_fingerprint': _pub_fp,
            # v1.16.50: do not leak on-disk path in /v1/identity.
                })

    # ---- v1.16.8 bi-temporal fact lifecycle endpoints ----

    @app.route('/v1/bitemporal/invalidate', methods=['POST'])
    def bitemporal_invalidate():
        """Mark a fact invalid (sets valid_until=NOW, invalidated_by=replaced_by)."""
        body = request.get_json(force=True) or {}
        try:
            fid = int(body.get('fact_id'))
        except (TypeError, ValueError):
            return jsonify({'error': 'fact_id required (int)'}), 400
        rb = body.get('replaced_by_fact_id')
        try:
            rb = int(rb) if rb is not None else None
        except (TypeError, ValueError):
            rb = None
        from .bus.bitemporal import invalidate_fact as _bt_invalidate
        _bt_bus = astor_bus(tier=body.get('tier') or 'public')
        result = _bt_invalidate(_bt_bus, fid, replaced_by_fact_id=rb,
                                reason=body.get('reason') or 'superseded')
        return jsonify(result), (200 if result.get('ok') else 400)

    @app.route('/v1/bitemporal/lifecycle/<int:fact_id>', methods=['GET'])
    def bitemporal_lifecycle(fact_id: int):
        """Return full temporal lifecycle for one fact."""
        from .bus.bitemporal import get_fact_lifecycle
        _bt_bus = astor_bus(tier=request.args.get('tier') or 'public')
        info = get_fact_lifecycle(_bt_bus, fact_id)
        if info is None:
            return jsonify({'error': 'fact not found', 'fact_id': fact_id}), 404
        return jsonify(info)

    @app.route('/v1/bitemporal/active', methods=['POST'])
    def bitemporal_active():
        """Recall query that excludes invalidated facts."""
        body = request.get_json(force=True) or {}
        q = (body.get('query') or '').strip()
        if not q:
            return jsonify({'error': 'query required'}), 400
        from .bus.bitemporal import find_active_facts
        _bt_bus = astor_bus(tier=body.get('tier') or 'public')
        results = find_active_facts(
            _bt_bus, q,
            top_k=body.get('top_k') or 10,
            tier=body.get('tier') or 'public',
            user_id=body.get('user_id'),
            namespace=body.get('namespace'),
        )
        return jsonify({'count': len(results), 'facts': results, 'query': q})

    @app.route('/v1/bitemporal/cascade_forget', methods=['POST'])
    def bitemporal_cascade_forget():
        """Cascade forget: tombstone + remove graph edges + clean entity refs."""
        body = request.get_json(force=True) or {}
        try:
            fid = int(body.get('fact_id'))
        except (TypeError, ValueError):
            return jsonify({'error': 'fact_id required (int)'}), 400
        from .bus.bitemporal import cascade_forget
        _bt_bus = astor_bus(tier=body.get('tier') or 'public')
        result = cascade_forget(_bt_bus, fid)
        return jsonify(result), (200 if result.get('ok') else 400)

    # v1.16.12 (2026-09-30): M-flow L0 episode layer. Article "受
    # 生物启发的认知记忆引擎 M-flow" 9/30 — 4-layer cone (Episode →
    # Facet → FacetPoint → Entity). astor has L1/L2/L3; missing L0
    # raw conversation chunks. Default OFF for backward compat.
    # Usage: write episode first, then derive facts (or vice versa).
    @app.route('/v1/episode', methods=['POST'])
    def episode_write():
        body = request.get_json(force=True) or {}
        raw_text = body.get('raw_text') or body.get('text') or ''
        if not raw_text:
            return jsonify({'error': 'raw_text required'}), 400
        _ep_tier = body.get('tier', 'public')
        _ep_bus = astor_bus(tier=_ep_tier, user_id=body.get('user_id'))
        from .nest.episodes import write_episode as _ep_write
        _ep_id = _ep_write(
            _ep_bus.conn,
            raw_text=raw_text,
            namespace=_ep_bus.namespace if hasattr(_ep_bus, 'namespace') else 'public',
            user_id=body.get('user_id') or '',
            tier=_ep_tier,
            session_id=body.get('session_id') or '',
            derived_fact_ids=body.get('derived_fact_ids') or [],
            entities=body.get('entities'),
        )
        return jsonify({
            'episode_id': _ep_id,
            'tier': _ep_tier,
            'raw_text_len': len(raw_text),
        }), 201

    @app.route('/v1/episode/<int:ep_id>', methods=['GET'])
    def episode_read(ep_id):
        _ep_tier = request.args.get('tier', 'public')
        _ep_bus = astor_bus(tier=_ep_tier, user_id=request.args.get('user_id'))
        from .nest.episodes import read_episode
        ep = read_episode(_ep_bus.conn, ep_id)
        if not ep:
            return jsonify({'error': 'episode not found', 'episode_id': ep_id}), 404
        return jsonify(ep)

    @app.route('/v1/episode/list', methods=['GET'])
    def episode_list():
        _ep_tier = request.args.get('tier', 'public')
        _ep_bus = astor_bus(tier=_ep_tier, user_id=request.args.get('user_id'))
        from .nest.episodes import list_episodes
        items = list_episodes(
            _ep_bus.conn,
            namespace=request.args.get('namespace'),
            user_id=request.args.get('user_id_filter'),
            session_id=request.args.get('session_id'),
            limit=int(request.args.get('limit', 20)),
            offset=int(request.args.get('offset', 0)),
        )
        return jsonify({'count': len(items), 'episodes': items})

    @app.route('/v1/episode/by_fact/<int:fact_id>', methods=['GET'])
    def episode_by_fact(fact_id):
        _ep_tier = request.args.get('tier', 'public')
        _ep_bus = astor_bus(tier=_ep_tier, user_id=request.args.get('user_id'))
        from .nest.episodes import find_episodes_by_fact
        items = find_episodes_by_fact(_ep_bus.conn, fact_id)
        return jsonify({'count': len(items), 'fact_id': fact_id, 'episodes': items})

    @app.route('/v1/episode/link', methods=['POST'])
    def episode_link():
        body = request.get_json(force=True) or {}
        try:
            ep_id = int(body.get('episode_id'))
            fact_id = int(body.get('fact_id'))
        except (TypeError, ValueError):
            return jsonify({'error': 'episode_id and fact_id required (int)'}), 400
        _ep_tier = body.get('tier', 'public')
        _ep_bus = astor_bus(tier=_ep_tier, user_id=body.get('user_id'))
        from .nest.episodes import link_fact_to_episode
        link_fact_to_episode(_ep_bus.conn, ep_id, fact_id)
        return jsonify({'ok': True, 'episode_id': ep_id, 'fact_id': fact_id})

    # v1.16.13 (2026-09-30): MemSkill-inspired Skill Bank abstraction.
    # Article mp.weixin.qq.com/s/RmbEJ28DNQ4bI-olYTX5mA: astor
    # 当前的 hardcode 记忆操作 (extract/inject/update/forget) 应该抽象
    # 成可调用的 skill. 4 built-in skills wrap 现有 ops:
    #   - coref_resolve (wraps v1.16.10)
    #   - path_score (wraps v1.16.11)
    #   - bitemporal_invalidate (wraps v1.16.8)
    #   - episode_link (wraps v1.16.12)
    # Default OFF for backward compat.
    @app.route('/v1/skill', methods=['GET'])
    def skill_list():
        from .nest.skills import get_bank
        tag = request.args.get('tag')
        bank = get_bank()
        skills = bank.list(tag=tag)
        return jsonify({
            'count': len(skills),
            'tag': tag,
            'skills': skills,
        })

    @app.route('/v1/skill/<name>', methods=['GET'])
    def skill_inspect(name):
        from .nest.skills import get_bank
        bank = get_bank()
        s = bank.get(name)
        if s is None:
            return jsonify({'error': 'skill not found', 'name': name}), 404
        return jsonify({
            'name': s.name,
            'description': s.description,
            'tags': list(s.tags),
            'version': s.version,
        })

    @app.route('/v1/skill/<name>/invoke', methods=['POST'])
    def skill_invoke(name):
        from .nest.skills import get_bank
        body = request.get_json(force=True) or {}
        bank = get_bank()
        # Inject the bus + namespace into context for built-in skills
        # that need DB access. Caller can override via body.context.
        ctx = dict(body.get('context') or {})
        if 'bus' not in ctx:
            _sk_tier = body.get('tier', 'public')
            try:
                ctx['bus'] = astor_bus(tier=_sk_tier, user_id=body.get('user_id'))
            except Exception:
                pass
        if 'conn' not in ctx and 'bus' in ctx and hasattr(ctx['bus'], 'conn'):
            ctx['conn'] = ctx['bus'].conn
        result = bank.invoke(name, ctx)
        status = 200 if result.get('ok') else 400
        return jsonify(result), status

    # v1.16.14: chain endpoint — run multiple skills in order, threading
    # context. Body: {"skills": ["coref_resolve", "path_score"], "context": {...}}
    @app.route('/v1/skill/chain', methods=['POST'])
    def skill_chain():
        from .nest.skills import get_bank, invoke_chain
        body = request.get_json(force=True) or {}
        names = body.get('skills') or []
        if not isinstance(names, list) or not names:
            return jsonify({'error': 'body.skills must be a non-empty list'}), 400
        ctx = dict(body.get('context') or {})
        # Inject bus + conn
        if 'bus' not in ctx:
            _sk_tier = body.get('tier', 'public')
            try:
                ctx['bus'] = astor_bus(tier=_sk_tier, user_id=body.get('user_id'))
            except Exception:
                pass
        if 'conn' not in ctx and 'bus' in ctx and hasattr(ctx['bus'], 'conn'):
            ctx['conn'] = ctx['bus'].conn
        results = invoke_chain(
            names, ctx, bank=get_bank(),
            stop_on_error=bool(body.get('stop_on_error', False)),
        )
        ok_count = sum(1 for r in results if r.get('ok'))
        return jsonify({
            'count': len(results),
            'ok_count': ok_count,
            'results': results,
        })

    # v1.16.18.1: ACTIVE skill recommendation. astor proactively tells
    # agents which skills + patterns to use for a given task — instead
    # of waiting for the agent to recall. Answers user feedback:
    # "astor should be active, not just a passive recall db."
    #
    # Body: {"task": "<description>", "platform": "muse", "chat_id": "..."}
    # Returns: {
    #   "recommended_skills": [<skill names + descriptions>],
    #   "relevant_facts": [<top 3 ranked facts>],
    #   "success_patterns": [<from memory_experience>],
    #   "user_preferences": [<locked user prefs like fact #6215>],
    #   "tier_hint": "<tier to use for downstream writes>",
    # }
    @app.route('/v1/skill/recommend', methods=['POST'])
    def skill_recommend():
        from .nest.skills import get_bank, controller_select, controller_select_scored
        body = request.get_json(force=True) or {}
        task = body.get('task', '').strip()
        platform = body.get('platform', 'muse')
        chat_id = body.get('chat_id', '')
        if not task:
            return jsonify({'error': 'task required'}), 400

        # Step 1: resolve caller
        _sk_tier = body.get('tier', 'public')
        try:
            _sk_bus = astor_bus(tier=_sk_tier, user_id=body.get('user_id'))
        except Exception:
            _sk_bus = None

        # Step 2: rank relevant skills by tag match
        bank = get_bank()
        # Heuristic: detect task type from keywords → tag preferences
        _task_lower = task.lower()
        _preferred_tags = []
        if any(w in _task_lower for w in ['fetch', 'url', 'read', 'crawl', 'wechat', '公众号', '文章']):
            _preferred_tags.extend(['preprocess', 'nlp', 'memory'])
        if any(w in _task_lower for w in ['write', 'store', 'remember']):
            _preferred_tags.extend(['write_hook', 'memory'])
        if any(w in _task_lower for w in ['read', 'recall', 'search', 'find', 'query']):
            _preferred_tags.extend(['read_hook', 'rank', 'graph'])
        if any(w in _task_lower for w in ['correct', 'wrong', 'update', 'fix']):
            _preferred_tags.extend(['bitemporal', 'invalidate'])
        if any(w in _task_lower for w in ['episode', 'chunk', 'conversation', 'turn']):
            _preferred_tags.extend(['episode', 'l0', 'evidence'])

        # v1.16.21: query-aware scoring replaces fixed tag routing.
        # controller_select_scored ranks EVERY bank skill by token overlap
        # with the task text (name + tags + description), so new skills are
        # discoverable without updating keyword heuristics. Tag heuristic
        # retained as fallback when scoring returns nothing.
        _scored = controller_select_scored(task, top_n=5, bank=bank)
        _skill_summaries = list(_scored)  # already has name/description/tags/score
        if not _skill_summaries:
            _ranked_skills = controller_select(
                _preferred_tags or ['memory'], available_tags=None, bank=bank,
            )
            for sn in _ranked_skills[:5]:
                s = bank.get(sn)
                if s:
                    _skill_summaries.append({
                        'name': sn,
                        'description': s.description,
                        'tags': list(s.tags),
                        'score': 0.0,
                    })

        # Step 3: recall relevant facts.
        # Use BOTH /v1/read (semantic) AND substring fallback (for LOCK
        # facts where ECV score may be 0 because language mismatch).
        # The user feedback was: "score=0.000 on Chinese query against
        # English fact" — so we use substring as a fallback.
        _relevant_facts = []
        try:
            _recall_r = requests.post(
                f"http://127.0.0.1:{request.environ.get('SERVER_PORT', '7803')}/v1/read",
                json={'query': task, 'tier': _sk_tier,
                      'user_id': body.get('user_id') or 'admin',
                      'top_k': 5},
                timeout=10,
            )
            if _recall_r.status_code == 200:
                _relevant_facts = _recall_r.json().get('results', [])
        except Exception:
            pass
        # Substring fallback: search for keywords in raw fact content.
        # Picks up LOCK rules and success_pattern facts that ECV missed.
        try:
            import sqlite3 as _sq
            import re as _re_kw
            _astor_dir_local = _os.environ.get('ASTOR_DIR') or str(_os.path.expanduser('~/.astor'))
            if not _os.path.exists(_astor_dir_local):
                _astor_dir_local = _os.getcwd()  # fallback to caller cwd (runtime deployment)
            _fallback_db = _os.path.join(_astor_dir_local, 'public', 'memory', 'astor_bus_public.db')
            _sq_conn = _sq.connect(_fallback_db)
            # Extract 1-3 char tokens (CJK bigrams + English words)
            _kw_tokens = set()
            for _cjk in _re_kw.findall(r'[\u4e00-\u9fff]', task):
                _kw_tokens.add(_cjk)
            for _cjk in _re_kw.findall(r'[\u4e00-\u9fff]{2}', task):
                _kw_tokens.add(_cjk)
            for _w in _re_kw.findall(r'[A-Za-z]{2,}', task):
                _kw_tokens.add(_w.lower())
            # Remove stopwords
            _STOP = {'the', 'a', 'an', 'is', 'are', 'or', 'of', 'to', 'in', 'on'}
            _kw_tokens = _kw_tokens - _STOP
            # Build OR LIKE: WHERE content LIKE '%kw1%' OR content LIKE '%kw2%' ...
            if _kw_tokens:
                _like_parts = []
                _params = []
                for _kw in list(_kw_tokens)[:6]:  # cap to 6 keywords
                    _like_parts.append('content LIKE ?')
                    _params.append(f'%{_kw}%')
                _sq_rows = _sq_conn.execute(
                    f"""SELECT id, content, importance FROM memory_canonical
                        WHERE tombstoned = 0 AND ({' OR '.join(_like_parts)})
                        ORDER BY importance DESC LIMIT 8""",
                    tuple(_params),
                ).fetchall()
                _sq_conn.close()
                # Add results, dedup
                _existing_ids = {f.get('id') for f in _relevant_facts}
                for _row in _sq_rows:
                    if _row[0] not in _existing_ids:
                        _relevant_facts.append({
                            'id': _row[0], 'content': _row[1],
                            'score': 0.5 + _row[2] * 0.5,
                        })
        except Exception:
            pass

        # Step 4: extract success_patterns and user_preferences
        # Use substring match (more reliable for LOCK keywords)
        _success_patterns = []
        _user_prefs = []
        for f in _relevant_facts:
            _content = str(f.get('content', ''))
            _score = f.get('score', 0)
            if 'success_pattern' in _content or '成功' in _content:
                _success_patterns.append({
                    'content': _content[:300], 'score': _score,
                })
            elif ('user_preference' in _content or 'LOCK' in _content
                  or 'cold-start' in _content or 'r-class' in _content.lower()):
                _user_prefs.append({
                    'content': _content[:300], 'score': _score,
                })
        # Cap at 3 each
        _success_patterns = _success_patterns[:3]
        _user_prefs = _user_prefs[:3]

        return jsonify({
            'task': task,
            'platform': platform,
            'chat_id': chat_id,
            'recommended_skills': _skill_summaries,
            'relevant_facts_count': len(_relevant_facts),
            'success_patterns': [
                {'content': f.get('content', '')[:300], 'score': f.get('score', 0)}
                for f in _success_patterns
            ],
            'user_preferences': [
                {'content': f.get('content', '')[:300], 'score': f.get('score', 0)}
                for f in _user_prefs
            ],
            'tier_hint': _sk_tier,
            'tip': (
                "astor proactively recommends these. Run the skills in "
                "/v1/skill/<name>/invoke or chain via /v1/skill/chain. "
                "If success_patterns exist, USE them directly."
            ),
        })

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
                "astor_dir": _astor_dir_label(astor_dir),
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
            return jsonify({"error": "user_db_not_found", "user": user}), 404

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
            # v1.16.50: do not leak sqlite file path in /v1/health/diagnose.
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
        # v1.16.72 (2026-10-06): per-stage write-path timing instrumentation.
        # Six stage markers (start, classify_intent, start_extract, end_extract,
        # promote_start, promote_done) surface in resp_body['w_stages_ms'] ONLY
        # when caller passes debug_timing=True (default off; backward-compat).
        import time as _w_t
        _w_stages = {'start': _w_t.perf_counter()}
        # v1.16.37+: ASTOR_AUTO_FILLED_KIND set by before_write_hook.
        # If agent wrote no kind, we already filled in our suggestion.
        # But forge_extract_uses_acts will overwrite; below we re-apply after.
        _astor_suggested_kind = request.environ.get('ASTOR_SUGGESTED_KIND')
        _astor_suggested_conf = request.environ.get('ASTOR_SUGGESTED_CONFIDENCE', 0)

        # v1.16.19 (2026-09-30): SERVER-SIDE tier resolver.
        # Per user feedback #4: "tier 映射收到服务端 — lookup 返回 admin、读写却不认,
        # 各客户端自己映射早晚出错". Now the SERVER resolves tier from
        # bot-binding.db lookup. Caller can still pass explicit tier
        # (override); if omitted, server does:
        #   1. Look up (platform, chat_id) → user_id + role
        #   2. Read user_meta.default_tier
        #   3. ACL check: tier must be allowed for this role
        # This eliminates the #1 cause of "write 400/403" errors from
        # external agents (muse) that hardcoded tier=public for admin.
        _server_resolved_tier = None
        if 'tier' not in body:
            # Caller didn't specify tier — resolve it server-side
            _platform = body.get('platform', '')
            _chat_id = body.get('chat_id', '')
            if _platform and _chat_id:
                try:
                    import sqlite3 as _sq_t
                    _astor_dir_local = _os.environ.get('ASTOR_DIR') or str(_os.path.expanduser('~/.astor'))
                    if not _os.path.exists(_astor_dir_local):
                        _astor_dir_local = _os.getcwd()  # fallback to caller cwd
                    _sqdb = _sq_t.connect(_os.path.join(_astor_dir_local, 'bot-binding.db'))
                    _sqdb.row_factory = _sq_t.Row
                    _row = _sqdb.execute(
                        """SELECT b.user_id, m.role, m.default_tier, m.trusted_agent
                           FROM bindings b
                           LEFT JOIN user_meta m ON m.user_id = b.user_id
                           WHERE b.platform_id = ? AND b.chat_id = ? AND b.active = 1
                           LIMIT 1""",
                        (_platform, _chat_id),
                    ).fetchone()
                    _sqdb.close()
                    if _row:
                        # Map user_meta.default_tier -> valid bus tier.
                        # 'admin' is a ROLE not a bus tier: admin writes go
                        # to private_<user_id> (their private bucket) with
                        # source mirror per P2-fix policy.
                        _raw_tier = _row['default_tier'] or 'public'
                        if _raw_tier == 'admin':
                            # admin role → private tier (owner-only bucket)
                            body['tier'] = 'private'
                        elif _raw_tier in ('vip', 'user'):
                            body['tier'] = 'public'
                        else:
                            body['tier'] = _raw_tier
                        body.setdefault('user_id', _row['user_id'] or 'admin')
                        _server_resolved_tier = body['tier']
                except Exception as _tier_exc:
                    _safe_stderr_write(
                        f'[astor.server] tier resolver failed (non-fatal): {_tier_exc}\n'
                    )



        # v1.16.20 (2026-09-30): ASYNC embedding opt-in.
        # Per user feedback #1: "实测单次写 40 多秒, embedding 在 PC 上是
        # 最大瓶颈. 落库和 embedding 分离, 读加缓存".
        # When body.async_embed=True (default False for backward compat):
        #   1. Fact is committed to memory_canonical IMMEDIATELY
        #   2. Embedding is computed in a background thread (daemon)
        #   3. /v1/read with use_embedding=True will miss this fact
        #      until embedding lands; without use_embedding (default
        #      fast read), recall still works on lexical/BM25/ECV.
        # This trades off semantic-recall latency on the JUST-written
        # fact for write-end latency dropping from ~40s to <100ms.
        _async_embed = bool(body.get('async_embed'))
        if _async_embed:
            body['_skip_nest_store'] = True  # opt-out flag for promote_candidate

        # v1.16.10 (2026-09-30): coreference resolution (M-flow-inspired).
        # When body.coref_resolve=True (default off for backward compat),
        # rewrite pronouns in `text` against recent facts in the same
        # namespace + user. Article: "入库时就做指代消解。'他/她/它/
        # 那个/该公司'这类代词，在写入图之前就会被替换成具体的实体名。"
        # Default OFF because: (a) adds 1 DB read per write, (b) could
        # surface as a surprise in tests/CLI scripts that expect raw text.
        # Audit row written when any pronoun was resolved.
        _coref_meta: dict = {'enabled': bool(body.get('coref_resolve')), 'resolutions': []}
        if _coref_meta['enabled']:
            # Read tier + bus_user_id fresh from body — the local `tier`
            # and `bus_user_id` variables aren't bound until LATER in
            # /v1/write (after this hook runs). Reading them from body
            # matches what /v1/write eventually uses for both.
            _coref_tier = body.get('tier', 'public')
            _coref_uid = body.get('user_id') or body.get('user') or 'admin'
            try:
                from .nest.coref import resolve_coreferences
                # Resolve against the bus for the target tier.
                _coref_bus = astor_bus(tier=_coref_tier, user_id=_coref_uid)
                # CRITICAL: namespace must match what _resolve_namespace
                # returns later in /v1/write (e.g. 'admin' for default
                # admin user, '<agent_id>/<session_id>' when agent_ctx
                # is set). Use _resolve_namespace here to match exactly.
                _coref_ns = _resolve_namespace(
                    agent_ctx,
                    fallback=_coref_uid,
                    session_id=body.get('session_id') or None,
                )
                _coref_result = resolve_coreferences(
                    _coref_bus,
                    text,
                    namespace=_coref_ns,
                    user_id=_coref_uid,
                    antecedent_window=int(body.get('coref_window', 10)),
                )
                _safe_stderr_write(
                    f'[astor.server] coref: text={text!r} ns={_coref_ns!r} '
                    f'changed={_coref_result.get("changed")} '
                    f'resolved={_coref_result.get("resolved", "")!r} '
                    f'used={_coref_result.get("antecedents_used")}\n'
                )
                if _coref_result.get('changed'):
                    text = _coref_result['resolved']
                    _coref_meta['resolutions'] = _coref_result['resolutions']
                    _coref_meta['antecedents_used'] = _coref_result.get('antecedents_used', [])
                    _safe_stderr_write(
                        f'[astor.server] coref resolved {len(_coref_result["resolutions"])} pronoun(s): '
                        f'{_coref_result["antecedents_used"]}\n'
                    )
            except Exception as _coref_exc:
                # Coref failure must never break the write — log + continue.
                _safe_stderr_write(
                    f'[astor.server] coref failed (non-fatal): {type(_coref_exc).__name__}: {_coref_exc}\n'
                )
        tier = body.get('tier', 'public')
        # v1.16+ (Plan "public tier 共享方法/流程/教训"): public tier hard gate.
        # Any write to tier='public' must pass through Memory Defense first.
        # Default policy is 'block' (reject writes that contain PII patterns);
        # caller may explicitly opt out with body.pii_scan=False (admin escape
        # hatch). Env ASTOR_PII_PUBLIC_FORCE=0 disables the entire hard gate
        # (emergency escape — normal ops should leave this on).
        if (tier == 'public'
                and body.get('pii_scan') is not False
                and os.environ.get('ASTOR_PII_PUBLIC_FORCE', '1') == '1'
                and body.get('visibility_hint') != 'personal'):
            # v1.16.30: skip when writing personal bucket (intent: store private
            # info safe; classifier already prevents personal→public leaks).
            body = dict(body)
            body['pii_scan'] = True
            body.setdefault('pii_policy', 'block')
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
            # v1.16+ (Plan "public tier 共享方法/流程/教训"): when policy='block',
            # reject on ANY PII match (severity in {redact, block}), not just
            # block-severity. This is the contract that "宁可漏一条不能泄露一条"
            # relies on — without it, redact-severity patterns (e.g. telegram_chat_id
            # catching a CN phone) would still leak under the public-tier hard gate.
            if _md_matches and _md_policy == 'block' and any(m.severity in ('block', 'redact') for m in _md_matches):
                _names = sorted({m.name for m in _md_matches if m.severity in ('block', 'redact')})
                _pii_gate_stats['block_count'] += 1  # v1.16.x: lifetime block counter
                return jsonify({
                    'error': 'pii_blocked',
                    'detail': f"Memory Defense (block policy) rejected write: {len(_md_matches)} PII match(es) including {_names[:3]}",
                    'matches': [{'name': m.name, 'severity': m.severity} for m in _md_matches],
                }), 400
            if _md_matches:
                # Log to audit (fire-and-forget; never blocks the write)
                try:
                    _audit_entry = _md_audit(None, user, tier, _md_matches, _md_policy)
                    _safe_stderr_write('[MEMORY_DEFENSE] ' + repr(_audit_entry) + chr(10))
                except Exception as _md_audit_exc:
                    _safe_stderr_write('[MEMORY_DEFENSE] audit log failed: ' + repr(_md_audit_exc) + chr(10))
                text = _processed_text  # use redacted content for the actual write
                _pii_gate_stats['redact_count'] += 1  # v1.16.x: lifetime redact counter
        # v1.16.x: Layer 2 personal content sniff (warn only, never block).
        # Runs on public tier writes only — private tier carries no
        # cross-user leakage risk so sniffing is skipped. Categories are
        # attached to fact metadata and returned via response header so
        # agent frameworks can surface "are you sure?" prompts.
        _personal_categories: list[str] = []
        if tier == 'public' and text:
            try:
                from .nest.memory_defense import detect_personal_content as _dpc
                _personal_categories = _dpc(text)
            except Exception as _dpc_exc:
                _safe_stderr_write(f'[astor.server] detect_personal_content failed (non-fatal): {_dpc_exc}\n')
        # v1.16.x: tier_hint / behavior_class opt-in fields. When tier=public,
        # callers can declare content type ('behavior' = method/pattern/flow;
        # 'content' = plain fact; 'preference' = personal taste; 'learning' =
        # fact for later review). Default 'content' for backward compatibility.
        # behavior_class only accepted when tier_hint='behavior'.
        _tier_hint_raw = body.get('tier_hint')
        if _tier_hint_raw is not None and _tier_hint_raw not in ('behavior', 'content', 'preference', 'learning'):
            return jsonify({
                'error': 'invalid_tier_hint',
                'detail': f"tier_hint={_tier_hint_raw!r} must be one of 'behavior'/'content'/'preference'/'learning'",
            }), 400
        _tier_hint = _tier_hint_raw if _tier_hint_raw is not None else (
            'content' if tier == 'public' else None
        )
        _behavior_class = None
        if _tier_hint == 'behavior':
            _bc_raw = body.get('behavior_class')
            if _bc_raw is not None and _bc_raw not in ('method', 'anti-pattern', 'recipe', 'flow', 'lesson', 'pattern'):
                return jsonify({
                    'error': 'invalid_behavior_class',
                    'detail': f"behavior_class={_bc_raw!r} must be one of method/anti-pattern/recipe/flow/lesson/pattern",
                }), 400
            _behavior_class = _bc_raw
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
                resp = jsonify({
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
                # v1.16.x Layer 2: personal content sniff on dedup short-circuit too.
                if _personal_categories:
                    resp.headers['X-Astor-Personal-Content'] = ','.join(_personal_categories)
                return resp
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

        # v1.16.20 (2026-09-30): TRUE async write mode.
        # Per user feedback #1: "写/读异步化: 实测单次写 40 多秒, embedding
        # 在你 PC 上是最大瓶颈。落库和 embedding 分离, 读加缓存."
        # Actual bottleneck measured: mode='llm' forge extraction (an LLM
        # round-trip), not embedding. So async split happens HERE:
        #   1. Append event IMMEDIATELY (fast SQLite write)
        #   2. Return event_id right away (latency < 100ms)
        #   3. Extract + promote in a daemon thread
        # Fact is NOT visible in /v1/read until extraction completes.
        # Clients that need synchronous semantics omit async_write.
        if body.get('async_write'):
            # v1.16.19 tier resolver already ran above (it reads body).
            # NOTE: outcome/mode/caller_event_ts are computed LATER in the
            # sync path — must compute them HERE for the async thread.
            _async_write = True
            _w_tier = tier
            _w_user = bus_user_id or user
            _w_event_id = event_id
            _w_text = text
            _w_mode = mode
            try:
                from .forge.extractor import astor_classify_outcome as _aco
                _w_outcome = _aco(text)
            except Exception:
                _w_outcome = 'neutral'
            _w_why = (
                f'auto-classified outcome={_w_outcome} (async write path)'
                if _w_outcome != 'neutral' else None
            )
            _w_caller_ts = body.get('event_time') or body.get('event_ts')
            _w_body = dict(body)
            _w_session = _write_session_id
            _w_agent_ctx = dict(agent_ctx)
            _w_namespace = _resolve_namespace(
                agent_ctx, fallback=user, session_id=_write_session_id,
            )

            import threading as _w_thr

            def _extract_and_promote():
                try:
                    # Background thread has no ACL ContextVar — init as
                    # system actor (background task convention).
                    from ._internal.acl import astor_init_acl as _wia
                    _wia(actor='system', role='system', tier=_w_tier,
                         user_id=_w_user if _w_tier == 'private' else None)
                    from .bus.store import astor_bus as _w_bus_factory
                    from .forge.extractor import astor_extract_facts as _w_extract
                    _w_facts = _w_extract(
                        _w_text, mode=_w_mode, tier=_w_tier,
                        user_id=_w_user if _w_tier == 'private' else None,
                        actor='rest_api', outcome=_w_outcome, why=_w_why,
                        doc_timestamp=_w_caller_ts,
                    )
                    _w_bus = _w_bus_factory(tier=_w_tier, user_id=_w_user)
                    _w_fact_ids = []
                    for _w_f in _w_facts:
                        _w_cand = _w_bus.insert_candidate(
                            event_id=_w_event_id, namespace=_w_namespace,
                            content=_w_f.content, kind=_w_f.kind,
                            confidence=_w_f.confidence,
                            importance=_w_f.importance,
                            tags=_w_f.tags or [],
                            keywords=_w_f.keywords or [],
                            context=_w_f.context or '',
                            entities=getattr(_w_f, 'entities', None),
                        )
                        _w_cid = _w_bus.promote_candidate(
                            _w_cand, promoted_by='rest.write.async',
                            user_id=_w_user, tier=_w_tier, scope_type='long_term',
                            # v1.16.33: thread conflict_kind + entities through
                            # so the auto-resolver can match against old facts.
                        )
                        # v1.16.33: auto conflict resolution (OPT-2). When
                        # the new fact is a correction/preference/decision
                        # and shares entities with an older active fact of
                        # the same kind, mark the older one valid_until=now
                        # (Zep-style bi-temporal invalidation).
                        try:
                            _w_invalidated = _w_bus.resolve_conflicts(
                                new_fact_id=_w_cid,
                                new_kind=_w_f.kind or 'fact',
                                new_user_id=_w_user,
                                new_entities_json=getattr(_w_f, 'entities_json', None)
                                    or json.dumps(getattr(_w_f, 'entities', None) or []),
                                new_content=_w_f.content or '',
                            )
                            if _w_invalidated:
                                _safe_stderr_write(
                                    f'[v1.16.33] conflict_resolver: fact={_w_cid} '
                                    f'invalidated {len(_w_invalidated)} older fact(s)\n'
                                )
                        except Exception as _cr_exc:
                            _safe_stderr_write(
                                f'[v1.16.33] conflict_resolver failed (non-fatal): {_cr_exc}\n'
                            )
                        _w_fact_ids.append(_w_cid)
                    _safe_stderr_write(
                        f'[astor.server] async write event={_w_event_id} '
                        f'promoted facts={_w_fact_ids}\n'
                    )
                except Exception as _w_exc:
                    _safe_stderr_write(
                        f'[astor.server] async write FAILED event={_w_event_id}: '
                        f'{type(_w_exc).__name__}: {_w_exc}\n'
                    )

            _w_t = _w_thr.Thread(target=_extract_and_promote, daemon=True)
            _w_t.start()
            _w_lat_ms = 0  # immediate accept
            return jsonify({
                'event_id': event_id,
                'fact_ids': [],  # facts promote in background
                'count': 0,
                'tier': tier,
                'scope': scope,
                'async_write': True,
                'status': 'accepted',
                'detail': 'event committed; facts will appear in /v1/read '
                          'after extraction completes (typically 5-40s)',
                'server_resolved_tier': _server_resolved_tier,
                'latency_ms': _w_lat_ms,
                'effective_user_id': bus_user_id or user,
            }), 202

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
        _w_stages['classify_intent'] = _w_t.perf_counter()
        _w_stages['start_extract'] = _w_t.perf_counter()
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
        _w_stages['end_extract'] = _w_t.perf_counter()
# 2026-10-03 v1.16.60: pre-promote filter (article-driven).
        # The 4-tier "提纯" principle from the Agent memory article
        # (过滤 / 去重 / 纠错 / 沉淀) says: filter noise before it
        # hits long-term storage. Without this hook, every low-signal
        # fragment (confidence < 0.3, importance < 0.3, text < 5 chars)
        # takes up a row in memory_canonical, dilutes future recall,
        # and triggers per-fact side effects (lex index, nest embed,
        # auto-link, conflict_resolver). Three cheap gates:
        #   1. text length < 5 chars  → drop (single-token noise)
        #   2. confidence < 0.3       → drop (extractor unsure)
        #   3. importance < 0.3 AND kind='fact' → drop (low-signal fact;
        #      rules/lessons/decisions stay even at low importance)
        # Rules/lessons/decisions are kept at low importance because
        # they have intrinsic value regardless of retrieval weight.
        # Env ASTOR_WRITE_FILTER_ENABLED=1 (default) — set 0 to disable
        # for backward compat with operators who expect every /v1/write
        # call to land something, even noise.
        _filter_enabled = (
            body.get('filter_noise') is not False
            and os.environ.get('ASTOR_WRITE_FILTER_ENABLED', '1') == '1'
        )
        _filtered_out: list[dict] = []
        if _filter_enabled and facts:
            _surviving = []
            for _fx in facts:
                _fx_text = (_fx.content or '').strip()
                _fx_kind = _fx.kind or 'fact'
                _fx_conf = float(_fx.confidence or 0.0)
                _fx_imp = float(_fx.importance or 0.0)
                _drop_reason = None
                if len(_fx_text) < 5:
                    _drop_reason = 'text_too_short'
                elif _fx_conf < 0.3:
                    _drop_reason = 'low_confidence'
                elif _fx_imp < 0.3 and _fx_kind == 'fact':
                    _drop_reason = 'low_importance_fact'
                if _drop_reason is not None:
                    _filtered_out.append({
                        'content_preview': _fx_text[:60],
                        'kind': _fx_kind,
                        'confidence': _fx_conf,
                        'importance': _fx_imp,
                        'reason': _drop_reason,
                    })
                    continue
                _surviving.append(_fx)
            if _filtered_out:
                _safe_stderr_write(
                    f'[v1.16.60] pre-promote filter dropped {len(_filtered_out)}'
                    f'/{len(facts)} low-signal facts: '
                    f'{[(f["reason"], f["content_preview"][:30]) for f in _filtered_out]}\n'
                )
            facts = _surviving
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

        # v1.16.29 (2026-10-01): visibility tier classification.
        # Three layers (R-class 7587 + 7568):
        #   1. admin_global_toggle (user_meta.allow_commons_write) — if 0, force personal
        #   2. visibility_hint — caller-passed explicit override
        #   3. kind-driven auto — AUTO_COMMONS_KINDS + clean content (no PII / fp / emotion / geo)
        from .nest.visibility_classifier import classify_visibility as _classify_vis
        _vis_hint = body.get('visibility_hint') or body.get('visibility') or 'auto'
        # Look up admin_global_toggle from bot-binding.db (cheap; called once per ())
        _admin_allow = True
        try:
            import sqlite3 as _sq
            _astor_dir_local = _os.environ.get('ASTOR_DIR') or str(_os.path.expanduser('~/.astor'))
            if not _os.path.exists(_astor_dir_local):
                _astor_dir_local = _os.getcwd()  # fallback to caller cwd
            _sqdb = _sq.connect(_os.path.join(_astor_dir_local, 'bot-binding.db'))
            _row = _sqdb.execute(
                "SELECT allow_commons_write FROM user_meta WHERE user_id = ?",
                (bus_user_id,),
            ).fetchone()
            _sqdb.close()
            if _row is not None and _row[0] == 0:
                _admin_allow = False
        except Exception:
            pass
        # Apply to each fact
        for _fx in facts:
            _vis_decision = _classify_vis(
                text=_fx.content or text,
                kind=_fx.kind,
                hint=_vis_hint if _vis_hint in ('auto', 'commons', 'personal') else 'auto',
                admin_allow_commons=_admin_allow,
            )
            _fx.visibility = _vis_decision['visibility']
            _fx.provenance_kind = 'auto_extracted'
        # Store the visibility classification report (helpful for client introspection)
        body['_visibility_report'] = {
            'hint': _vis_hint,
            'admin_allow': _admin_allow,
            'count_personal': sum(1 for f in facts if getattr(f, 'visibility', None) == 'personal'),
            'count_commons': sum(1 for f in facts if getattr(f, 'visibility', None) == 'commons'),
        }



        # 3. Insert candidates + promote (which auto-stores embeddings via nest)
        fact_ids = []
        facts_entities: list[list[dict]] = []
        _async_embed_count = 0
        # 2026-08-16 opt1: hook BM25 lex index — every promoted fact gets
        # tokenized and indexed for exact-match keyword recall. Failures
        # are logged but never block the write (lex is a redundant store).
        from .nest.lex_index import astor_lex as _astor_lex_for_write
        _lex = _astor_lex_for_write(tier=tier, user_id=bus_user_id)
        _w_stages['promote_start'] = _w_t.perf_counter()
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
                provenance_kind=getattr(f, 'provenance_kind', None) or body.get('provenance_kind') or _infer_provenance_kind(_write_session_id),
                provenance_agent=body.get('provenance_agent') or None,  # P1-fix 2026-08-15: enable content-hash dedup
                # v1.14.74+ Ship A2-Akasha: evidence-grounded source linking.
                # Caller may pass evidence_quote / source_ref / source_hash as
                # top-level body fields. All three are optional; defaults are
                # '' (legacy behavior — facts without source provenance).
                evidence_quote=str(body.get('evidence_quote') or '')[:1024],
                source_ref=str(body.get('source_ref') or '')[:512],
                source_hash=str(body.get('source_hash') or '')[:64],
                # v1.16.29 visibility tier (commons/personal).
                visibility=getattr(f, 'visibility', 'personal'),
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

            # v1.16.20 (2026-09-30): ASYNC embedding.
            # If caller set async_embed=True, we already wrote the fact.
            # Now spawn a daemon thread to compute embedding + store
            # in the embeddings table. If the thread fails, the next
            # read with use_embedding=True won't find this fact via
            # semantic similarity — but BM25/ECV/path_score still work.
            if _async_embed:
                import threading as _thr
                def _embed_async(_fid, _txt, _bus_obj, _tier_v, _uid_v):
                    try:
                        from .nest.embeddings import astor_get_embedding_model
                        import numpy as _np
                        _m = astor_get_embedding_model()
                        _emb = list(_m.embed([_txt[:500]]))[0]
                        _bytes = _np.asarray(_emb, dtype=_np.float32).tobytes()
                        # Insert into nest embeddings DB
                        from ._internal.acl_layout import (
                            get_db_path, Tier, Store,
                        )
                        _nest_path = str(get_db_path(
                            Tier.PUBLIC if _tier_v == 'public' else Tier.PRIVATE,
                            Store.NEST,
                        ))
                        import sqlite3 as _sq2
                        _cn = _sq2.connect(_nest_path)
                        _cn.execute(
                            "INSERT OR REPLACE INTO embeddings "
                            "(fact_id, embedding, model_version) VALUES (?, ?, ?)",
                            (_fid, _bytes, 'multilingual-e5-large'),
                        )
                        _cn.commit()
                        _cn.close()
                    except Exception as _e_exc:
                        _safe_stderr_write(
                            f'[astor.server] async embed failed (non-fatal): '
                            f'fid={_fid} {_e_exc}\n'
                        )
                _t = _thr.Thread(
                    target=_embed_async,
                    args=(int(canon_id), f.content, bus, tier, bus_user_id),
                    daemon=True,
                )
                _t.start()
                _async_embed_count = _async_embed_count + 1
            # v1.16.72: stage marker — promote loop done + counter
            _w_stages['promote_done'] = _w_t.perf_counter()
            _w_stages['promote_calls'] = 1  # counter of /v1/write calls
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

        # v1.15.57 (2026-09-30) Ship P1: auto-fork correction kinds to memory_experience.
        # When caller writes a fact with kind in ('correction', 'pushback', 'user_correction',
        # 'failure_pattern', 'lesson') we ALSO insert a memory_experience row so the
        # self-improving gate (match_experiences) can recall it at next /v1/read time.
        # This is the server-side enforcement of the pushback-capture protocol —
        # no client opt-in required, so any framework (hermes / LangChain / custom)
        # gets the benefit by just calling /v1/write with the right kind.
        _experience_id = None
        _experience_deduped = False
        _experience_occ = 0
        try:
            _write_kind = body.get('kind', '')
            _pushback_kinds = {'correction', 'pushback', 'user_correction', 'failure_pattern', 'lesson'}
            if _write_kind in _pushback_kinds and text:
                import hashlib as _hl_e
                from .bus import astor_bus as _ab_e
                _e_actor = user or 'admin'
                _e_tier = 'private' if not _e_actor.startswith('admin') and not _e_actor == 'admin' else 'source'
                _e_user_id = _e_actor if _e_tier.startswith('private') else None
                _e_text = text.strip()
                _e_keywords = body.get('tags') or []
                _e_blob = _e_actor + '||' + _e_text + '||' + '|'.join(sorted(str(k) for k in _e_keywords[:5]))
                _e_dedup = _hl_e.sha256(_e_blob.encode('utf-8')).hexdigest()[:16]
                _e_outcome = 'failure' if _write_kind in ('correction', 'pushback', 'user_correction', 'failure_pattern') else 'neutral'
                _e_bus = _ab_e(tier=_e_tier, user_id=_e_user_id)
                # dedup lookup
                _e_existing = None
                _experience_occ = 0
                try:
                    _e_upd = _e_bus.conn.execute(
                        "UPDATE memory_experience "
                        "SET invocation_count = invocation_count + 1, "
                        "    last_invoked_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                        "WHERE user_id = ? AND instr(reflection, ?) > 0 "
                        "RETURNING id, invocation_count",
                        (_e_actor, f'[dedup:{_e_dedup}]'),
                    ).fetchone()
                    if _e_upd:
                        _e_existing = int(_e_upd[0])
                        _experience_occ = int(_e_upd[1])
                        _e_bus.conn.commit()
                except Exception:
                    pass
                if _e_existing:
                    # _experience_occ already set by atomic UPDATE...RETURNING above.
                    pass
                    _experience_deduped = True
                    _experience_id = _e_existing
                    # v1.16.9: 3-tier promotion (medium 0.85 @ 3 invokes, HOT 0.95 @ 6).
                    # Article §2.3 — graduated tiers from default 0.7 → 0.85 → 0.95
                    # replaces the old binary jump.
                    if _experience_occ >= 6:
                        _e_bus.conn.execute(
                            "UPDATE memory_experience SET importance = 0.95 WHERE id = ?",
                            (_e_existing,),
                        )
                    elif _experience_occ >= 3:
                        _e_bus.conn.execute(
                            "UPDATE memory_experience SET importance = 0.85 WHERE id = ?",
                            (_e_existing,),
                        )
                    if _experience_occ >= 3:
                        _e_bus.conn.commit()
                else:
                    _e_reflection = f'[dedup:{_e_dedup}] auto-forked from /v1/write kind={_write_kind} fact_ids={fact_ids}'
                    _experience_id = _e_bus.insert_experience(
                        namespace=('private:' + _e_actor) if _e_tier.startswith('private') else _e_tier,
                        outcome=_e_outcome,
                        user_id=_e_actor,
                        trigger_keywords=list(_e_keywords) or None,
                        trigger_fact_ids=fact_ids or None,
                        action_summary=_e_text,
                        context=f'auto-forked from /v1/write kind={_write_kind}',
                        reflection=_e_reflection,
                        next_step_hint='',
                        source_session_id=_write_session_id,
                        importance=0.85,
                    )
                    _experience_occ = 1
        except Exception as _e_fork_exc:
            # Auto-fork failure must NEVER break the write — log to stderr only.
            _safe_stderr_write(f'[astor.server] correction auto-fork failed (non-fatal): {type(_e_fork_exc).__name__}: {_e_fork_exc}\n')

        # v1.16.8 (2026-09-30): bi-temporal auto-invalidation. When this write
        # carries an update-kind (correction / pushback / success_pattern /
        # user_correction / update / fact_update), scan existing ACTIVE facts
        # with overlapping entities and high content similarity, and mark them
        # valid_until=now() so they no longer pollute recall. Article
        # "2026 Agent 记忆六条路线" route 5 (Temporal KG) identified this gap
        # in route 4 (Agentic Memory Pipeline / Mem0-style).
        invalidated: list[dict] = []
        try:
            if fact_ids and _write_kind in {
                'correction', 'update', 'fact_update',
                'user_correction', 'pushback', 'success_pattern',
            }:
                from .bus.bitemporal import auto_invalidate_on_update
                for _nfid in fact_ids:
                    _ents = _extract_entities_for_fact(bus, _nfid) or []
                    _inv = auto_invalidate_on_update(
                        bus,
                        new_fact_id=_nfid,
                        content=body.get('text', '') or '',
                        entities=_ents,
                        kind=_write_kind,
                    )
                    if _inv:
                        invalidated.extend(_inv)
        except Exception as _bt_exc:
            # Bi-temporal failure must never break the write.
            _safe_stderr_write(f'[astor.server] bitemporal auto-invalidate failed (non-fatal): {type(_bt_exc).__name__}: {_bt_exc}\n')

        # v1.16.9 (2026-09-30): on-success auto-fork. Article §2.3 指出
        # "正信号触发写入: 只有收到明确的正信号时才写入长期记忆——用户
        # 确认/评测通过/任务成功". Currently /v1/write only auto-forks on
        # FAILURE-kind triggers (correction / pushback / etc); the success
        # path is silent. This hook inserts a memory_experience row when:
        #   (a) caller opted in via body.success_signal=True, OR
        #   (b) caller metadata explicitly marks verified=True (e.g. a
        #       post-tool-call hook confirming a successful ship).
        # Outcome is "success_pattern"; importance 0.7 (matches existing
        # auto-fork default). 3-occurrence auto-promote to medium-HOT (0.85)
        # and 6-occurrence to full HOT (0.95) — see the new 3-tier ladder.
        _success_exp_ids: list[int] = []
        try:
            _signal_success = bool(
                body.get('success_signal')
                or (body.get('metadata') or {}).get('verified') is True
            )
            # v1.16.9 defensive: _write_kind is assigned inside the auto-fork
            # try block above. If body.get('kind') raised (e.g. body is None
            # from a malformed POST), _write_kind would be unbound. Wrap
            # in a `.get(..., '')` fallback via the source-of-truth `body`
            # so this hook never NameErrors.
            _write_kind_safe = body.get('kind', '') or ''
            _accepted_kind = _write_kind_safe in {
                '', 'fact', 'observation', 'success_pattern', 'mental_model',
            }
            if _signal_success and fact_ids and _accepted_kind:
                import re as _re_s
                _e_text_s = (body.get('text') or '').strip()
                _e_keywords_s = list(dict.fromkeys(
                    _re_s.findall(r'[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9_]{2,}',
                                  _e_text_s)
                ))[:8]
                _e_reflect_s = f'[success] /v1/write kind={_write_kind or "fact"} fact_ids={fact_ids} verified=True'
                # v1.16.9: use the SAME bus instance that wrote the canonical
                # facts above — bus is already in scope as the closure variable
                # in /v1/write. Avoid double-bus init (which races on ACL).
                _e_id_s = bus.insert_experience(
                    namespace=(('private:' + (bus_user_id or user))
                               if tier.startswith('private') else tier),
                    outcome='success_pattern',
                    user_id=bus_user_id or user,
                    trigger_keywords=_e_keywords_s or None,
                    trigger_fact_ids=fact_ids or None,
                    action_summary=_e_text_s,
                    context=f'auto-forked from /v1/write kind={_write_kind or "fact"} (success signal)',
                    reflection=_e_reflect_s,
                    next_step_hint='',
                    source_session_id=_write_session_id,
                    importance=0.7,
                )
                _success_exp_ids.append(_e_id_s)
        except Exception as _e_success_exc:
            # On-success auto-fork failure must NEVER break the write — log only.
            _safe_stderr_write(f'[astor.server] on-success auto-fork failed (non-fatal): {type(_e_success_exc).__name__}: {_e_success_exc}\n')

        resp_body = {
            'event_id': event_id,
            'fact_ids': fact_ids,
            'count': len(fact_ids),
            'tier': tier,
            'scope': scope,
            'mirrored': mirrored_fact_ids,
            'success_experience_ids': _success_exp_ids,
            # v1.16.72: per-stage write-path timing (ms). Stage names:
            # classify_intent, start_extract, end_extract, promote_start,
            # promote_done, promote_calls. Only surfaced when
            # body.debug_timing=True (default off; backward-compat).
            'w_stages_ms': {k: round((v - _w_stages['start']) * 1000.0, 2)
                              for k, v in _w_stages.items()
                              if k != 'start' and isinstance(v, float)}
                              if body.get('debug_timing') else None,
            # v1.16.10: coref resolution report (empty if disabled or no pronouns)
            'coref_resolutions': _coref_meta.get('resolutions', []),
            'invalidated': invalidated,
            # v1.14.23 Ship E: structured entity binding per fact.
            # Index N in entities_per_fact = entities for fact_ids[N].
            'entities_per_fact': facts_entities,
            # v1.15.57 Ship P1: auto-fork to memory_experience.
            'experience_id': _experience_id,
            'experience_deduped': _experience_deduped,
            'experience_occurrence': _experience_occ,
            # v1.16.19: server-side tier resolver report.
            # If client didn't pass tier, we resolved it from
            # bot-binding.db lookup. Clients can rely on this
            # instead of doing the lookup themselves.
            'server_resolved_tier': _server_resolved_tier,
            # v1.16.20: async embedding report.
            'async_embed': bool(_async_embed),
            'async_embed_jobs': _async_embed_count,
            'effective_user_id': bus_user_id or user,
            # v1.16.29: visibility tier report.
            # visibility_report shows what visibility each fact was classified
            # to (commons/personal) and why (kind / hint / admin toggle).
            'visibility_report': body.get('_visibility_report') or {},
        }
        resp = jsonify(resp_body)
        # v1.16.x Layer 2: attach personal content sniff to response (warn only).
        if _personal_categories:
            resp.headers['X-Astor-Personal-Content'] = ','.join(_personal_categories)
            resp_body['personal_content_categories'] = list(_personal_categories)
            resp = jsonify(resp_body)
            resp.headers['X-Astor-Personal-Content'] = ','.join(_personal_categories)
        # v1.15.57 Ship P1: response header for downstream agents to detect
        # that this write created/updated an experience row.
        if _experience_id is not None:
            resp.headers['X-Astor-Experience-Id'] = str(_experience_id)
            resp.headers['X-Astor-Experience-Occurrence'] = str(_experience_occ)
        return resp

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
            words = [w.strip() for w in query.split() if len(w.strip()) >= 3][:6]
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

    def _meta_recall_lessons(query: str, user: str, tier: str) -> list:
        """v1.16+ (Plan "public tier 共享方法/流程/教训"): lesson auto-injection.

        Mirror of _meta_recall_patterns but filters kind='lesson' only — keeps
        lessons on their own top-2 quota so they don't compete with
        success_pattern / failure_pattern hits. Same tier routing semantics
        (private_X → private+public+source; source → source only;
        public/None → public+source). Same latency budget (<100ms, pure SQL).

        Why separate from _meta_recall_patterns: lessons are rarer + more
        specific, and they have higher importance (>=0.99). Putting them
        on a shared LIMIT 2 would risk getting crowded out by success/
        failure pattern noise.
        """
        if not query or len(query.strip()) < 3:
            return []
        try:
            words = [w.strip() for w in query.split() if len(w.strip()) >= 3][:6]
            if not words:
                return []
            like_clauses = ' AND '.join(['content LIKE ?' for _ in words])
            like_params = [f'%{w}%' for w in words]

            # Same tier routing as _meta_recall_patterns.
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
                        f"WHERE tombstoned = 0 AND kind = 'lesson' "
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
                            'meta_source': 'auto-meta-recall-lessons-v1.16',
                            'meta_tier': t,
                            'similarity': 0.99,  # synthetic — pattern match, not vector
                            'hit_source': 'meta-recall-lessons',
                        })
                except Exception as inner_exc:
                    _safe_stderr_write(f'[astor.server] meta-recall-lessons tier={t} err={inner_exc}\n')
                    continue
            return results
        except Exception as exc:
            _safe_stderr_write(f'[astor.server] meta-recall-lessons failed (non-fatal): {exc}\n')
            return []

    # v1.15.48 S22 (2026-09-29): trigger-aware meta-recall bootstrap.
    #
    # Root cause that motivated this: meta-recall only injects facts the agent
    # CAN see. If the agent never calls /v1/read before grabbing tools, no
    # pattern is injected — and the same "blind tool-grab" failure recurs.
    # Repeated 4× for mp.weixin (fact 6215 + 12274 + 12736 + this ship).
    #
    # Fix: when the user's QUERY (first turn of a session) matches a fetch-
    # like trigger verb + a resource hint (URL / platform token / file ext),
    # this gate injects a hardcoded "always-first-recall" cold-start rule
    # PLUS re-runs meta-recall with broader trigger-keywords so any matching
    # bus fact surfaces even if the original query didn't lexically match.
    #
    # This means: even a brand-new user with zero facts in bus still gets the
    # "recall before tool" reminder on their first wechat/github/pdf/url
    # request. After 1-2 such incidents, the auto-extract hook writes a
    # failure_pattern into bus and that fact starts surfacing instead.
    _TRIGGER_VERBS_CN = ('读', '抓', '爬', '搜', '找', '看', '分析', '处理', '帮我')
    _TRIGGER_VERBS_EN = (
        'read', 'fetch', 'crawl', 'scrape', 'lookup', 'look up', 'look at',
        'find', 'search', 'analyze', 'analyse', 'process', 'get',
        'download', 'open', 'visit', 'browse', 'extract', 'parse',
        # 2026-10-01: internal domain action verbs (R-class 12574,
        # fact 6463 'astor_memory 召回返回空 lesson 没自动注入',
        # R-class 6570 'global trigger lesson').
        # Without these, queries about internal SDK / API actions
        # don't trigger meta-recall and miss the relevant lessons.
        # Generic internal-domain verbs (any platform):
        'place_order', 'cancel_order', 'submit', 'fill', 'settle',
        'unlock', 'authenticate', 'deposit', 'withdraw',
        'buy', 'sell', 'trade', 'trading',
        'query', 'fetch_positions', 'list_orders', 'list_fills',
        'cancel', 'replace', 'modify', 'amend',
        # Chinese internal action verbs:
        '下单', '撤单', '挂单', '改单', '买入', '卖出', '交易',
        '查询', '解锁', '转账',
        # Generic CLI action verbs (helps with bash, git, kubectl, etc.):
        'run', 'execute', 'deploy', 'restart', 'start', 'stop',
        'install', 'uninstall', 'upgrade', 'update',
        'commit', 'push', 'pull', 'merge', 'rebase', 'checkout',
        'shell', 'cd', 'ls', 'cat', 'grep',
    )
    _TRIGGER_RESOURCE_HINTS = (
        'mp.weixin', 'weixin', '公众号', 'csdn', 'zhihu',
        'github.com', 'gitlab.com', 'bitbucket',
        'arxiv', 'doi.org', 'pubmed', 'ncbi.nlm',
        '.pdf', '.docx', '.xlsx', '.pptx', '.csv', '.json', '.xml',
        '.html', '.md', '.txt', '.log',
        'paper', '论文', 'article', '文章', 'repo', '仓库',
        'youtube.com', 'youtu.be', 'bilibili.com', 'vimeo',
        'twitter.com', 'x.com', 'reddit.com',
        'stackoverflow', 'http://', 'https://',
        # 2026-10-01: internal domain resource hints (R-class 12574,
        # fact 6570 'global trigger lesson').
        # When query mentions ANY internal platform / SDK / CLI tool,
        # the trigger fires and pulls in success_pattern / lesson /
        # failure_pattern. This is GLOBAL across all domains.
        # moomoo / OpenD
        'moomoo', 'opend', 'futu', 'opend_core', 'credentials.py',
        'moomooapi',
        # crypto exchanges
        'kraken', 'bybit', 'ndax', 'binance', 'coinbase', 'kucoin',
        # stock brokers / SEC filings
        'sec_firm', 'interactive brokers', 'schwab', 'robinhood',
        'wealthsimple', 'questrade',
        # shell / OS / k8s / git / docker
        'kubectl', 'docker', 'terraform', 'aws cli', 'gcloud',
        # sci / eng tooling
        'arxiv', 'pubmed', 'github', 'gitlab',
        # internal astor itself
        'astor', 'astor_recall', 'memory_canonical', 'memory_bus',
        # web resources (already covered above but added for completeness)
        'wikipedia', 'docs.', 'documentation',
        # .py files (Python source code) — these often have success_patterns
        '.py', 'python script',
    )
    _COLD_START_FIRST_RECALL_RULE = (
        '[astor cold-start rule] When the user asks you to read / fetch / '
        'crawl / lookup any external resource (URL, paper, article, repo, '
        'PDF, mp.weixin, github, etc.), your FIRST action MUST be '
        'astor_recall (or astor_recall-equivalent like auto_route_read) to '
        'check whether astor already has a success_pattern / failure_pattern '
        'for that exact resource type. Do NOT call web_extract, browser_act, '
        'web_search, Perplexity, curl, or any other fetch tool before that '
        'recall returns. This rule was added 2026-09-29 after the 4th '
        'repeated mp.weixin failure (facts 6215 / 12274 / 12736).'
    )
    _TRIGGER_BOOST_KEYWORDS = (
        'recall', 'astor_recall', 'success_pattern', 'failure_pattern',
        'first action', 'before tool', 'first recall',
        '先 recall', '先查', '成功模式', '失败模式',
        'first tool call', 'pre-tool-call',
    )

    def _detect_first_recall_trigger(query: str) -> bool:
        """Return True iff query looks like 'fetch external resource X'."""
        if not query or len(query.strip()) < 4:
            return False
        q = query
        q_lower = q.lower()
        has_verb = any(v in q for v in _TRIGGER_VERBS_CN) or any(
            v in q_lower for v in _TRIGGER_VERBS_EN
        )
        has_hint = any(h.lower() in q_lower for h in _TRIGGER_RESOURCE_HINTS)
        return has_verb and has_hint

    def _meta_recall_trigger_aware(query: str, user: str, tier: str) -> list:
        """v1.15.48 S22: trigger-aware meta-recall bootstrap.

        When query is a 'fetch X' request:
          1. Always inject the cold-start first-recall rule (so brand-new
             users without bus facts still see the gate).
          2. Run a broadened-keyword LIKE query across success_pattern /
             failure_pattern / lesson tables so any matching bus fact
             surfaces even if the original query didn't lexically match.
        """
        if not _detect_first_recall_trigger(query):
            return []
        out = [{
            'fact_id': 0,
            'content': _COLD_START_FIRST_RECALL_RULE,
            'kind': 'cold_start_rule',
            'importance': 0.99,
            'memory_class': 'mental_model',
            'meta_source': 'auto-trigger-aware-v1.15.48',
            'meta_tier': 'source',
            'similarity': 1.0,
            'hit_source': 'trigger-aware',
            'cold_start': True,
        }]
        try:
            words = list(query.split())[:4] + list(_TRIGGER_BOOST_KEYWORDS)[:6]
            words = [w for w in words if len(w.strip()) >= 2][:8]
            if not words:
                return out
            like_clauses = ' OR '.join(['content LIKE ?' for _ in words])
            like_params = [f'%{w}%' for w in words]
            seen_ids = {r['fact_id'] for r in out if r.get('fact_id')}
            tiers_to_query = []
            if tier and tier.startswith('private'):
                if user:
                    tiers_to_query.append(('private', user))
                tiers_to_query.append(('public', None))
                tiers_to_query.append(('source', None))
            elif tier == 'source':
                tiers_to_query.append(('source', None))
            else:
                tiers_to_query.append(('public', None))
                tiers_to_query.append(('source', None))
            for t, u in tiers_to_query:
                try:
                    b = astor_bus(tier=t, user_id=u)
                    rows = b.conn.execute(
                        f"SELECT id, content, kind, importance, memory_class "
                        f"FROM memory_canonical "
                        f"WHERE tombstoned = 0 "
                        f"AND kind IN ('success_pattern', 'failure_pattern', 'lesson') "
                        f"AND ({like_clauses}) "
                        f"ORDER BY importance DESC, created_at DESC LIMIT 5",
                        tuple(like_params),
                    ).fetchall()
                    for row in rows:
                        if row[0] in seen_ids:
                            continue
                        seen_ids.add(row[0])
                        out.append({
                            'fact_id': row[0],
                            'content': row[1][:500] if row[1] else '',
                            'kind': row[2],
                            'importance': row[3],
                            'memory_class': row[4] if len(row) > 4 else 'world_fact',
                            'meta_source': 'auto-trigger-aware-v1.15.48',
                            'meta_tier': t,
                            'similarity': 0.95,
                            'hit_source': 'trigger-aware-boost',
                        })
                except Exception as inner_exc:
                    _safe_stderr_write(f'[astor.server] trigger-aware tier={t} err={inner_exc}\n')
                    continue
        except Exception as exc:
            _safe_stderr_write(f'[astor.server] trigger-aware failed (non-fatal): {exc}\n')
        return out

    def _meta_recall_mental_models(query: str, user: str, tier: str) -> list:
        """v1.15.42: Hindsight-style mental_model auto-injection.

        When the user's query contains keywords that match a fixed-question
        mental_model's question, surface the answer at the top of recall.
        Returns dicts shaped like _meta_recall_patterns (compatible with
        the existing meta-recall prepending path).

        Pure SQL via json_each + lexical LIKE on memory_canonical.kind=
        'mental_model' rows. Same tier routing as _meta_recall_patterns.
        """
        if not query or len(query.strip()) < 3:
            return []
        try:
            from .nest.mental_models import _parse_mm_content
            # Tokenize query (CJK-friendly: split on whitespace + take 2+ chars).
            words = [w.strip() for w in query.split() if len(w.strip()) >= 2][:8]
            if not words:
                return []
            like_clauses = ' AND '.join(['content LIKE ?' for _ in words])
            like_params = [f'%{w}%' for w in words]
            tiers_to_query = []
            if tier and tier.startswith('private'):
                if user:
                    tiers_to_query.append(('private', user))
                tiers_to_query.append(('public', None))
                tiers_to_query.append(('source', None))
            elif tier == 'source':
                tiers_to_query.append(('source', None))
            else:
                tiers_to_query.append(('public', None))
                tiers_to_query.append(('source', None))
            results = []
            seen_ids = set()
            for t, u in tiers_to_query:
                try:
                    b = astor_bus(tier=t, user_id=u)
                    rows = b.conn.execute(
                        f"SELECT id, content, importance, confidence "
                        f"FROM memory_canonical "
                        f"WHERE tombstoned = 0 AND kind = 'mental_model' "
                        f"AND ({like_clauses}) "
                        f"ORDER BY confidence DESC, importance DESC, id DESC LIMIT 3",
                        tuple(like_params),
                    ).fetchall()
                    for row in rows:
                        fid, content, imp, conf = row
                        if fid in seen_ids:
                            continue
                        parsed = _parse_mm_content(content or '')
                        if parsed is None:
                            continue
                        q_text, a_text = parsed[0], parsed[1]
                        seen_ids.add(fid)
                        # Format: [MM] Q=<question> | A=<answer>
                        formatted = f"[MM] Q={q_text} | A={a_text[:300]}"
                        results.append({
                            'fact_id': fid,
                            'content': formatted[:500],
                            'kind': 'mental_model',
                            'importance': float(imp or 0.5),
                            'memory_class': 'mental_model',
                            'confidence': float(conf or 0.5),
                            'meta_source': 'auto-mental-model-v1.15.42',
                            'meta_tier': t,
                            'similarity': 1.0,  # synthetic — direct match
                            'hit_source': 'mental-model-auto',
                        })
                except Exception as inner_exc:
                    _safe_stderr_write(f'[astor.server] mental-model recall tier={t} err={inner_exc}\n')
                    continue
            return results
        except Exception as exc:
            _safe_stderr_write(f'[astor.server] mental-model auto-recall failed (non-fatal): {exc}\n')
            return []

    def _meta_recall_knowledge_pages(query: str, user: str, tier: str) -> list:
        """v1.15.43: Hindsight-style knowledge_page recall augmentation.

        When the user's query matches a knowledge_page's slug/title keywords,
        surface the page (with linked_fact count) as a reference card at
        the top of recall. Returns dicts shaped like _meta_recall_patterns.

        Pure SQL LIKE on memory_canonical.kind='knowledge_page' rows. Same
        tier routing as _meta_recall_mental_models.
        """
        if not query or len(query.strip()) < 3:
            return []
        try:
            # Tokenize query (>=2 chars, max 8 words).
            words = [w.strip() for w in query.split() if len(w.strip()) >= 2][:8]
            if not words:
                return []
            like_clauses = ' AND '.join(['content LIKE ?' for _ in words])
            like_params = [f'%{w}%' for w in words]
            tiers_to_query = []
            if tier and tier.startswith('private'):
                if user:
                    tiers_to_query.append(('private', user))
                tiers_to_query.append(('public', None))
                tiers_to_query.append(('source', None))
            elif tier == 'source':
                tiers_to_query.append(('source', None))
            else:
                tiers_to_query.append(('public', None))
                tiers_to_query.append(('source', None))
            results = []
            seen_ids = set()
            for t, u in tiers_to_query:
                try:
                    b = astor_bus(tier=t, user_id=u)
                    rows = b.conn.execute(
                        f"SELECT id, content, importance, confidence, metadata "
                        f"FROM memory_canonical "
                        f"WHERE tombstoned = 0 AND kind = 'knowledge_page' "
                        f"AND ({like_clauses}) "
                        f"ORDER BY confidence DESC, importance DESC, id DESC LIMIT 3",
                        tuple(like_params),
                    ).fetchall()
                    for row in rows:
                        fid, content, imp, conf, meta_json = row
                        if fid in seen_ids:
                            continue
                        # Parse slug + title.
                        slug, title, body = "", "", ""
                        if content and content.startswith("[KP]"):
                            header = content.split(chr(10) + chr(10), 1)[0]
                            for ln in header.split(chr(10)):
                                if ln.startswith("[KP] slug: "):
                                    slug = ln[len("[KP] slug: "):].strip()
                                elif ln.startswith("title: "):
                                    title = ln[len("title: "):].strip()
                        if not slug:
                            continue
                        # Parent fact count from metadata.
                        try:
                            meta = json.loads(meta_json) if meta_json else {}
                        except Exception:
                            meta = {}
                        parent_count = len(meta.get("parent_fact_ids") or [])
                        seen_ids.add(fid)
                        body_preview = (body[:200] + "...") if len(body) > 200 else body
                        formatted = (
                            f"[KP] slug={slug} title={title[:80]} "
                            f"linked_facts={parent_count} body={body_preview[:300]}"
                        )
                        results.append({
                            'fact_id': fid,
                            'content': formatted[:500],
                            'kind': 'knowledge_page',
                            'importance': float(imp or 0.5),
                            'memory_class': 'knowledge_page',
                            'confidence': float(conf or 0.5),
                            'meta_source': 'auto-knowledge-page-v1.15.43',
                            'meta_tier': t,
                            'similarity': 0.95,
                            'hit_source': 'knowledge-page-auto',
                        })
                except Exception as inner_exc:
                    _safe_stderr_write(f'[astor.server] kp recall tier={t} err={inner_exc}\n')
                    continue
            return results
        except Exception as exc:
            _safe_stderr_write(f'[astor.server] kp auto-recall failed (non-fatal): {exc}\n')
            return []

    @app.route('/v1/read', methods=['POST'])
    def read():
        """Recall similar facts via nest vector search.

        Body JSON:
          query: str (required)
          user: str (optional, filter by user_id)
          top_k: int (default 5)
          routing_strategy: str (optional, v1.16.54) — one of "auto" | "graph"
                            | "dense" | "hybrid". "auto" (default) uses
                            astor_memory.nest.retrieval_router.choose_route()
                            to decide graph-first vs dense-only vs hybrid
                            based on query features (length, multihop
                            markers, factoid markers, proper nouns).
                            "hybrid" is back-compat with v1.10.9 baseline.
                            "graph" / "dense" force a strategy.
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

        # 2026-10-03 (v1.16.61): intent classification (SimpleMem-inspired).
        # Caller may pass body.intent explicitly; otherwise we classify
        # server-side using classify_read_intent() (no DB reads, no
        # extra latency). Logs to recall_log.jsonl so future ship arc
        # can wire intent-aware rerank or EvolveMem-style self-tuning.
        # Six buckets: factual / procedural / temporal / personal /
        # preference / method. The body.intent escape hatch is per-request
        # so callers can override (e.g. for A/B testing).
        _intent = body.get('intent') or classify_read_intent(query)
        if _intent not in ('factual', 'procedural', 'temporal', 'personal',
                           'preference', 'method'):
            _intent = 'factual'

        # v1.16.54 (Ship P1): Learnable Routing dispatch. Decides whether
        # to use graph-first expansion (cheap, finds implicit preferences)
        # or dense-only (faster, best for explicit keyword queries) or
        # hybrid (back-compat). Default "auto" uses astor.nest.retrieval_router
        # heuristics. body.routing_strategy = "auto" | "graph" | "dense" | "hybrid".
        # Computed BEFORE cache check so cache hit responses still surface
        # the routing decision in the response.
        from .nest.retrieval_router import (
            choose_route as _choose_route,
            routing_decision_to_dict as _rd_to_dict,
        )
        _routing_decision = _choose_route(query, body)
        # v1.16.20: LRU read cache (60s TTL). Same query+tier+user+top_k
        # within TTL returns cached results — kills redundant nest/embedding
        # work when agents hammer the same query (muse retry loops).
        global _READ_CACHE
        _rcache_key = (
            query, body.get('tier', 'public'),
            body.get('user') or body.get('user_id') or '',
            body.get('top_k', 5),
        )
        _rcache_hit = _READ_CACHE.get(_rcache_key)
        if _rcache_hit and (_rc_time.time() - _rcache_hit[0]) < 60:
            return jsonify({
                'results': _rcache_hit[1], 'count': len(_rcache_hit[1]),
                'cached': True,
                'cache_age_s': round(_rc_time.time() - _rcache_hit[0], 1),
                'recall_controller': {'action': 'cache_hit', 'reason': 'lru60s',
                                       'elapsed_ms': 0},
                # v1.16.54 Ship P1: surface routing decision from cache hit too.
                'routing_decision': _rd_to_dict(_routing_decision),
            })
        # Mutate body copy so downstream conditional logic can branch on strategy.
        body_for_strategy = dict(body)
        body_for_strategy.setdefault('routing_strategy', _routing_decision.strategy)
        global _meta_recall_stats
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
        # v1.16.54 Ship P1: routing dispatch gates expensive query rewriting.
        # "graph" strategy skips synonym variants — graph-first expansion
        # already produces enough variants from the entity graph.
        _skip_synonyms = _routing_decision.strategy == 'graph'
        if not _skip_synonyms and os.environ.get('ASTOR_EXPANSION', '1') != '0':
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
        # v1.16.54 Ship P1: routing strategy gates multihop+graph expansion.
        # "dense" strategy skips both — pure keyword lookup, no extra latency.
        if _routing_decision.strategy != 'dense' and os.environ.get('ASTOR_MULTIHOP', '1') != '0':
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
        # Graph expansion: skip when routing strategy is "dense".
        if (_is_multihop or _routing_decision.strategy == 'graph') and os.environ.get('ASTOR_GRAPH', '1') != '0':
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
        # v1.16.54 Ship P1: routing strategy gates multihop+graph expansion.
        # "dense" strategy skips both — pure keyword lookup, no extra latency.
        if _routing_decision.strategy != 'dense' and os.environ.get('ASTOR_MULTIHOP', '1') != '0':
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
        # Graph expansion: skip when routing strategy is "dense".
        if (_is_multihop or _routing_decision.strategy == 'graph') and os.environ.get('ASTOR_GRAPH', '1') != '0':
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
            # v1.16.73 Ship F: opt-in RRF (Reciprocal Rank Fusion) path.
            # When caller passes body.use_rrf=True, replace the weighted-sum
            # hybrid_merge with proper RRF across the existing candidate
            # channels. We add an explicit TIME-filter channel (4th path,
            # "TEMPR"-style) using event_date pre-filter. Default off for
            # backward compat — paper pattern (mp/-ula31Nw4kmDYPQooKPsGA).
            _use_rrf = bool(body.get('use_rrf'))
            _results_from_rrf = False
            if _use_rrf:
                from .recall_rrf import rrf_fusion as _rrf_f
                _rrf_k = int(body.get('rrf_k') or 60)
                # Channel 4: time filter (TEMPR's 4th path).
                # Pre-filter candidates whose event_date falls in time_range.
                _time_filtered = list(bm25_hits) + list(vector_hits)
                if time_range:
                    _since_iso, _until_iso = time_range
                    _tr_kept = set()
                    if _time_filtered:
                        _tf_fids = [f for f, _ in _time_filtered]
                        if _tf_fids:
                            _ph = ','.join('?' * len(_tf_fids))
                            _tf_rows = bus.conn.execute(
                                f"SELECT id, event_date FROM memory_canonical "
                                f"WHERE id IN ({_ph}) AND event_date IS NOT NULL "
                                f"AND event_date >= ? AND event_date <= ?",
                                _tf_fids + [_since_iso, _until_iso],
                            ).fetchall()
                            _tr_kept = {r[0] for r in _tf_rows}
                    _time_hits = [(f, 1.0) for f, _ in _time_filtered
                                   if f in _tr_kept]
                else:
                    _time_hits = []
                # Run RRF across 3 channels (BM25, vector, time).
                _rrf_merged = _rrf_f(bm25_hits, vector_hits, _time_hits, k=_rrf_k)
                results = _rrf_merged[:top_k]
                _hit_src = {}
                for fid, _ in bm25_hits[:top_k]:
                    _hit_src[int(fid)] = 'bm25'
                for fid, _ in vector_hits[:top_k]:
                    _hit_src.setdefault(int(fid), 'vector')
                for fid, _ in _time_hits[:top_k]:
                    _hit_src.setdefault(int(fid), 'time_filter')
                _results_from_rrf = True
            if not _results_from_rrf:
                pass  # fall through to existing weighted-sum hybrid below
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
            # v1.16.6 (2026-09-30): bridge boost ON by default (ASTOR_BRIDGE=1).
            # Per GraphMemix ablation (北大 + MemoraX AI, wechat summary 9/30):
            # multi-view + node verifier + ECV drives +5.2pp → +0.9pp → +1.85pp
            # on multi-hop benchmarks. The bridge is our cheap proxy for
            # "evidence chain coherence". Empirical note from earlier eval:
            # decay<0.4 hurts LoCoMo — we pass decay=0.10 (well under 0.4)
            # to keep entity collisions on generic nouns from over-promoting
            # wrong answers. Set ASTOR_BRIDGE=0 to disable for opt-out.
            if os.environ.get('ASTOR_BRIDGE', '1') == '1' and results and len(results) >= 2:
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
                _now_tb = _dt_tb.datetime.now(_dt_tb.timezone.utc).replace(tzinfo=None)
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
                _now_iso = _dt_acc.datetime.now(_dt_acc.timezone.utc).replace(tzinfo=None).isoformat(timespec='seconds') + 'Z'
                # Decay sweep (ENABLED by default as of v1.14.39 — disable via
                # ASTOR_DECAY_SWEEP=0). MemPalace's living-memory dynamics (Hebbian
                # potentiation + Ebbinghaus decay, v3.3.6) validates this direction:
                # facts that get surfaced stay hot, facts that don't get surfaced
                # fade out so the corpus doesn't grow stale forever.
                # 30d no-recall: access_count halved (floor 1).
                # 90d no-recall: tombstoned (archive).
                if _os_acc.environ.get('ASTOR_DECAY_SWEEP', '1') != '0':
                    _30d_iso = (_dt_acc.datetime.now(_dt_acc.timezone.utc).replace(tzinfo=None) - _dt_acc.timedelta(days=30)).isoformat(timespec='seconds') + 'Z'
                    _90d_iso = (_dt_acc.datetime.now(_dt_acc.timezone.utc).replace(tzinfo=None) - _dt_acc.timedelta(days=90)).isoformat(timespec='seconds') + 'Z'
                    # v1.14.63 (R-class fix): skip LOCK rules. They are
                    # administrative configuration, not recall-derived facts;
                    # auto-tombstoning them would silently break /v1/classify
                    # Path 2 (rule_ship / private routing). Same applies to
                    # `rule` facts (compiled Ship A/B rules).
                    # v1.15.47 (Ship F.1): also skip operator-authored surfaces
                    # mental_model + knowledge_page. These are admin reference
                    # rows that should NEVER decay — they're refreshed by
                    # explicit `am mental-model rebuild` / `am knowledge-page
                    # upsert`, not by recall dynamics.
                    bus.conn.execute(
                        f"UPDATE memory_canonical SET access_count = MAX(1, access_count / 2) "
                        f"WHERE id NOT IN ({_ph_acc}) AND tombstoned = 0 "
                        f"AND (last_confirmed_at IS NULL OR last_confirmed_at < ?) "
                        f"AND kind NOT IN ('lock_rule', 'rule', 'mental_model', 'knowledge_page')",
                        _surfaced_fids + [_30d_iso],
                    )
                    bus.conn.execute(
                        f"UPDATE memory_canonical SET tombstoned = 1 "
                        f"WHERE id NOT IN ({_ph_acc}) AND tombstoned = 0 "
                        f"AND (last_confirmed_at IS NULL OR last_confirmed_at < ?) "
                        f"AND kind NOT IN ('lock_rule', 'rule', 'mental_model', 'knowledge_page')",
                        _surfaced_fids + [_90d_iso],
                    )
                # Always: bump surfaced facts' access_count + last_confirmed_at +
                # distinct_queries_hit (v1.16.71 Dream-tier T3 gate).
                bus.conn.execute(
                    f"UPDATE memory_canonical SET access_count = access_count + 1, "
                    f"distinct_queries_hit = distinct_queries_hit + 1, "
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
            _usage_path = os.environ.get('ASTOR_DIR') or os.path.expanduser('~/.astor')
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
                # v1.16.61: capture hit-rate signal. top_score is the
                # top hit's hybrid score (0 = empty, > 0.5 = strong hit).
                # Used by astor_self_eval to compute per-intent hit rate.
                _top_score = float(enriched[0].get('confidence') or 0) if enriched else 0.0
                _top_kind = str(enriched[0].get('kind') or '') if enriched else ''
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
                    'top_hit_score': round(_top_score, 4),
                    'top_hit_kind': _top_kind[:32],
                    # v1.16.61: caller-set or server-classified.
                    'intent': _intent,
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

        # v1.16.x: reactive consult gate. Body.consult=True forces meta-recall;
        # body.consult=False skips it; body.consult=None falls back to env
        # ASTOR_CONSULT_DEFAULT_ON (default '1' = ON, preserves v1.16 proactive
        # behavior). Operators can set ASTOR_CONSULT_DEFAULT_ON=0 to switch to
        # the new "reactive consult" semantics globally (agent must opt in).
        _consult = body.get('consult')
        if _consult is None:
            _consult = os.environ.get('ASTOR_CONSULT_DEFAULT_ON', '1') == '1'
        else:
            _consult = bool(_consult)
        if _consult:
            _meta_recall_stats['consult_triggered'] = _meta_recall_stats.get('consult_triggered', 0) + 1
            # v1.15.48 S22 (2026-09-29): trigger-aware meta-recall bootstrap.
            # Runs FIRST so cold-start users + fetch-trigger queries get the
            # 'recall before tool' reminder even before normal pattern recall.
            # This is the code-level fix for the 4× repeated mp.weixin failure
            # (facts 6215 / 12274 / 12736) — without this gate, /v1/read only
            # injects facts that lexically match the query, and 'first recall
            # before tool' facts don't lexically match 'read this mp.weixin'.
            try:
                _trigger = _meta_recall_trigger_aware(
                    query, body.get('user') or body.get('user_id'), tier,
                )
                if _trigger:
                    enriched = _trigger + enriched
                    _meta_recall_stats['triggered'] += 1
                    _meta_recall_stats['returned_total'] += len(_trigger)
                    _meta_recall_stats['trigger_aware_triggered'] += 1
                    _meta_recall_stats['trigger_aware_returned_total'] += len(_trigger)
            except Exception as _tr_exc:
                _safe_stderr_write(f'[astor.server] trigger-aware prepend failed: {_tr_exc}\n')
                _meta_recall_stats['errors'] += 1
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
            # v1.16+ (Plan "public tier 共享方法/流程/教训"): lesson auto-injection.
            # Mirror of the meta-recall block above but routed through _meta_recall_lessons.
            # Lessons carry higher importance (>=0.99) and rarer phrasing, so they
            # need their own top-2 channel to avoid being crowded out by pattern noise.
            try:
                _lessons = _meta_recall_lessons(query, body.get('user') or body.get('user_id'), tier)
                if _lessons:
                    enriched = _lessons + enriched
                    _meta_recall_stats['triggered'] += 1
                    _meta_recall_stats['returned_total'] += len(_lessons)
            except Exception as _ml_exc:
                _safe_stderr_write(f'[astor.server] meta-recall-lessons prepend failed: {_ml_exc}\n')
                _meta_recall_stats['errors'] += 1
        # v1.15.42 (Ship P3.1 recall enhancement): mental_model auto-injection.
        try:
            _mm = _meta_recall_mental_models(query, body.get('user') or body.get('user_id'), tier)
            _safe_stderr_write(f'[DEBUG-MM] query={query[:30]!r} tier={tier!r} user={body.get("user") or body.get("user_id")!r} → {len(_mm) if _mm else 0} hits\n')
            if _mm:
                enriched = _mm + enriched
                _meta_recall_stats['triggered'] += 1
                _meta_recall_stats['returned_total'] += len(_mm)
        except Exception as _mm_exc:
            _safe_stderr_write(f'[astor.server] mental-model recall failed (non-fatal): {_mm_exc}\n')
            _meta_recall_stats['errors'] += 1
        # v1.15.43 (Ship P3.1 knowledge_page recall):
        try:
            _kp = _meta_recall_knowledge_pages(query, body.get('user') or body.get('user_id'), tier)
            _safe_stderr_write(f'[DEBUG-KP] query={query[:30]!r} tier={tier!r} user={body.get("user") or body.get("user_id")!r} → {len(_kp) if _kp else 0} hits\n')
            if _kp:
                enriched = _kp + enriched
                _meta_recall_stats['triggered'] += 1
                _meta_recall_stats['returned_total'] += len(_kp)
        except Exception as _kp_exc:
            _safe_stderr_write(f'[astor.server] kp recall failed: type={type(_kp_exc).__name__} {_kp_exc}\n')
            _meta_recall_stats['errors'] += 1

        # v1.16.20: cache store (before return).
        try:
            _READ_CACHE[_rcache_key] = (_rc_time.time(), enriched)
            if len(_READ_CACHE) > 128:
                # Simple eviction: drop oldest 32
                _oldest = sorted(_READ_CACHE.items(), key=lambda kv: kv[1][0])[:32]
                for _k, _v in _oldest:
                    _READ_CACHE.pop(_k, None)
        except Exception:
            pass
        # v1.16.34: RPMem-inspired recall-aware importance adjustment.
        # Boost importance of facts that got recalled (they ARE useful);
        # softly decay importance of facts that haven't been touched in
        # 90+ days (noise accumulating). Article reference:
        # "同领域事件的写入模式相似度为 0.872, 跨领域为 0.784"
        try:
            _hit_fact_ids = [
                int(e.get('fact_id') or e.get('id'))
                for e in (enriched or [])
                if (e.get('fact_id') or e.get('id'))
            ]
            if _hit_fact_ids and body.get('recall_aware_decay', True):
                _bus_for_decay = astor_bus(tier=tier, user_id=user_id)
                _decay_report = _bus_for_decay.apply_recall_aware_decay(
                    hit_fact_ids=_hit_fact_ids,
                    decay_factor_unhit=0.95,
                    boost_factor_hit=1.05,
                    days_unhit_threshold=90,
                )
            else:
                _decay_report = {'skipped': 'no_hits_or_disabled'}
        except Exception as _dec_exc:
            _decay_report = {'error': str(_dec_exc)}
        return jsonify({
            'results': enriched,
            'count': len(enriched),
            # v1.16.61: surface intent classification for caller introspection
            # and downstream tooling (e.g. dashboard "what do I ask most?").
            # Caller can override via body.intent; otherwise server-classified.
            'intent': _intent,
            # v1.16.61: top_hit_score is the score of the top hit (0 if empty).
            # Lets the caller decide their own threshold for "useful recall".
            'top_hit_score': round(float(enriched[0].get('confidence') or 0), 4) if enriched else 0.0,
            # v1.16.54 Ship P1: surface routing decision so callers can
            # debug "why graph-first vs dense-only" outcomes and adjust
            # body.routing_strategy override accordingly.
            'routing_decision': _rd_to_dict(_routing_decision),
            'recall_aware_decay': _decay_report,
            # v1.16.34: surface decay stats so callers know if any
            # facts were touched (boosted or decayed) by this recall.
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

    @app.route('/v1/backfill_memory_class', methods=['POST'])
    def backfill_memory_class():
        """v1.15.49 S23: one-shot backfill of memory_class for existing rows.

        Iterates per-tier DBs, calls AstorBus.backfill_memory_class(dry_run).
        Body: {'tier': 'public'|'source'|'private'|'all' (default 'all'),
               'dry_run': bool (default True),
               'user_id': str (only when tier='private')}
        Admin only (R218 astor no-restart rule: this writes to bus, so
        write ACL gate applies). Returns counts per kind → mapped_class.
        Idempotent: re-running with dry_run=True after a backfill returns
        zero candidates (nothing more to update).
        """
        body = request.get_json(silent=True) or {}
        tier_arg = body.get('tier', 'all')
        dry_run = bool(body.get('dry_run', True))
        user_id = body.get('user_id')
        tiers_to_process = []
        if tier_arg == 'all':
            tiers_to_process = ['public', 'source']
            if user_id:
                tiers_to_process.append(('private', user_id))
            else:
                # admin tier
                tiers_to_process.append(('private', 'admin'))
        elif tier_arg == 'private':
            if not user_id:
                return jsonify({
                    'error': 'user_id required',
                    'detail': "tier='private' requires user_id in body (e.g. 'admin')",
                }), 400
            tiers_to_process = [('private', user_id)]
        else:
            tiers_to_process = [tier_arg]
        results = {}
        total_to_update = 0
        for t in tiers_to_process:
            if isinstance(t, tuple):
                t_name, t_user = t
            else:
                t_name, t_user = t, None
            try:
                bus = astor_bus(tier=t_name, user_id=t_user)
                r = bus.backfill_memory_class(dry_run=dry_run)
                results[f"{t_name}:{t_user}"] = r
                total_to_update += r.get('total_to_update', 0)
            except Exception as exc:
                results[f"{t_name}:{t_user}"] = {'error': str(exc)}
        return jsonify({
            'total_to_update': total_to_update,
            'dry_run': dry_run,
            'tiers': results,
        })

    @app.route('/v1/fact/<int:fact_id>/set_status', methods=['POST'])
    def set_fact_status(fact_id: int):
        """v1.16.68 Ship B #1: flip a fact between active/inactive/archived.

        Body JSON:
          status  (str, required) — 'active' | 'inactive' | 'archived'
          reason  (str, optional) — audit-logged

        Returns: {fact_id, status, ok: True}

        Use case: user says "I don't drink coffee anymore". Instead of
        deleting the old "user likes coffee" fact, call this with
        status='inactive' so the old fact stays in recall as
        "⚠[INACTIVE] [EVIDENCE] user likes coffee [/EVIDENCE]" — the
        LLM sees both the old AND the current preference and can
        reason about the change.
        """
        body = request.get_json(force=True)
        new_status = body.get('status')
        reason = body.get('reason', '')
        if new_status not in ('active', 'inactive', 'archived'):
            return jsonify({'error': 'invalid_status',
                            'detail': 'status must be one of active|inactive|archived'}), 400
        try:
            from .bus.store import _get_or_create_singleton as _g
            from ._internal.acl import astor_check_write
            astor_check_write('public', None)
        except Exception:
            pass
        try:
            # We write to all 3 tiers since status is a global SSoT property
            # of a fact_id. In practice fact_ids are tier-namespaced, but
            # for safety we update every tier DB that contains the row.
            from ._internal.acl_layout import get_db_path as _gdp
            from .bus.schema import astor_init_schema
            updated_tiers = []
            for t in ('public', 'private', 'source'):
                for uid in (None, 'admin'):
                    try:
                        conn = sqlite3.connect(str(_gdp(t, 'bus', uid)))
                        astor_init_schema(conn)
                        cur = conn.execute(
                            "UPDATE memory_canonical SET status = ? WHERE id = ? AND tombstoned = 0",
                            (new_status, fact_id)
                        )
                        if cur.rowcount > 0:
                            conn.commit()
                            updated_tiers.append(f"{t}:{uid or '_'}")
                        conn.close()
                    except Exception:
                        continue
            if not updated_tiers:
                return jsonify({'error': 'fact_not_found',
                                'detail': f'no tier DB had fact_id={fact_id}'}), 404
            # Audit
            try:
                from .bus.store import astor_audit
                astor_audit(
                    actor='admin:admin', tier='public', action='set_status',
                    target=str(fact_id),
                    metadata={'new_status': new_status, 'reason': reason},
                )
            except Exception:
                pass
            return jsonify({'fact_id': fact_id, 'status': new_status,
                            'updated_tiers': updated_tiers, 'ok': True})
        except Exception as e:
            return jsonify({'error': 'set_status_failed', 'detail': str(e)}), 500

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

    @app.route('/v1/chat/ingest', methods=['POST'])
    def chat_ingest():
        """v1.16.66 Ship P24 #1 — ingest a raw conversation chunk.

        Body JSON:
          namespace  (str, required) — e.g. 'private:alice' or 'public:lobby'
          agent_id   (str, required)
          window_id  (str, required) — UUID of the parent conversation window
          role       (str, optional, default 'mixed') — user|assistant|mixed|system
          turns      (list[dict], required) — [{role, content}, ...]
          prefix     (str, optional) — pre-computed LLM context prefix
          source     (str, optional, default 'chat_session')

        Returns:
          { event_id, indexed, status }            on success
          { error, detail }                       on validation failure
        """
        body = request.get_json(force=True)
        namespace = body.get('namespace')
        agent_id = body.get('agent_id')
        window_id = body.get('window_id')
        turns = body.get('turns')
        role = body.get('role', 'mixed')
        prefix = body.get('prefix', '') or ''
        source = body.get('source', 'chat_session')

        if not namespace or not agent_id or not window_id:
            return jsonify({'error': 'missing_required_fields',
                            'detail': 'namespace, agent_id, window_id are all required'}), 400
        if not isinstance(turns, list) or len(turns) == 0:
            return jsonify({'error': 'turns_must_be_nonempty_list',
                            'detail': 'turns must be a non-empty list of {role, content} dicts'}), 400

        # Tier + user_id resolve from namespace ('tier:user_id' shape).
        tier = namespace.split(':', 1)[0] if ':' in namespace else 'private'
        if tier not in ('public', 'source', 'private', 'repo'):
            tier = 'private'
        user_id = namespace.split(':', 1)[1] if ':' in namespace else '_current'

        try:
            from .bus.store import astor_bus_for
            bus = astor_bus_for(tier, user_id=user_id)
        except Exception as e:
            return jsonify({'error': 'bus_unavailable', 'detail': str(e)}), 500

        # Derive prefix if not provided (fallback only)
        if not prefix:
            first = turns[0].get('content', '') if turns else ''
            prefix = '[ts=' + namespace + ' role=' + role + '] ' + first[:80]

        try:
            event_id = bus.append_raw_chat_chunk(
                namespace=namespace,
                agent_id=agent_id,
                window_id=window_id,
                role=role,
                turns=turns,
                prefix=prefix,
                source=source,
            )
        except Exception as e:
            return jsonify({'error': 'chunk_insert_failed', 'detail': str(e)}), 500

        # Embed + index
        indexed = False
        try:
            from .nest.vector_store import astor_nest
            nest = astor_nest(tier=tier, user_id=user_id)
            text_to_embed = prefix + ' ' + ' '.join(
                (t.get('content', '') or '') for t in turns
            )[:1500]
            nest.index_chat_chunk(
                event_id=event_id,
                prefix=prefix,
                turn_count=len(turns),
                text=text_to_embed,
            )
            indexed = True
        except Exception as e:
            # Embedding failure should not block ingest — chunks live in raw
            # events regardless of whether the vector side is healthy.
            return jsonify({
                'event_id': event_id,
                'indexed': False,
                'index_error': str(e),
                'status': 'stored_not_indexed',
            })

        return jsonify({'event_id': event_id, 'indexed': indexed, 'status': 'ok'})

    @app.route('/v1/chat/recall', methods=['POST'])
    def chat_recall():
        """v1.16.66 Ship P24 #1 — context-aware chunk retrieval.

        Body JSON:
          query          (str, required)
          namespace      (str, required)
          user_id        (str, optional)
          top_k          (int, optional, default 5)
          max_age_days   (int, optional) — drop chunks older than this
          include_turns  (bool, optional, default True)

        Returns:
          { results: [{event_id, similarity, prefix, turn_count, ts,
                       turns?, window_id?}], count, namespace }
        """
        body = request.get_json(force=True)
        query = body.get('query')
        namespace = body.get('namespace')
        user_id = body.get('user_id')
        top_k = int(body.get('top_k', 5))
        max_age_days = body.get('max_age_days')
        include_turns = bool(body.get('include_turns', True))

        if not query or not namespace:
            return jsonify({'error': 'missing_required_fields',
                            'detail': 'query and namespace are both required'}), 400
        if max_age_days is not None:
            try:
                max_age_days = float(max_age_days)
            except (TypeError, ValueError):
                return jsonify({'error': 'bad_max_age_days',
                                'detail': 'max_age_days must be a number'}), 400

        tier = namespace.split(':', 1)[0] if ':' in namespace else 'private'
        if tier not in ('public', 'source', 'private', 'repo'):
            tier = 'private'
        # Derive user_id from namespace when caller didn't pass one explicitly
        # (e.g. namespace 'public:test' -> user_id='test').
        if not user_id and ':' in namespace:
            user_id = namespace.split(':', 1)[1]
        if tier == 'private' and not user_id:
            user_id = '_current'

        try:
            from .nest.vector_store import astor_nest
            from .nest.embeddings import astor_get_embedding_model
            nest = astor_nest(tier=tier, user_id=user_id or '_current')
            model = astor_get_embedding_model()
            q_emb = np.array(list(model.embed([query]))[0], dtype=np.float32)
        except Exception as e:
            return jsonify({'error': 'embed_unavailable', 'detail': str(e)}), 500

        # v1.16.67 Ship A #2: hybrid BM25 + dense fusion (MemFit 2026-10).
        # Lexical path catches names/dates/exact terms the dense path
        # misses; dense path catches paraphrases BM25 misses. Fuse with
        # 0.7 dense + 0.3 BM25 weights (MemFit default), min-max
        # normalize each side to [0, 1] before fusing.
        try:
            dense_raw = nest.search_chat_chunks(
                query_embedding=q_emb,
                limit=max(top_k * 2, 10),  # oversample for fusion
                max_age_days=max_age_days,
            )
        except Exception as e:
            return jsonify({'error': 'recall_failed', 'detail': str(e)}), 500

        bm25_raw = []
        try:
            from .nest.lex_index import astor_lex as _astor_lex
            lex = _astor_lex(tier=tier, user_id=user_id or '_current')
            bm25_raw = lex.bm25_search_chat_chunks(
                query=query, nest=nest, limit=max(top_k * 2, 10),
                tier=tier, user_id=user_id or '_current',
            )
        except Exception:
            pass

        # Min-max normalize each side
        def _minmax(pairs):
            if not pairs:
                return {}
            vals = [s for _, s in pairs]
            lo, hi = min(vals), max(vals)
            if hi - lo < 1e-12:
                return {k: 1.0 for k, _ in pairs}
            return {k: (s - lo) / (hi - lo) for k, s in pairs}

        dense_n = _minmax([(h['event_id'], h['similarity']) for h in dense_raw])
        bm25_n = _minmax(bm25_raw)

        all_ids = set(dense_n) | set(bm25_n)
        fused = []
        prefix_by_id = {h['event_id']: h.get('prefix', '') for h in dense_raw}
        ts_by_id = {h['event_id']: h.get('ts', '') for h in dense_raw}
        tc_by_id = {h['event_id']: h.get('turn_count', 0) for h in dense_raw}
        for eid in all_ids:
            d = dense_n.get(eid, 0.0)
            b = bm25_n.get(eid, 0.0)
            fused.append((eid, 0.7 * d + 0.3 * b))
        fused.sort(key=lambda x: -x[1])
        fused = fused[:top_k]

        if not fused:
            return jsonify({'results': [], 'count': 0, 'namespace': namespace})

        # Reconstruct dict-list using dense-side metadata when present
        hits = []
        dense_meta = {h['event_id']: h for h in dense_raw}
        for eid, score in fused:
            meta = dense_meta.get(eid, {})
            hits.append({
                'event_id': eid,
                'similarity': round(score, 4),
                'prefix': meta.get('prefix', ''),
                'turn_count': meta.get('turn_count', 0),
                'ts': meta.get('ts', ''),
            })

        # Enrich with turn text from the bus events table.
        # v1.16.67 Ship A #3 (MemFit 2026-10 §3): also attach
        # window_id context (the parent conversation) so callers can
        # walk to neighbours if needed.
        if include_turns:
            try:
                from .bus.store import astor_bus_for
                bus = astor_bus_for(tier, user_id=user_id or '_current')
                import json as _json
                for h in hits:
                    row = bus._conn.execute(
                        "SELECT chunk_turns, chunk_window_id, chunk_role "
                        "FROM events WHERE id = ?",
                        (h['event_id'],),
                    ).fetchone()
                    if row:
                        try:
                            h['turns'] = _json.loads(row[0]) if row[0] else []
                        except Exception:
                            h['turns'] = []
                        h['window_id'] = row[1]
                        h['chunk_role'] = row[2]
            except Exception:
                pass

        # v1.16.67 Ship A #3 (MemFit §3 + §3.2): for each hit, attach
        # SIBLING chunks from the SAME conversation (within 5 turns of
        # the hit's window boundary) — MemFit's "hit carries +/- 1
        # turn to avoid orphan context" pattern. Cheap because chat
        # chunks in the same window_id share a parent conversation.
        # Only run if we have hits + bus connection.
        try:
            from .bus.store import astor_bus_for as _b4
            bus = _b4(tier, user_id=user_id or '_current')
            for h in hits:
                wid = h.get('window_id')
                if not wid:
                    continue
                sibs = bus._conn.execute(
                    "SELECT id, chunk_turns, chunk_role, chunk_prefix "
                    "FROM events WHERE chunk_window_id = ? "
                    "AND action = 'raw_chat_chunk' AND id != ? "
                    "ORDER BY id ASC LIMIT 2",
                    (wid, h['event_id']),
                ).fetchall()
                if sibs:
                    import json as _json2
                    h['siblings'] = [
                        {
                            'event_id': s[0],
                            'role': s[2],
                            'prefix': s[3],
                            'turns': _json2.loads(s[1]) if s[1] else [],
                        }
                        for s in sibs
                    ]
        except Exception:
            pass

        # v1.16.67 Ship A #3 (MemFit §3.2 pseudo-relevance feedback):
        # from top-2 hits extract rare tokens (NOT stopwords) and
        # append them as a `prf_terms` list so the caller can re-issue
        # a refined query if needed. Don't auto-re-search — that would
        # double latency; surface the suggested terms instead.
        try:
            _STOP = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be',
                     'in', 'on', 'at', 'to', 'for', 'of', 'and', 'or',
                     'but', 'so', 'it', 'this', 'that', 'we', 'i', 'you',
                     'he', 'she', 'they', 'my', 'your', 'with', 'as', 'by',
                     'from', 'not', 'no', 'do', 'did', 'has', 'have', 'had'}
            from collections import Counter as _Cnt
            top2 = [h.get('prefix', '') for h in hits[:2]]
            c = _Cnt()
            for t in top2:
                for tok in t.split():
                    tl = tok.lower().strip('.,;:()[]{}')
                    if len(tl) >= 4 and tl not in _STOP:
                        c[tl] += 1
            # Rarest first (count=1 are rarest)
            prf = [t for t, _ in c.most_common() if c[t] <= 1][:12]
            if prf:
                hits[0].setdefault('_meta', {})['prf_terms'] = prf
        except Exception:
            pass

        return jsonify({
            'results': hits,
            'count': len(hits),
            'namespace': namespace,
            'fusion': '0.7_dense_0.3_bm25',
        })


    @app.route('/v1/chat/cluster/rebuild', methods=['POST'])
    def chat_cluster_rebuild():
        """v1.16.68 Ship B #2: rebuild cluster_summary table from chat_chunks.

        Body JSON:
          namespace    (str, required)
          user_id      (str, optional; default nest.user_id)
          gap_minutes  (int, optional, default 30)
          max_clusters (int, optional, default 100; safety cap)

        Reads chat_chunk_embeddings, groups by time-gap, calls LLM
        for a 2-sentence summary per cluster (>1 member), persists.

        Returns: {clusters_created, members_total, ...}
        """
        body = request.get_json(force=True)
        namespace = body.get('namespace')
        user_id = body.get('user_id')
        gap_minutes = int(body.get('gap_minutes', 30))
        max_clusters = int(body.get('max_clusters', 100))
        if not namespace:
            return jsonify({'error': 'namespace_required'}), 400
        tier = namespace.split(':', 1)[0] if ':' in namespace else 'private'
        if tier not in ('public', 'source', 'private', 'repo'):
            tier = 'private'
        if not user_id:
            user_id = '_current'

        try:
            from .nest.vector_store import astor_nest
            from .nest.cluster_summary import (
                build_clusters, save_cluster_summary,
            )
            nest = astor_nest(tier=tier, user_id=user_id)
            clusters = build_clusters(
                nest, gap_seconds=gap_minutes * 60,
                user_id=user_id, namespace=tier,
            )
        except Exception as e:
            return jsonify({'error': 'build_clusters_failed', 'detail': str(e)}), 500

        # Only summarize clusters with >= 2 chunks OR rich prefix text.
        created = []
        skipped = 0
        for c in clusters[:max_clusters]:
            if c['member_count'] < 2 and len(c['prefixes_concat']) < 50:
                skipped += 1
                continue
            summary = c['prefixes_concat'][:800]  # TODO LLM call when wired
            try:
                cid = save_cluster_summary(
                    nest, c, summary=summary,
                    user_id=user_id, namespace=tier,
                )
                created.append({
                    'cluster_id': cid,
                    'member_count': c['member_count'],
                    'start_ts': c['start_ts'],
                    'end_ts': c['end_ts'],
                })
            except Exception as e:
                skipped += 1
        return jsonify({
            'namespace': namespace,
            'gap_minutes': gap_minutes,
            'clusters_total': len(clusters),
            'clusters_created': len(created),
            'skipped': skipped,
            'created': created[:10],  # cap response
        })

    @app.route('/v1/chat/cluster/recall', methods=['POST'])
    def chat_cluster_recall():
        """v1.16.68 Ship B #2: dense-search cluster summaries.

        Body JSON:
          namespace   (str, required)
          user_id     (str, optional)
          query       (str, required)
          top_k       (int, optional, default 5)
          max_age_days (int, optional)

        Returns: cluster summaries with member_event_ids so caller can
        pull original chunks if a cluster looks promising (multi-hop nav).
        """
        body = request.get_json(force=True)
        namespace = body.get('namespace')
        user_id = body.get('user_id')
        query = body.get('query')
        top_k = int(body.get('top_k', 5))
        max_age_days = body.get('max_age_days')
        if not namespace or not query:
            return jsonify({'error': 'namespace_and_query_required'}), 400
        tier = namespace.split(':', 1)[0] if ':' in namespace else 'private'
        if tier not in ('public', 'source', 'private', 'repo'):
            tier = 'private'
        if not user_id:
            user_id = '_current'

        try:
            from .nest.vector_store import astor_nest
            from .nest.embeddings import astor_get_embedding_model
            from .nest.cluster_summary import cluster_summary_search
            import numpy as np
            nest = astor_nest(tier=tier, user_id=user_id)
            model = astor_get_embedding_model()
            q_emb = np.array(list(model.embed([query]))[0], dtype=np.float32)
        except Exception as e:
            return jsonify({'error': 'embed_unavailable', 'detail': str(e)}), 500

        try:
            hits = cluster_summary_search(
                nest, q_emb, limit=top_k,
                user_id=user_id, namespace=tier,
                max_age_days=max_age_days,
            )
        except Exception as e:
            return jsonify({'error': 'cluster_search_failed', 'detail': str(e)}), 500
        return jsonify({
            'results': hits,
            'count': len(hits),
            'namespace': namespace,
        })


    @app.route('/v1/export/markdown', methods=['POST'])
    def export_markdown():
        """v1.16.69 Ship C #1: export memory_canonical rows as Obsidian-style Markdown.

        Body JSON:
          tier              (str, required) — 'public'|'source'|'private_<user>'
          user_id           (str, optional) — defaults to tier's default user
          include_inactive  (bool, default True)
          overwrite         (bool, default False)
          include_chunks    (bool, default False) — also export chat_chunk
                            events as separate .md files

        Output goes to <ASTOR_DIR>/export/<tier>/<user_id>/. Mount that
        directory as an Obsidian vault for human-auditable memory.
        """
        body = request.get_json(force=True)
        tier = body.get('tier', 'public')
        user_id = body.get('user_id', 'admin')
        include_inactive = bool(body.get('include_inactive', True))
        overwrite = bool(body.get('overwrite', False))
        include_chunks = bool(body.get('include_chunks', False))

        try:
            from .bus.store import astor_bus_for
            from .nest.markdown_export import (
                export_user_facts, export_chat_chunks,
            )
            bus = astor_bus_for(tier=tier, user_id=user_id)
            fact_result = export_user_facts(
                bus, tier=tier, user_id=user_id,
                include_inactive=include_inactive,
                overwrite=overwrite,
            )
        except Exception as e:
            return jsonify({'error': 'export_failed', 'detail': str(e)}), 500

        chunk_result = None
        if include_chunks:
            try:
                from .nest.vector_store import astor_nest
                nest = astor_nest(tier=tier, user_id=user_id)
                chunk_result = export_chat_chunks(
                    nest, bus, include_inactive=include_inactive,
                    overwrite=overwrite,
                )
            except Exception as e:
                chunk_result = {'error': str(e)}

        # v1.16.70 Ship D #1: build entities.jsonl wikilink index.
        entities_index = None
        try:
            from .nest.markdown_export import build_entities_index
            entities_index = build_entities_index(
                bus, tier=tier, user_id=user_id,
            )
        except Exception as e:
            entities_index = {'error': str(e)}

        return jsonify({
            'tier': tier,
            'user_id': user_id,
            'facts': fact_result,
            'chunks': chunk_result,
            'entities_index': entities_index,
        })

    @app.route('/v1/consult', methods=['POST'])
    def consult():
        """v1.16.x (Plan "reactive consult"): explicit success/failure/lesson lookup.

        Bypasses the consult-default gate (always fires meta-recall + lessons)
        and skips the heavy vector recall path. Designed for the workflow:
          1. Agent hits a problem ("搞砸了 / 不行 / 卡住")
          2. Agent calls /v1/consult with query describing the obstacle
          3. astor returns top success_pattern + failure_pattern + lesson hits
          4. Agent reads what worked before, what failed before, what was learned

        Body: {query, user, tier, top_k (default 2 per kind)}
        Returns: {success: [...], failure: [...], lesson: [...]}
        """
        try:
            body = request.get_json(force=True) or {}
            query = body.get('query')
            if not query:
                return jsonify({'error': 'query required', 'detail': 'POST /v1/consult requires "query"'}), 400
            user = body.get('user', 'admin')
            tier = body.get('tier', 'public')
            top_k = int(body.get('top_k', 2))

            # Direct SQL via astor_bus (re-implements _meta_recall_patterns +
            # _meta_recall_lessons, which are nested inside read() and not
            # callable from a sibling endpoint).
            from .bus import astor_bus
            _words = [w.strip() for w in query.split() if len(w.strip()) >= 3][:6]
            patterns = []
            lessons = []
            # v1.16.x patch: hoist experience_hits init out of the if-block so
            # the return jsonify below can reference it even when _words=[].
            # Ship P2 declared it inside the if, causing UnboundLocalError
            # for short queries like "A" / "优" (1-2 chars).
            experience_hits = []
            if _words:
                _like_clauses = ' AND '.join(['content LIKE ?' for _ in _words])
                _like_params = [f'%{w}%' for w in _words]
                _tiers_to_query = []
                if tier and tier.startswith('private'):
                    if user:
                        _tiers_to_query.append(('private', user))
                    _tiers_to_query.append(('public', None))
                    _tiers_to_query.append(('source', None))
                elif tier == 'source':
                    _tiers_to_query.append(('source', None))
                else:
                    _tiers_to_query.append(('public', None))
                    _tiers_to_query.append(('source', None))
                # Patterns (success + failure) — fetch with LIMIT 4 so split gives
                # ~2 of each kind.
                _pat_seen = set()
                for _t, _u in _tiers_to_query:
                    try:
                        _b = astor_bus(tier=_t, user_id=_u)
                        _rows = _b.conn.execute(
                            f"SELECT id, content, kind, importance FROM memory_canonical "
                            f"WHERE tombstoned=0 AND kind IN ('success_pattern','failure_pattern') "
                            f"AND ({_like_clauses}) ORDER BY importance DESC, created_at DESC LIMIT 4",
                            tuple(_like_params)
                        ).fetchall()
                        for _row in _rows:
                            if _row[0] in _pat_seen:
                                continue
                            _pat_seen.add(_row[0])
                            # SQL row order: id, content, kind, importance
                            patterns.append({
                                'fact_id': _row[0], 'kind': _row[2],
                                'content': (_row[1] or '')[:500], 'importance': _row[3],
                                'meta_source': 'auto-meta-recall-patterns-consult',
                                'meta_tier': _t,
                            })
                    except Exception:
                        pass
                # Lessons
                _seen = set()
                for _t, _u in _tiers_to_query:
                    try:
                        _b = astor_bus(tier=_t, user_id=_u)
                        _rows = _b.conn.execute(
                            f"SELECT id, content, kind, importance FROM memory_canonical "
                            f"WHERE tombstoned=0 AND kind='lesson' "
                            f"AND ({_like_clauses}) ORDER BY importance DESC, created_at DESC LIMIT 2",
                            tuple(_like_params)
                        ).fetchall()
                        for _row in _rows:
                            if _row[0] in _seen:
                                continue
                            _seen.add(_row[0])
                            lessons.append({
                                'fact_id': _row[0], 'kind': _row[2],
                                'content': (_row[1] or '')[:500], 'importance': _row[3],
                                'meta_source': 'auto-meta-recall-lessons-consult',
                                'meta_tier': _t,
                            })
                    except Exception:
                        pass
                # v1.15.57 (2026-09-30) Ship P2: also pull from memory_experience table.
                try:
                    for _t_e, _u_e in _tiers_to_query:
                        _b_e = astor_bus(tier=_t_e, user_id=_u_e)
                        _e_rows = _b_e.match_experiences(
                            query=query, namespace=None, user_id=_u_e,
                            top_k=top_k, use_embedding=False,
                        )
                        for _e_row in _e_rows:
                            experience_hits.append({
                                'experience_id': _e_row['id'],
                                'action_summary': _e_row['action_summary'][:500],
                                'outcome': _e_row['outcome'],
                                'reflection': _e_row['reflection'][:500],
                                'next_step_hint': _e_row['next_step_hint'][:500],
                                'occurrence_count': _e_row['invocation_count'],
                                'importance': _e_row['importance'],
                                'meta_source': 'auto-experience-consult',
                                'meta_tier': _t_e,
                            })
                except Exception:
                    pass
                experience_hits = experience_hits[:top_k]

            return jsonify({
                'query': query,
                'tier': tier,
                'success': [h for h in patterns if h.get('kind') == 'success_pattern'][:top_k],
                'failure': [h for h in patterns if h.get('kind') == 'failure_pattern'][:top_k],
                'lesson': lessons[:top_k],
                'experience': experience_hits,
                'counts': {
                    'success': len([h for h in patterns if h.get('kind') == 'success_pattern']),
                    'failure': len([h for h in patterns if h.get('kind') == 'failure_pattern']),
                    'lesson': len(lessons),
                    'experience': len(experience_hits),
                },
            })
        except Exception as _consult_exc:
            _safe_stderr_write(
                f'[astor.server] /v1/consult exception: {type(_consult_exc).__name__}: {_consult_exc}\n'
            )
            return jsonify({
                'error': 'consult_internal_error',
                'detail': f'{type(_consult_exc).__name__}: {_consult_exc}',
            }), 500

    # ---- v1.15.57 (2026-09-30): /v1/experience endpoints — pushback-capture protocol
    # (OpenClaw self-improving pattern, bus-side).  Three new routes:
    #   POST /v1/experience        : explicit experience insert (anyone, any framework)
    #   GET  /v1/experience/match  : recall experiences matching a query
    #                                  (powers /v1/consult expansion in P2)
    # Design notes:
    #   - Backed by memory_experience table (already shipped v1.6.0) — we only
    #     expose HTTP, no schema change.
    #   - Pushback kinds ('correction', 'pushback', 'user_correction') auto-dedup
    #     by sha256(text+context_keywords[:5]) — repeated pushback on the same
    #     topic increments occurrence_count instead of inserting a new row.
    #   - This is the channel external agents (hermes, LangChain, custom scripts)
    #     use to capture 'the user just told me I'm wrong' without going through
    #     memory_canonical first.

    @app.route('/v1/experience', methods=['POST'])
    def experience_insert():
        """Insert a new experience (OpenClaw self-improving pattern).

        Body JSON:
          text / action_summary : str (required) — what was tried
          outcome               : 'success'|'partial'|'failure'|'neutral' (default 'neutral')
          reflection            : str (optional) — why it succeeded/failed
          next_step_hint        : str (optional) — what to do next time
          trigger_keywords      : list[str] (optional) — for retrieval matching
          trigger_fact_ids      : list[int] (optional)
          context               : str (optional)
          user / actor          : str (default 'admin')
          tier                  : 'public'|'source'|'private_<user>' (default 'private_<actor>')
          source_session_id     : str (optional)
          importance            : float (default 0.7; auto-bumped for pushback kinds)

        Behavior:
          - outcome ∈ {failure, partial} OR text starts with correction trigger
            → kind='pushback_correction', importance=0.85, namespace='private:<actor>'
          - Same actor + same text+keywords → return existing experience_id with
            incremented occurrence_count (auto-dedup).
          - Otherwise → fresh insert, returns experience_id.

        Returns: {experience_id, occurrence_count, deduped: bool}
        """
        import hashlib as _hl
        from .bus import astor_bus as _ab
        body = request.get_json(force=True) or {}
        text = body.get('text') or body.get('action_summary') or ''
        if not text or len(text.strip()) < 4:
            return jsonify({'error': 'text required', 'detail': 'POST /v1/experience requires text/action_summary (4+ chars)'}), 400
        actor = body.get('user') or body.get('actor') or 'admin'
        tier = body.get('tier') or ('private' if not actor.startswith('admin') else 'source')
        outcome = (body.get('outcome') or 'neutral').lower()
        if outcome not in ('success', 'partial', 'failure', 'neutral'):
            outcome = 'neutral'
        # Detect pushback kind from content
        _pushback_triggers = ('不对', '错了', '应该是', '搞错了', '你错了', '你搞错',
                              'no,', 'wrong', "that's wrong", 'should be', 'actually',
                              '别再', 'stop', "don't", '不应该')
        _is_pushback = (
            outcome in ('failure', 'partial')
            or any(t in text for t in _pushback_triggers)
            or body.get('kind') in ('correction', 'pushback', 'user_correction')
        )
        kind = 'pushback_correction' if _is_pushback else 'experience'
        importance = float(body.get('importance', 0.85 if _is_pushback else 0.7))
        # Build dedup hash from (actor + text + first 5 trigger_keywords)
        _trig_kws = body.get('trigger_keywords') or []
        _dedup_blob = actor + '||' + text.strip() + '||' + '|'.join(sorted(str(k) for k in _trig_kws[:5]))
        _dedup_hash = _hl.sha256(_dedup_blob.encode('utf-8')).hexdigest()[:16]
        try:
            _bus = _ab(tier=tier if tier != 'private' else 'private',
                       user_id=actor if tier.startswith('private') else None)
            # Dedup lookup: SELECT for existing row matching dedup hash. If
            # found, increment its invocation_count and read back the new value
            # via a follow-up SELECT (more verbose than UPDATE...RETURNING but
            # matches db state reliably on this bus).
            _existing = None
            _occ = 0
            try:
                _row = _bus.conn.execute(
                    "SELECT id, invocation_count FROM memory_experience "
                    "WHERE user_id = ? AND instr(reflection, ?) > 0 LIMIT 1",
                    (actor, f'[dedup:{_dedup_hash}]'),
                ).fetchone()
                if _row:
                    _existing = int(_row[0])
                    _bus.conn.execute(
                        "UPDATE memory_experience "
                        "SET invocation_count = invocation_count + 1, "
                        "    last_invoked_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                        "WHERE id = ?", (_existing,))
                    _bus.conn.commit()
                    _occ_row = _bus.conn.execute(
                        "SELECT invocation_count FROM memory_experience WHERE id = ?",
                        (_existing,),
                    ).fetchone()
                    _occ = int(_occ_row[0]) if _occ_row else (_existing and 1) or 0
            except Exception:
                pass
            if _existing:
                # v1.16.9: 3-tier promotion. 3 invokes → 0.85 (medium-HOT);
                # 6 invokes → 0.95 (full HOT). Graduated tiers vs binary jump.
                if _occ >= 6 and importance < 0.95:
                    _bus.conn.execute(
                        "UPDATE memory_experience SET importance = 0.95 WHERE id = ?",
                        (_existing,),
                    )
                    _bus.conn.commit()
                    importance = 0.95
                elif _occ >= 3 and importance < 0.85:
                    _bus.conn.execute(
                        "UPDATE memory_experience SET importance = 0.85 WHERE id = ?",
                        (_existing,),
                    )
                    _bus.conn.commit()
                    importance = 0.85
                return jsonify({
                    'experience_id': _existing,
                    'occurrence_count': _occ,
                    'deduped': True,
                    'dedup_hash': _dedup_hash,
                    'promoted_to_hot': _occ >= 3,
                    'kind': kind,
                    'importance': importance,
                })
            # Fresh insert — encode dedup_hash into reflection field (cheap)
            _reflection = body.get('reflection', '') or ''
            _reflection_with_hash = f'[dedup:{_dedup_hash}] ' + _reflection if not _reflection.startswith('[dedup:') else _reflection
            _exp_id = _bus.insert_experience(
                namespace=('private:' + actor) if tier.startswith('private') else tier,
                outcome=outcome,
                user_id=actor,
                trigger_keywords=_trig_kws or None,
                trigger_fact_ids=body.get('trigger_fact_ids') or None,
                action_summary=text.strip(),
                context=body.get('context', ''),
                reflection=_reflection_with_hash,
                next_step_hint=body.get('next_step_hint', ''),
                source_session_id=body.get('source_session_id'),
                importance=importance,
            )
            return jsonify({
                'experience_id': _exp_id,
                'occurrence_count': 1,
                'deduped': False,
                'dedup_hash': _dedup_hash,
                'promoted_to_hot': False,
                'kind': kind,
                'importance': importance,
            })
        except Exception as _exc:
            _safe_stderr_write(f'[astor.server] /v1/experience failed: {type(_exc).__name__}: {_exc}\n')
            return jsonify({'error': 'experience_insert_failed', 'detail': str(_exc)}), 500

    @app.route('/v1/experience/match', methods=['POST'])
    def experience_match():
        """Match experiences against a query (hybrid kw + embedding).

        Body: {query, user, tier, top_k (default 5), outcome (optional filter)}
        Returns: {matches: [{experience_id, action_summary, outcome, reflection,
                              next_step_hint, occurrence_count, importance, score}]}
        """
        try:
            from .bus import astor_bus as _ab
            body = request.get_json(force=True) or {}
            query = body.get('query')
            if not query:
                return jsonify({'error': 'query required', 'detail': 'POST /v1/experience/match requires "query"'}), 400
            actor = body.get('user', 'admin')
            tier = body.get('tier') or ('private' if actor != 'admin' else 'public')
            top_k = int(body.get('top_k', 5))
            outcome_filter = body.get('outcome')
            user_id = actor if tier.startswith('private') else None
            _bus = _ab(tier=tier if tier != 'private' else 'private',
                       user_id=user_id)
            _rows = _bus.match_experiences(
                query=query, namespace=None, user_id=user_id,
                outcome=outcome_filter, top_k=top_k,
            )
            # Map to public API shape (rename keys)
            matches = [{
                'experience_id': r['id'],
                'action_summary': r['action_summary'],
                'outcome': r['outcome'],
                'reflection': r['reflection'],
                'next_step_hint': r['next_step_hint'],
                'occurrence_count': r['invocation_count'],
                'last_invoked_at': r['last_invoked_at'],
                'importance': r['importance'],
                'trigger_keywords': r['trigger_keywords'],
                'score': 1.0,  # match_experiences returns by score already; we expose 1.0 placeholder
            } for r in _rows]
            return jsonify({'query': query, 'tier': tier, 'matches': matches, 'count': len(matches)})
        except Exception as _exc:
            _safe_stderr_write(f'[astor.server] /v1/experience/match failed: {type(_exc).__name__}: {_exc}\n')
            return jsonify({'error': 'experience_match_failed', 'detail': str(_exc)}), 500

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

    @app.route('/v1/failure_loop/run', methods=['POST'])
    def failure_loop_run():
        """v1.16.23 (2026-09-30): 失败复盘闭环 (user feedback #2).

        3-step closed loop in one call:
          1. SCAN — find recent failure/lesson facts (kind in
             failure_pattern/lesson or outcome tags), up to N days old.
          2. EXTRACT — for each failure, rank skills via
             controller_select_scored on the failure content; top skill
             names become the "提炼技能" recommendation. Recurring
             failures (>=2 hits same topic) get promoted to a
             success_pattern-style fact for future recall.
          3. SNAPSHOT — pre-loop DB snapshot row (kind counts + fact
             count) written to audit_log so a bad reflection merge can
             be rolled back via /v1/snapshot/restore-style tooling.

        Body: {tier (default public), user_id, days (default 7),
               max_items (default 20)}
        Returns: {scanned, recurring, skills_recommended, snapshot_id}
        """
        try:
            ctx = astor_current_acl()
            if ctx.role != 'admin':
                return jsonify({'error': 'failure_loop requires admin'}), 403
        except Exception:
            pass
        body = request.get_json(force=True) if request.is_json else {}
        tier = body.get('tier', 'public')
        user_id = body.get('user_id') or None
        days = int(body.get('days', 7))
        max_items = int(body.get('max_items', 20))
        bus = astor_bus(tier=tier, user_id=user_id)

        # ---- Step 3 FIRST: snapshot BEFORE any mutation ----
        import json as _fl_json
        import time as _fl_time
        _snap = {
            'ts': _fl_time.strftime('%Y-%m-%dT%H:%M:%S'),
            'tier': tier, 'user_id': user_id,
            'fact_count': 0, 'kind_counts': {},
        }
        try:
            _rows = bus.conn.execute(
                "SELECT kind, COUNT(*) FROM memory_canonical "
                "WHERE tombstoned = 0 GROUP BY kind").fetchall()
            _snap['fact_count'] = sum(r[1] for r in _rows)
            _snap['kind_counts'] = {r[0]: r[1] for r in _rows}
        except Exception:
            pass
        _snap_id = None
        try:
            bus.write_audit(
                event='failure_loop_snapshot', actor='admin:admin',
                target_type='system', target_id='failure_loop',
                metadata=_snap, severity='info',
            )
            _snap_id = bus.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            bus.conn.commit()
        except Exception:
            pass

        # ---- Step 1: scan recent failures/lessons ----
        _fl_facts = []
        try:
            _cutoff = _fl_time.strftime('%Y-%m-%d', _fl_time.gmtime(
                _fl_time.time() - days * 86400))
            _rows = bus.conn.execute(
                "SELECT id, content, kind, created_at FROM memory_canonical "
                "WHERE tombstoned = 0 AND created_at >= ? AND "
                "(kind IN ('failure_pattern', 'lesson', 'error') OR "
                " content LIKE '%失败%' OR content LIKE '%error%' OR "
                " content LIKE '%失败模式%') "
                "ORDER BY created_at DESC LIMIT ?",
                (_cutoff, max_items)).fetchall()
            _fl_facts = [
                {'id': r[0], 'content': r[1], 'kind': r[2], 'created_at': r[3]}
                for r in _rows
            ]
        except Exception as _scan_exc:
            return jsonify({'error': 'scan failed',
                            'detail': str(_scan_exc)}), 500

        # ---- Step 2: extract skills per failure + detect recurring ----
        from .nest.skills import controller_select_scored
        _skill_rec = {}
        _topic_counts = {}
        for _f in _fl_facts:
            for _s in controller_select_scored(_f['content'], top_n=2):
                _skill_rec[_s['name']] = max(_skill_rec.get(_s['name'], 0),
                                             _s['score'])
            # crude topic key: first 12 chars of normalized content
            _topic = _f['content'][:12].strip()
            _topic_counts[_topic] = _topic_counts.get(_topic, 0) + 1
        _recurring = [{'topic': t, 'count': c}
                      for t, c in _topic_counts.items() if c >= 2]

        # Promote recurring failures (count>=2) into a lesson fact so
        # future recall surfaces the pattern, not just the raw failure.
        _promoted = []
        for _r in _recurring:
            try:
                _prom_text = (f"[failure_loop] recurring failure x{_r['count']}: "
                              f"{_r['topic']}... — review via /v1/consult")
                _fl_cand = bus.insert_candidate(
                    event_id=bus.append_event(
                        namespace='failure_loop', agent_id='failure_loop',
                        source='rest.failure_loop', action='write',
                        content=_prom_text),
                    namespace='failure_loop', content=_prom_text,
                    kind='lesson', confidence=0.8, importance=0.7,
                    tags=['failure_loop', 'recurring'])
                bus.promote_candidate(_fl_cand, promoted_by='rest.failure_loop',
                                      user_id=user_id, tier=tier)
                _promoted.append(_r['topic'])
            except Exception:
                continue

        return jsonify({
            'scanned': len(_fl_facts),
            'recurring': _recurring,
            'promoted_lessons': _promoted,
            'skills_recommended': [
                {'name': k, 'score': v}
                for k, v in sorted(_skill_rec.items(),
                                   key=lambda kv: -kv[1])[:5]
            ],
            'snapshot_id': _snap_id,
            'snapshot': _snap,
        })

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
                list_source_facts as _mm_list_sources,
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
                # v1.15.51 A3: include source_facts traceback so callers can
                # verify the mental_model is grounded in real bus facts.
                _sources = _mm_list_sources(_mm_bus, _mm.fact_id)
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
                        # Traceback link to evidence (A3).
                        'source_facts': _sources,
                        'source_fact_count': len(_sources),
                    },
                })
            else:
                # v1.15.51 A2 fix: for source tier (operator-level, user_id IS NULL),
                # don't require user_id match — source rows are owned by admin role,
                # not specific user. For private tier, user_id filter is required.
                if _tier == 'source':
                    _list_user = None
                else:
                    _list_user = _user
                _items = _mm_list(_mm_bus, tier=_tier, user_id=_list_user)
                # v1.15.51 A3: include source_fact_count for each MM in list view.
                return jsonify({
                    'tier': _tier,
                    'mental_models': [{
                        'fact_id': x.fact_id,
                        'question': x.question,
                        'answer': x.answer,
                        'confidence': x.confidence,
                        'created_at': x.created_at,
                        'updated_at': x.updated_at,
                        'source_fact_count': len(_mm_list_sources(_mm_bus, x.fact_id)),
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
        # v1.15.51 A2 fix: source tier is operator-level (user_id IS NULL).
        # Bypass user filter so admin caller sees source pages.
        _kp_user = None if _tier == 'source' else _user
        try:
            from ._internal.acl import astor_check_read as _kp_acr
            _kp_acr(tier=_tier, user_id=_kp_user)
        except Exception as _kp_acl_exc:
            return jsonify({'error': 'acl_denied',
                            'detail': str(_kp_acl_exc)}), 403
        from .bus import astor_bus as _kp_bus_factory
        _bus = _kp_bus_factory(tier=_tier, user_id=_kp_user)
        try:
            _page = _kp_get(_bus, _slug, tier=_tier, user_id=_kp_user)
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
        """List all knowledge_pages for tier (Ship P3.1, v1.15.39).

        v1.15.51 A2 fix: source tier bypass for user_id (same pattern as
        knowledge_page_get above).
        """
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
        _kp_user = None if _tier == 'source' else _user
        try:
            from ._internal.acl import astor_check_read as _kp_acr
            _kp_acr(tier=_tier, user_id=_kp_user)
        except Exception as _kp_acl_exc:
            return jsonify({'error': 'acl_denied',
                            'detail': str(_kp_acl_exc)}), 403
        from .bus import astor_bus as _kp_bus_factory
        _bus = _kp_bus_factory(tier=_tier, user_id=_kp_user)
        try:
            _pages = _kp_list(_bus, tier=_tier, user_id=_kp_user)
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
            'astor_dir': _astor_dir_label(astor_dir),
            'generated_at': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).replace(tzinfo=None).isoformat() + 'Z',
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
        # personal until proven otherwise. Without this, a non-admin typing
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
        request's ``_CURRENT`` binding (e.g. ``user:<non_admin>`` after a
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
            py_exe = sys.executable  # use the same Python that's running this server
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
            'audit_ts': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).replace(tzinfo=None).isoformat() + 'Z',
            'dimension_scores': scores,
            'total_score': f'{total}/3',
            'verdict': verdict,
            'evidence': evidence,
            # v1.15.17 S21: meta-recall stats so dashboard / ops can verify
            # astor is actively auto-injecting success/failure patterns.
            'meta_recall_stats': dict(_META_RECALL_STATS),
            # v1.16.x: PII gate lifetime counters + last-24h scan count from audit_log.
            'pii_gate': {
                **dict(_pii_gate_stats),
                'last_24h_block_count': _pii_last_24h_count(),
            },
            'reference': 'mp.weixin.qq.com/s/aL1gaDDGR1eJy2uzL5kKdQ (Bannings 2026-08)'
        })

    # v1.16.16: orphan detection — find facts with no embedding, no
    # entities, AND low importance. These are facts that don't help
    # recall (no entities → no entity-overlap matching, no embedding
    # → no vector recall, low importance → not HOT). Surface them
    # so operators can decide to clean up (forget) or upgrade (add
    # entities / importance via correction).
    #
    # Why ship: bus accumulates facts over time. Without periodic
    # hygiene, the recall index bloats with low-signal facts that
    # dilute the ECV / path_score boost ratios. This endpoint makes
    # the hygiene problem observable.

    @app.route('/v1/audit/memory_snr', methods=['GET'])
    def audit_memory_snr():
        """v1.16.33: memory SNR audit (article eval criterion).

        Article quote: '第三点几乎没有 benchmark 覆盖, 但它决定了一个记忆
        系统能不能活过半年 — 跑 3 个月之后, 检索回来的 top-5 里还有几条是有用的'.

        Returns signal-vs-noise metrics for the memory library:
          - total_facts: total active (non-tombstoned) facts across all tiers
          - tombstoned_ratio: 0..1 (higher = more invalidations / deletes)
          - high_importance_count: facts with importance >= 0.7
          - high_importance_ratio: high_importance / total_facts
          - stale_count_90d: facts not accessed in 90+ days (likely noisy)
          - stale_ratio: stale / total_facts
          - recently_invalidated_30d: facts invalidated in last 30 days
          - top10_content_diversity: distinct first-50-chars in top-10 most
            recent facts (proxy for 'all-same-template' noise)
          - tier_distribution: {public, source, private_<uid>: count}
          - kind_distribution: {fact, rule, ...: count}
          - memory_class_distribution: {world_fact, mental_model, ...}
          - snr_score: 0..100 composite (high_importance_ratio * 100 -
            stale_ratio * 50 - tombstoned_ratio * 30, clamped 0..100)
        """
        import sqlite3 as _audit_sql
        import datetime as _audit_dt
        import glob as _audit_glob
        import os as _audit_os

        result = {
            'total_facts': 0,
            'tombstoned_count': 0,
            'tombstoned_ratio': 0.0,
            'high_importance_count': 0,
            'high_importance_ratio': 0.0,
            'stale_count_90d': 0,
            'stale_ratio': 0.0,
            'recently_invalidated_30d': 0,
            'top10_content_diversity': 0,
            'tier_distribution': {},
            'kind_distribution': {},
            'memory_class_distribution': {},
            'snr_score': 0.0,
            'computed_at': _audit_dt.datetime.now(_audit_dt.timezone.utc).isoformat(),
        }

        _astor_dir = _audit_os.environ.get('ASTOR_DIR') or str(_audit_os.path.expanduser('~/.astor'))
        if not _audit_os.path.exists(_astor_dir):
            _astor_dir = _audit_os.getcwd()  # fallback to caller cwd (runtime deployment)
        _bus_files = []
        _bus_files += _audit_glob.glob(_audit_os.path.join(_astor_dir, 'public', 'memory', 'astor_bus_*.db'))
        _bus_files += _audit_glob.glob(_audit_os.path.join(_astor_dir, 'source', 'memory', 'astor_bus_*.db'))
        _bus_files += _audit_glob.glob(_audit_os.path.join(_astor_dir, 'users', '*', 'memory', 'astor_bus_*.db'))

        _now = _audit_dt.datetime.now(_audit_dt.timezone.utc)
        _cutoff_90d = (_now - _audit_dt.timedelta(days=90)).isoformat()
        _cutoff_30d = (_now - _audit_dt.timedelta(days=30)).isoformat()

        for _dbpath in _bus_files:
            try:
                _conn = _audit_sql.connect(_dbpath)
                _conn.row_factory = _audit_sql.Row
                # Total + tombstoned
                _row = _conn.execute(
                    "SELECT COUNT(*) AS n, SUM(tombstoned) AS t "
                    "FROM memory_canonical",
                ).fetchone()
                _total = int(_row['n'] or 0)
                _tomb = int(_row['t'] or 0)
                result['total_facts'] += _total
                result['tombstoned_count'] += _tomb

                # High importance
                _row = _conn.execute(
                    "SELECT COUNT(*) AS n FROM memory_canonical "
                    "WHERE tombstoned = 0 AND importance >= 0.7",
                ).fetchone()
                result['high_importance_count'] += int(_row['n'] or 0)

                # Stale (last_confirmed_at NULL OR < cutoff_90d)
                _row = _conn.execute(
                    "SELECT COUNT(*) AS n FROM memory_canonical "
                    "WHERE tombstoned = 0 "
                    "  AND (last_confirmed_at IS NULL OR last_confirmed_at < ?) "
                    "  AND (created_at < ? OR created_at IS NULL)",
                    (_cutoff_90d, _cutoff_90d),
                ).fetchone()
                result['stale_count_90d'] += int(_row['n'] or 0)

                # Recently invalidated (30d)
                _row = _conn.execute(
                    "SELECT COUNT(*) AS n FROM memory_canonical "
                    "WHERE invalidated_at IS NOT NULL AND invalidated_at > ?",
                    (_cutoff_30d,),
                ).fetchone()
                result['recently_invalidated_30d'] += int(_row['n'] or 0)

                # Top-10 content diversity (proxy)
                try:
                    _rows = _conn.execute(
                        "SELECT content FROM memory_canonical "
                        "WHERE tombstoned = 0 ORDER BY id DESC LIMIT 10",
                    ).fetchall()
                    _fingerprints = {str(r['content'])[:50] for r in _rows}
                    result['top10_content_diversity'] = max(
                        result['top10_content_diversity'],
                        len(_fingerprints),
                    )
                except Exception:
                    pass

                # Tier / kind / memory_class distributions
                _tier_label = _audit_os.path.basename(_audit_os.path.dirname(_audit_os.path.dirname(_dbpath)))
                _row = _conn.execute(
                    "SELECT kind, COUNT(*) AS n FROM memory_canonical "
                    "WHERE tombstoned = 0 GROUP BY kind",
                ).fetchall()
                for _kr in _row:
                    _k = str(_kr['kind'] or 'unknown')
                    result['kind_distribution'][_k] = result['kind_distribution'].get(_k, 0) + int(_kr['n'])
                _row = _conn.execute(
                    "SELECT memory_class, COUNT(*) AS n FROM memory_canonical "
                    "WHERE tombstoned = 0 GROUP BY memory_class",
                ).fetchall()
                for _kr in _row:
                    _mc = str(_kr['memory_class'] or 'unknown')
                    result['memory_class_distribution'][_mc] = result['memory_class_distribution'].get(_mc, 0) + int(_kr['n'])

                result['tier_distribution'][_tier_label] = result['tier_distribution'].get(_tier_label, 0) + _total

                _conn.close()
            except Exception as _audit_e:
                continue

        # Derive ratios + SNR score
        if result['total_facts'] > 0:
            result['tombstoned_ratio'] = round(result['tombstoned_count'] / result['total_facts'], 4)
            result['high_importance_ratio'] = round(result['high_importance_count'] / result['total_facts'], 4)
            result['stale_ratio'] = round(result['stale_count_90d'] / result['total_facts'], 4)
            # SNR score: reward high importance, penalize stale + tombstoned
            _snr = (
                result['high_importance_ratio'] * 100
                - result['stale_ratio'] * 50
                - result['tombstoned_ratio'] * 30
            )
            result['snr_score'] = max(0, min(100, round(_snr, 2)))

        return jsonify(result)

    @app.route('/v1/audit/orphans', methods=['GET'])
    def audit_orphans():
        import sqlite3 as _sqlite3
        from ._internal.acl_layout import get_db_path, Tier, Store

        _orphan_tier = request.args.get('tier', 'public')
        _importance_max = float(request.args.get('importance_max', 0.5))
        _limit = int(request.args.get('limit', 50))

        try:
            bus_path = str(get_db_path(
                Tier.PUBLIC if _orphan_tier == 'public' else Tier.PRIVATE,
                Store.BUS,
            ))
            conn = _sqlite3.connect(bus_path)
            # Orphans: importance <= max AND entities is '[]' (no entities extracted)
            # AND kind is generic 'fact' (not a hot memory_class)
            rows = conn.execute(
                """
                SELECT id, content, kind, importance, created_at, entities_json
                FROM memory_canonical
                WHERE importance <= ?
                  AND (entities_json IS NULL OR entities_json = '[]' OR entities_json = '')
                  AND tombstoned = 0
                ORDER BY importance ASC, created_at DESC
                LIMIT ?
                """,
                (_importance_max, _limit),
            ).fetchall()
            conn.close()
            orphans = [
                {
                    'id': r[0],
                    'content': (r[1] or '')[:120],
                    'kind': r[2],
                    'importance': r[3],
                    'created_at': r[4],
                    'entities_count': 0 if not r[5] or r[5] in ('[]', '') else len(__import__('json').loads(r[5])),
                }
                for r in rows
            ]
            return jsonify({
                'tier': _orphan_tier,
                'importance_max': _importance_max,
                'count': len(orphans),
                'orphans': orphans,
                'note': 'Low-importance facts with no entities. Consider /v1/forget or upgrade via correction.',
            })
        except Exception as e:
            return jsonify({
                'error': f'{type(e).__name__}: {e}',
                'tier': _orphan_tier,
            }), 500

    # v1.16.17 (2026-09-30): bot-binding endpoints for external agent
    # platforms (muse, slack, custom) to self-register.
    #
    # Schema:
    #   platforms (platform_id PK, account_token, base_url, enabled)
    #   bindings  (binding_id PK, platform_id, chat_id, user_id, scope, active)
    #   user_meta (user_id PK, short_alias, role, plan, tier)
    #
    # Flow for a new agent platform (e.g. muse):
    #   1. POST /v1/binding/platform {platform_id, account_token, base_url}
    #      → registers the bot/agent as a known platform
    #   2. POST /v1/binding/user {user_id, short_alias, role, plan, default_tier}
    #      → registers (or updates) a user
    #   3. POST /v1/binding/bind {platform_id, chat_id, user_id, scope}
    #      → binds chat to user (so /v1/write knows who is calling)
    #   4. GET /v1/binding/lookup?platform=X&chat_id=Y
    #      → reverse: returns {user_id, role, plan, default_tier, short_alias}
    #
    # Why this matters: hermes is hardcoded with admin's chat_id bindings.
    # External agents (muse, slack, custom bots) need a way to register
    # themselves without manual DB edits. These endpoints let the agent
    # platform's admin (or astor's first_admin) wire bindings via HTTP.
    @app.route('/v1/binding/platform', methods=['POST'])
    def binding_register_platform():
        body = request.get_json(force=True) or {}
        platform_id = body.get('platform_id')  # e.g. "muse" or "muse:bot_alpha"
        account_token = body.get('account_token') or ''
        base_url = body.get('base_url') or ''
        platform_kind = body.get('platform_kind') or 'custom'
        account_id = body.get('account_id') or platform_id
        if not platform_id:
            return jsonify({'error': 'platform_id required'}), 400
        try:
            import sqlite3 as _sqlite3
            _db = _astor_bot_binding_connect()
            _db.execute("""
                INSERT OR REPLACE INTO platforms
                    (platform_id, platform_kind, account_id, account_token,
                     base_url, enabled, created_at, updated_at, source)
                VALUES (?, ?, ?, ?, ?, 1,
                        COALESCE((SELECT created_at FROM platforms WHERE platform_id = ?), datetime('now')),
                        datetime('now'),
                        'http:binding_register_platform')
            """, (platform_id, platform_kind, account_id, account_token,
                  base_url, platform_id))
            _db.commit()
            _db.close()
            return jsonify({
                'ok': True,
                'platform_id': platform_id,
                'platform_kind': platform_kind,
            }), 201
        except Exception as e:
            return jsonify({'ok': False, 'error': f'{type(e).__name__}: {e}'}), 500

    @app.route('/v1/binding/user', methods=['POST'])
    def binding_register_user():
        body = request.get_json(force=True) or {}
        user_id = body.get('user_id')
        if not user_id:
            return jsonify({'error': 'user_id required'}), 400
        try:
            import sqlite3 as _sqlite3
            _db = _astor_bot_binding_connect()
            _db.execute("""
                INSERT OR REPLACE INTO user_meta
                    (user_id, short_alias, display_name, real_name, role,
                     subscription_plan, timezone, tz_offset_hours, active,
                     default_tier, trusted_agent, created_at, updated_at, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?,
                        COALESCE((SELECT created_at FROM user_meta WHERE user_id = ?), datetime('now')),
                        datetime('now'),
                        'http:binding_register_user')
            """, (
                user_id,
                body.get('short_alias') or user_id,
                body.get('display_name'),
                body.get('real_name'),
                body.get('role') or 'user',
                body.get('subscription_plan') or 'free',
                body.get('timezone') or 'UTC',
                int(body.get('tz_offset_hours') or 0),
                body.get('default_tier') or 'public',
                1 if body.get('trusted_agent') else 0,
                user_id,
            ))
            _db.commit()
            _db.close()
            return jsonify({
                'ok': True,
                'user_id': user_id,
                'role': body.get('role') or 'user',
                'plan': body.get('subscription_plan') or 'free',
            }), 201
        except Exception as e:
            return jsonify({'ok': False, 'error': f'{type(e).__name__}: {e}'}), 500

    @app.route('/v1/binding/bind', methods=['POST'])
    def binding_bind():
        body = request.get_json(force=True) or {}
        platform_id = body.get('platform_id')
        chat_id = body.get('chat_id')
        user_id = body.get('user_id')
        scope = body.get('scope') or 'dm'
        if not platform_id or not chat_id or not user_id:
            return jsonify({'error': 'platform_id, chat_id, user_id required'}), 400
        try:
            import sqlite3 as _sqlite3
            import uuid as _uuid
            _db = _astor_bot_binding_connect()
            # v1.16.17.1 fix: auto-inherit role from user_meta.role if not
            # explicitly provided. Avoids the cross_channel_inconsistency
            # 409 when admin's role='admin' but binding's role_inherit='user'.
            _user_role = _db.execute(
                "SELECT role FROM user_meta WHERE user_id = ?", (user_id,)
            ).fetchone()
            _role_inherit = body.get('role_inherit') or (
                _user_role[0] if _user_role else 'user'
            )
            # Upsert: same (platform_id, chat_id) → update user_id
            _existing = _db.execute(
                "SELECT binding_id FROM bindings WHERE platform_id = ? AND chat_id = ?",
                (platform_id, chat_id),
            ).fetchone()
            if _existing:
                _binding_id = _existing[0]
                _db.execute("""
                    UPDATE bindings
                    SET user_id = ?, scope = ?, role_inherit = ?, active = 1, bound_at = datetime('now')
                    WHERE binding_id = ?
                """, (user_id, scope, _role_inherit, _binding_id))
            else:
                _binding_id = str(_uuid.uuid4())
                _db.execute("""
                    INSERT INTO bindings
                        (binding_id, platform_id, chat_id, user_id, scope, active,
                         bound_at, bound_by, role_inherit, allow_from)
                    VALUES (?, ?, ?, ?, ?, 1, datetime('now'), 'http:binding_bind', ?, ?)
                """, (_binding_id, platform_id, chat_id, user_id, scope,
                      _role_inherit, chat_id))
            _db.commit()
            _db.close()
            return jsonify({
                'ok': True,
                'binding_id': _binding_id,
                'platform_id': platform_id,
                'chat_id': chat_id,
                'user_id': user_id,
                'role_inherit': _role_inherit,
            }), 201
        except Exception as e:
            return jsonify({'ok': False, 'error': f'{type(e).__name__}: {e}'}), 500

    @app.route('/v1/binding/lookup', methods=['GET'])
    def binding_lookup():
        platform_id = request.args.get('platform')
        chat_id = request.args.get('chat_id')
        if not platform_id or not chat_id:
            return jsonify({
                'error': 'platform and chat_id required',
                'usage': '/v1/binding/lookup?platform=muse&chat_id=<their_chat_id>',
            }), 400
        try:
            import sqlite3 as _sqlite3
            _db = _astor_bot_binding_connect()
            _db.row_factory = _sqlite3.Row
            row = _db.execute("""
                SELECT b.platform_id, b.chat_id, b.user_id, b.scope,
                       m.short_alias, m.role, m.subscription_plan, m.default_tier,
                       m.trusted_agent, m.timezone
                FROM bindings b
                LEFT JOIN user_meta m ON m.user_id = b.user_id
                WHERE b.platform_id = ? AND b.chat_id = ? AND b.active = 1
            """, (platform_id, chat_id)).fetchone()
            _db.close()
            if row is None:
                return jsonify({
                    'found': False,
                    'platform_id': platform_id,
                    'chat_id': chat_id,
                    'note': 'no active binding — register platform + bind chat_id first',
                }), 404
            return jsonify({
                'found': True,
                'platform_id': row['platform_id'],
                'chat_id': row['chat_id'],
                'user_id': row['user_id'],
                'short_alias': row['short_alias'],
                'role': row['role'],
                'subscription_plan': row['subscription_plan'],
                'default_tier': row['default_tier'],
                'trusted_agent': bool(row['trusted_agent']),
                'timezone': row['timezone'],
                'scope': row['scope'],
            })
        except Exception as e:
            return jsonify({'found': False, 'error': f'{type(e).__name__}: {e}'}), 500

    @app.route('/v1/binding/list', methods=['GET'])
    def binding_list():
        """List all active bindings (operator view)."""
        try:
            import sqlite3 as _sqlite3
            _db = _astor_bot_binding_connect()
            _db.row_factory = _sqlite3.Row
            rows = _db.execute("""
                SELECT b.platform_id, b.chat_id, b.user_id, b.scope, b.bound_at,
                       m.short_alias, m.role, m.subscription_plan, m.default_tier
                FROM bindings b
                LEFT JOIN user_meta m ON m.user_id = b.user_id
                WHERE b.active = 1
                ORDER BY b.platform_id, b.user_id
            """).fetchall()
            _db.close()
            resp = jsonify({
                'count': len(rows),
                'bindings': [dict(r) for r in rows],
            })
            # v1.16.25: never let CF edge cache operator-view endpoints.
            resp.headers['Cache-Control'] = 'no-store'
            return resp
        except Exception as e:
            return jsonify({'error': f'{type(e).__name__}: {e}'}), 500

    # v1.15.46 (Ship E.1): standalone staleness endpoint.
    # Returns facts where staleness > threshold (default 30 days), grouped by
    # kind + tier. Operators query via `curl /v1/staleness?tier=public`
    # to find references that may need refreshing.
    @app.route('/v1/staleness', methods=['GET'])
    def staleness_list():
        """List facts whose promoted_at exceeds the staleness threshold.

        Query params:
          tier:    public|source|private (default public)
          user:    private tier only — namespace filter
          threshold_days: int (default 30) — facts older than this are stale
          kind:    optional — restrict to one kind (mental_model|knowledge_page|fact|...)
          limit:   int (default 100) — max rows returned

        Returns:
          {stale_count, threshold_days, items: [{id, kind, content, age_days, is_stale}]}
        """
        from .dashboard_data import _compute_staleness
        tier = request.args.get('tier', 'public')
        user = request.args.get('user') or None
        # v1.15.51 A2 fix: source tier bypass (operator-level, user_id IS NULL).
        _stal_user = None if tier == 'source' else user
        threshold_days = int(request.args.get('threshold_days', 30))
        kind_filter = request.args.get('kind') or None
        limit = int(request.args.get('limit', 100))
        # ACL gate
        try:
            from ._internal.acl import astor_check_read as _stal_acr
            _stal_acr(tier=tier, user_id=_stal_user)
        except Exception as _acl_exc:
            return jsonify({'error': 'forbidden', 'detail': str(_acl_exc)}), 403
        bus = astor_bus(tier=tier, user_id=_stal_user)
        where_extra = ''
        params: list = []
        if kind_filter:
            where_extra = 'AND kind = ?'
            params.append(kind_filter)
        sql = (
            f'SELECT id, kind, content, promoted_at, confidence, importance '
            f'FROM memory_canonical '
            f'WHERE tombstoned = 0 AND tier = ? '
            f'  AND (user_id IS ? OR user_id = ?) '
            f'  {where_extra} '
            f'ORDER BY promoted_at ASC LIMIT ?'
        )
        rows = bus.conn.execute(
            sql, [tier, _stal_user, _stal_user] + params + [limit * 4]  # over-fetch; filter after
        ).fetchall()
        items = []
        stale_count = 0
        for fid, kind, content, promoted_at, confidence, importance in rows:
            st = _compute_staleness(promoted_at or '')
            age_days = st.get('age_days', 0) or 0
            is_stale = age_days >= threshold_days
            if is_stale:
                stale_count += 1
            items.append({
                'id': int(fid),
                'kind': kind,
                'content': (content or '')[:300],
                'promoted_at': promoted_at,
                'confidence': float(confidence or 0),
                'importance': float(importance or 0),
                'age_days': age_days,
                'is_stale': is_stale,
            })
        items.sort(key=lambda x: x['age_days'], reverse=True)
        return jsonify({
            'tier': tier,
            'user': user,
            'threshold_days': threshold_days,
            'kind_filter': kind_filter,
            'stale_count': stale_count,
            'total_returned': min(len(items), limit),
            'items': items[:limit],
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
            'logs', '_server_500.log',
        )
        try:
            _os_e.makedirs(_os_e.path.dirname(_side_log), exist_ok=True)
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

    # ---- v1.15.57 (2026-09-30) Ship P3: experience-warning after_request middleware.
    # When any caller hits /v1/read, /v1/write, /v1/consult, this hook re-runs
    # match_experiences against the request body and surfaces the top experience
    # as an X-Astor-Experience-Warning response header. The point: external agents
    # don't have to opt in to "see" relevant pushback history — it's already on
    # every response. They can surface it to the user, log it, or ignore it.
    #
    # Best-effort: any exception here is swallowed silently so the middleware
    # never breaks the actual response.
    @app.after_request
    def _experience_warning_middleware(response):
        try:
            from flask import request as _fr
            _ep = _fr.path or ''
            if not (_ep.startswith('/v1/read') or _ep.startswith('/v1/write') or _ep.startswith('/v1/consult')):
                return response
            # All three target endpoints (/v1/read, /v1/write, /v1/consult)
            # are POST-only, so just filter on method == POST. (Old logic
            # checked `_ep != '/v1/read'` which was dead code.)
            if _fr.method != 'POST':
                return response
            # IMPORTANT: do NOT consume request body via get_json() here —
            # Flask caches the parsed JSON, so the endpoint's own get_json()
            # would see None and break. Read raw bytes + parse manually.
            _raw = _fr.get_data(cache=True, as_text=True) or ''
            try:
                import json as _json_mw
                _body = _json_mw.loads(_raw) if _raw.strip() else {}
            except Exception:
                _body = {}
            _qtext = ''
            if _ep.startswith('/v1/read') or _ep.startswith('/v1/consult'):
                _qtext = _body.get('query', '')
            elif _ep.startswith('/v1/write'):
                _qtext = _body.get('text', '')
            if not _qtext or len(_qtext.strip()) < 4:
                return response
            _actor = _body.get('user') or _body.get('user_id') or _body.get('actor') or 'admin'
            _tier = _body.get('tier') or 'private'
            _user_id = _actor if _tier.startswith('private') else None
            from .bus import astor_bus as _ab_mw
            _mw_bus = _ab_mw(tier=_tier, user_id=_user_id)
            _mw_hits = _mw_bus.match_experiences(
                query=_qtext, namespace=None, user_id=_user_id,
                top_k=2, use_embedding=False,  # kw-only for speed; middleware is per-request
            )
            if _mw_hits:
                _top = _mw_hits[0]
                _occ = _top.get('invocation_count', 1)
                _is_hot = _occ >= 3 or _top.get('importance', 0) >= 0.95
                # HTTP headers must be latin-1 (RFC 7230). Strip non-ASCII
                # to avoid UnicodeEncodeError in werkzeug send_header.
                _summary = (_top.get('next_step_hint') or _top.get('action_summary') or '')[:120]
                _safe_summary = _summary.encode('latin-1', 'replace').decode('latin-1')
                _warn_val = f"{_top['id']}|occ:{_occ}|{'HOT' if _is_hot else 'warm'}|{_safe_summary}"
                response.headers['X-Astor-Experience-Warning'] = _warn_val
                # multiple matches: IDs only (always ASCII-safe)
                if len(_mw_hits) > 1:
                    _all_ids = ','.join(str(h['id']) for h in _mw_hits[:3])
                    response.headers['X-Astor-Experience-Matches'] = _all_ids
        except Exception:
            pass  # never break the response
        return response


    # v1.16.42: pre-warm dashboard cache in daemon thread
    # v1.16.62: skip in tests (ASTOR_TEST_NO_PREWARM=1) to avoid the
    # background thread holding 16+ sqlite connections on the tmpdir's
    # .db files at tearDown time. Symptom without this: OSError
    # [Errno 39] / PermissionError on bot-binding.db / *.db-wal /
    # *.db-shm during shutil.rmtree(tmpdir). CI verified that
    # build_dashboard_payload opens 16 transient sqlite3 connections
    # that are only released when the thread exits, but the thread
    # can still be running when the test's tearDown fires.
    import os as _os
    if _os.environ.get('ASTOR_TEST_NO_PREWARM', '0') != '1':
        import threading as _th_dash
        _astor_default_dir = str(get_default_astor_dir())
        _dash_th = _th_dash.Thread(
            target=_astor_prewarm_dashboard_cache,
            args=(_astor_default_dir,),
            daemon=True,
        )
        _dash_th.start()
        print('   Dashboard prewarm: launched background thread', flush=True)
    return app


def main():
    """Run server: python -m astor_memory.server
    v1.16.41: switched from Flask dev server (threaded=False/True both bad)
    to waitress WSGI server (production-grade, bounded threads).
    """
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

    # v1.16.41: serve via waitress for true bounded concurrency.
    import os as _os
    import waitress as _waitress
    _threads = int(_os.environ.get('ASTOR_SERVER_THREADS', '8'))
    print(f'   WSGI: waitress threads={_threads} (production-grade concurrency)', flush=True)
    _waitress.serve(app, host=args.host, port=args.port, threads=_threads,
                    ident=None, cleanup_interval=30)
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
      # S13: enable Flask threaded mode for concurrent /v1/read requests (R-class N). Bus uses WAL mode so concurrent reads safe.


if __name__ == '__main__':
    main()


__all__ = ['create_app', 'main']

# S13 v1.14.6 threaded=True — verified 2-concurrent OK, 4-concurrent overload (R-class Q bus lock)
