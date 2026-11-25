"""memory_defense.py — Hindsight-style PII scanner (Ship P0, 2026-09-28).

Inspired by Vectorize.io Hindsight's "Memory Defense" feature: write-time
PII pattern detection. Default OFF (operator opt-in via env gate).

Two policies:
- "redact"  (default): replace PII match with [REDACTED:TYPE] tag,
  preserve fact length for embedding stability.
- "block"   (strict): return error, fact not written.

45-pattern registry covers the most common categories operator flagged:
- API keys (OpenAI, Anthropic, OpenRouter, Slack, Discord, Telegram, GitHub)
- Cloud credentials (AWS, GCP, Azure)
- Crypto (BTC/ETH addresses, private keys)
- PII (email, phone, SSN, credit card)
- WeChat / Telegram chat IDs (operator's specific concern)
- Discord tokens
- URLs with secrets in query strings

Gated by env:
  ASTOR_PII_SCAN_ON_WRITE=1   → enable scanning (default off — cost + false-positive risk)
  ASTOR_PII_POLICY=redact|block  → default redact

Returns:
  scan_facts(content) → list of PIIMatch(name, start, end, snippet)
  redact_facts(content, matches) → content with redactions applied
  audit_log(name, fid, matches)  → write to bus.audit_log

References:
  Hindsight: https://github.com/vectorize-io/hindsight (Memory Defense)
  R-class 12747 (astor fact ≠ truth): PII tables need explicit gating
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Iterable

# v1.17.84: one source of truth for "this text names a conversation".
#
# The write gate (memory_defense) and the tier classifier
# (visibility_classifier) both have to answer the same question -- does
# this text contain a chat id? -- and they used to answer it with two
# separate regex sets. When they drifted apart, the gate missed the
# shapes the operator actually writes and a chat id walked into the
# public tier. The classifier's patterns are the ones that were
# hardened against real text (role words, platform abbreviations, the
# bare WeChat id, and the boundary that keeps a Hermes session id
# out), so the gate borrows them instead of restating them.
from .visibility_classifier import (  # noqa: E402
    _RE_ABBREV_CHAT_ID as _VIS_ABBREV_CHAT_ID,
    _RE_NAMED_CHAT_ID as _VIS_NAMED_CHAT_ID,
    _RE_WECHAT_ID_BARE as _VIS_WECHAT_BARE,
)

# The pattern sources, as strings, so they can be composed into the
# gate's own combined regex (which also carries the hyphen form).
_VIS_CHAT_ID_SOURCE = (
    _VIS_NAMED_CHAT_ID.pattern + "|" + _VIS_ABBREV_CHAT_ID.pattern
)
_VIS_WECHAT_BARE_SOURCE = _VIS_WECHAT_BARE.pattern

# ---------------------------------------------------------------------------
# Pattern registry (45 categories — Hindsight parity + WeChat-specific)
# ---------------------------------------------------------------------------
# Each tuple: (name, regex, severity). Severity in {redact, block}.
# Group 0 captures the full match; group 1 captures the secret portion
# (for hashing in audit log without storing the raw secret).
_PATTERNS: list[tuple[str, re.Pattern, str]] = [
    # API keys (block — should never be written to memory)
    ("openai_api_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "block"),
    ("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9-]{20,}\b"), "block"),
    ("openrouter_api_key", re.compile(r"\bsk-or-v1-[A-Za-z0-9]{20,}\b"), "block"),
    ("slack_token", re.compile(r"\bxox[abp]-[A-Za-z0-9-]{10,}\b"), "block"),
    ("discord_bot_token", re.compile(r"\b[MN][A-Za-z\d]{23,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{27,}\b"), "block"),
    ("github_pat", re.compile(r"\bghp_[A-Za-z0-9]{36,}\b"), "block"),
    ("github_oauth", re.compile(r"\bgho_[A-Za-z0-9]{36,}\b"), "block"),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "block"),
    ("aws_secret_key", re.compile(r"\b[A-Za-z0-9/+=]{40}\b"), "block"),  # broad; false positives acceptable
    ("gcp_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "block"),
    ("telegram_bot_token", re.compile(r"\b[0-9]{8,10}:[A-Za-z0-9_-]{35}\b"), "block"),

    # Crypto (block)
    ("btc_address", re.compile(r"\b[13][a-km-zA-HJ-NP-Z1-9]{25,34}\b"), "block"),
    ("eth_address", re.compile(r"\b0x[a-fA-F0-9]{40}\b"), "block"),
    ("eth_private_key", re.compile(r"\b0x[a-fA-F0-9]{64}\b"), "block"),

    # PII (block by default — operator can override to redact)
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "block"),
    ("phone_us", re.compile(r"\b\(?\d{3}\)?[-.\s]+\d{3}[-.\s]+\d{4}\b"), "block"),
    ("phone_intl", re.compile(r"\+\d{1,3}[\s-]\d{3,}[\s-]\d{3,}[\s-]\d{3,}\b"), "block"),
    ("ssn_us", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "block"),
    ("credit_card", re.compile(r"\b(?:\d[ -]*?){13,19}\b"), "block"),
    # v1.17.92: the old pattern matched ANY 4 dot-separated numbers, so a
    # browser UA carried a false positive -- "Chrome/120.0.0.0" was read
    # as an IPv4 address, and a public-tier write containing a UA was
    # refused with HTTP 400. Exclude the version-number SHAPE (first
    # octet >= 100 with three zero octets) rather than excluding by
    # context: a context rule like "not preceded by /" would also drop a
    # real IP inside a URL (http://192.168.1.1:8080/), which is the
    # direction that must not fail. Octets are also bounded to 0-255 now.
    ("ipv4", re.compile(
        r"(?<![\d.])"
        r"(?!(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.0\.0\.0\b)"
        r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
        r"(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}"
        r"(?!\d)"), "block"),

    # WeChat chat IDs — operator's specific concern (R-class 1152, 8983)
    ("wechat_chat_id", re.compile(r"\bo[A-Za-z0-9_-]{20,}@im\.wechat\b"), "block"),
    # v1.17.15: the hyphenated form only. The common form is a bare
    # id next to the platform name -- "telegram 555000111", "chat id
    # 555000111" -- and the gate did not see those, so a public-tier
    # write could carry a chat id past Memory Defense. The platform
    # name has to be adjacent: a bare 6-12 digit number is far too
    # common to treat as an identifier on its own, which is why the
    # hyphen anchor was there.
    #
    # v1.17.84: the gate and the visibility classifier were two
    # separate implementations of the same rule, and they disagreed.
    # The gate knew only "platform name, maybe chat_id/user_id, then
    # digits" -- it did not know the ROLE word, which is what the
    # operator actually writes: "telegram DM 448...", "DC admin DM
    # 通道(1514...)". So the write gate saw a chat id as ordinary
    # prose and let it into the public tier.
    #
    # Now there is ONE source of truth. The classifier already knows
    # the role words, the platform abbreviations, the bare WeChat id
    # and the boundary that keeps a Hermes session id out. The gate
    # imports those patterns instead of restating them, so the two
    # scanners cannot drift apart again -- which is exactly how this
    # hole opened the first time.
    #
    # The hyphenated form stays here: it is not a classifier rule.
    ("telegram_chat_id", re.compile(
        r"(?<!\w)-\d{6,12}(?!\d)|" + _VIS_CHAT_ID_SOURCE, re.I), "redact"),
    # v1.17.84: the bare WeChat id, without the "@im.wechat" suffix.
    # "read the WEIXIN_ADMIN_ID (admin's <wechat_id>-...)" -- the full
    # form was known, the bare one was not, so the gate let it past.
    ("wechat_chat_id", re.compile(_VIS_WECHAT_BARE_SOURCE, re.I), "redact"),

    # Discord IDs (snowflakes)
    ("discord_snowflake", re.compile(r"\b\d{17,20}\b"), "redact"),

    # Bearer / Basic auth headers
    ("bearer_token", re.compile(r"\bBearer\s+[A-Za-z0-9_-]{20,}\b"), "block"),
    ("basic_auth", re.compile(r"\bBasic\s+[A-Za-z0-9+/=]{10,}\b"), "block"),

    # URL with secret query params — 密钥参数可以落在任何一个 query 槽位,
    # 旧写法只认 '?' 后面的第一个槽位,'?a=1&api_key=SECRET' 整条被判成干净文本。
    # 用 lookbehind 吃掉分隔符,redact 之后 URL 结构还留着。
    ("url_secret_param", re.compile(
        r"(?<=[?&])[^&;\s=]*(?:key|token|secret|password|api_key|access_token)=[A-Za-z0-9]+",
        re.IGNORECASE), "block"),

    # Private key markers — 整块匹配(头 + base64 正文 + 尾)。
    # 只匹配头一行的话,redact 之后 base64 正文原样写进公开层,私钥等于没拦。
    # 没有 END 尾行(粘贴被截断)时退化成吃掉后面的 base64 行。
    ("private_key_pem", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
        r"(?:[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
        r"|(?:[ \t]*\r?\n[ \t]*[A-Za-z0-9+/=]+)*)"), "block"),
    ("ssh_key", re.compile(r"ssh-(?:rsa|dss|ed25519|ecdsa)\s+[A-Za-z0-9+/=]{40,}"), "block"),

    # JWT tokens (3 base64 chunks separated by dots)
    ("jwt_token", re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "block"),

    # Database connection strings
    ("db_connection_string", re.compile(r"\b(?:postgres|postgresql|mysql|mongodb)://[^\s]+:[^\s]+@[^\s]+\b"), "block"),
    ("stripe_key", re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{24,}\b"), "block"),
    ("twilio_key", re.compile(r"\bSK[a-fA-F0-9]{32}\b"), "block"),
    ("sendgrid_key", re.compile(r"\bSG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}\b"), "block"),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b"), "block"),
    ("gitlab_pat", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), "block"),
    ("monero_address", re.compile(r"\b4[0-9AB][1-9A-HJ-NP-Za-km-z]{93}\b"), "block"),
    ("ipv6", re.compile(r"\b(?:[A-Fa-f0-9]{1,4}:){7}[A-Fa-f0-9]{1,4}\b"), "block"),
    ("mac_address", re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b"), "block"),
    ("windows_path", re.compile(r"\b[A-Z]\:\\\\(?:Users|Documents|Program Files|Windows)[^\s]*\b"), "block"),
    ("unix_home_path", re.compile(r"\b/home/[a-z]+/(?:\\.ssh|aws|salesforce)\b"), "block"),
    ("high_entropy_token", re.compile(r"\b[A-Za-z0-9+/=]{50,}\b"), "redact"),
    ("cert_pem", re.compile(r"-----BEGIN [A-Z ]*CERTIFICATE-----"), "block"),
    ("url_with_password", re.compile(r"://[^:\s]+:[^@\s]+@[^\s]+"), "block"),
    ("url_query_secret", re.compile(r"\b[a-z_]+=sk_[A-Za-z0-9]{20,}\b"), "block"),
]

# Total pattern count (must match `len(_PATTERNS)` below; updated at import).
PATTERN_COUNT = len(_PATTERNS)
assert PATTERN_COUNT >= 40, f"Memory Defense parity: expected ≥40 patterns, got {PATTERN_COUNT}"


@dataclass(frozen=True)
class PIIMatch:
    """One PII hit inside content."""
    name: str       # pattern name, e.g. 'openai_api_key'
    start: int      # byte offset (0-indexed, inclusive)
    end: int        # byte offset (exclusive)
    snippet: str    # redacted snippet for audit log (first/last 4 chars + hash)
    severity: str   # 'redact' or 'block'

    def fingerprint(self) -> str:
        """Stable hash of the matched text — audit logs identify repeats
        without storing the secret itself."""
        return hashlib.sha256(self.snippet.encode("utf-8", errors="replace")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Scan / redact / audit
# ---------------------------------------------------------------------------
def _luhn_ok(digits: str) -> bool:
    """Luhn checksum on a 13-19 digit run.

    The `credit_card` rule matches any run of 13-19 digits, but a
    WeChat message id, an order number and a millisecond timestamp all
    have that exact shape -- so a public-tier write carrying one was
    refused with HTTP 400 (`pii_blocked`). A real card number passes
    Luhn; a random id of that length passes about one time in ten.
    """
    nums = [int(c) for c in digits if c.isdigit()]
    if len(nums) < 13:
        return False
    total = 0
    for i, d in enumerate(reversed(nums)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# A rule whose regex matches a SHAPE rather than a SEMANTIC gets a
# validator here: a hit that fails the validator is not a hit. The
# pattern behind them is the same one that made "Chrome/120.0.0.0" read
# as an IPv4 address.
_MATCH_VALIDATORS = {
    'credit_card': _luhn_ok,
}


def scan_facts(content: str) -> list[PIIMatch]:
    """Return all PII hits in `content`. Empty list if clean."""
    if not content:
        return []
    out: list[PIIMatch] = []
    for name, rx, sev in _PATTERNS:
        _validator = _MATCH_VALIDATORS.get(name)
        for m in rx.finditer(content):
            if _validator is not None and not _validator(m.group(0)):
                continue
            snippet = m.group(0)
            out.append(PIIMatch(
                name=name,
                start=m.start(),
                end=m.end(),
                snippet=_redact_snippet(snippet),
                severity=sev,
            ))
    # Sort by start, dedup overlaps (keep highest-severity — block > redact)
    out.sort(key=lambda x: (x.start, -ord(x.severity[0])))
    return out


def _redact_snippet(s: str) -> str:
    """Return a redacted form of `s` for audit logs. First 4 + last 2 chars only."""
    if len(s) <= 8:
        return f"{s[:2]}***{s[-2:]}"
    return f"{s[:4]}***{s[-2:]}"


def redact_facts(content: str, matches: Iterable[PIIMatch]) -> str:
    """Apply redactions in-place. `matches` must be from scan_facts(content)
    (positional correctness guaranteed). Block-severity matches are
    replaced with `[BLOCKED:NAME]` tag (not reversible, by design).
    Redact-severity matches are replaced with `[REDACTED:NAME]` tag.

    When two matches overlap (same span), the LESS destructive tag wins
    (redact > block). This avoids over-redacting text that the operator
    may want to keep (e.g. a 10-digit chat ID is redact, but matches
    phone_us block — we prefer the redact tag so the write succeeds
    under block policy with a [REDACTED] marker).
    """
    if not content:
        return content
    # Dedupe overlaps: for each start position, keep the redact-severity match
    # (which is less destructive). Sort by start desc for in-place mutation.
    by_start: dict[int, PIIMatch] = {}
    for m in matches:
        existing = by_start.get(m.start)
        if existing is None or m.severity == 'redact':
            by_start[m.start] = m
    # One left-to-right pass. Replacing one span changes the length of
    # the string, so every later replacement used a stale offset -- with a
    # whole-block match (a PEM key) that shifted the end past the block
    # and ate the text after it. A match that starts inside a span already
    # replaced is skipped instead.
    chosen = sorted(by_start.values(), key=lambda x: x.start)
    out: list[str] = []
    pos = 0
    for m in chosen:
        if m.start < pos:
            continue
        tag = f"[BLOCKED:{m.name}]" if m.severity == "block" else f"[REDACTED:{m.name}]"
        out.append(content[pos:m.start])
        out.append(tag)
        pos = m.end
    out.append(content[pos:])
    return "".join(out)


def has_block_severity(matches: Iterable[PIIMatch]) -> bool:
    """True if any match has block severity (would be rejected in 'block' policy)."""
    return any(m.severity == "block" for m in matches)


def scan_policy(content: str, policy: str = "redact") -> tuple[str, list[PIIMatch]]:
    """Apply the configured policy. Returns (processed_content, matches).

    policy='redact'  → all matches replaced with [REDACTED:NAME] or [BLOCKED:NAME]
    policy='block'   → if any block-severity match, return (original, matches)
                        and caller should reject; otherwise redact
    """
    matches = scan_facts(content)
    if not matches:
        return content, []
    if policy == "block" and has_block_severity(matches):
        return content, matches  # caller rejects based on matches
    return redact_facts(content, matches), matches


def is_enabled() -> bool:
    """ASTOR_PII_SCAN_ON_WRITE=1 enables scanning."""
    return os.environ.get("ASTOR_PII_SCAN_ON_WRITE", "0") == "1"


def get_policy() -> str:
    """ASTOR_PII_POLICY=redact|block (default redact)."""
    return os.environ.get("ASTOR_PII_POLICY", "redact").lower()


def audit_log_record(fact_id: int | None, user: str, tier: str,
                     matches: list[PIIMatch], policy_applied: str) -> dict:
    """Build an audit-log entry dict AND write to bus.audit_log.

    v1.15.45 (Ship P0.1b): PII audit entries now persist to bus.audit_log
    instead of stderr-only. Operators can query PII scan history via
    /v1/audit or direct SQL: `SELECT * FROM audit_log WHERE event =
    'memory_defense_scan' ORDER BY ts DESC LIMIT 50`. Severity is
    'critical' for block policy (write rejected), 'warning' for redact.

    Never includes raw secrets — only `fingerprint()` (sha256[:12]).
    """
    severity = 'critical' if policy_applied == 'block' else 'warning'
    entry = {
        "kind": "memory_defense_scan",
        "fact_id": fact_id,
        "user": user,
        "tier": tier,
        "policy": policy_applied,
        "match_count": len(matches),
        "matches": [
            {"name": m.name, "severity": m.severity, "fingerprint": m.fingerprint()}
            for m in matches
        ],
        "severity": severity,
    }
    # v1.15.45 (Ship P0.1b): zero-match scans don't pollute audit log.
    # Only write audit row when at least one PII match was detected.
    if not matches:
        return entry
    # Write to bus.audit_log (fire-and-forget). If bus not yet initialized
    # (early startup), fall back to stderr — never break the write path.
    try:
        from ..bus.store import astor_bus
        from .._internal.acl import astor_check_write
        # astor_check_write takes (tier, user_id) — no actor arg. ACL is
        # thread-local already initialized by the request handler.
        astor_check_write(
            tier=tier if tier in ('public', 'source') else 'public',
            user_id=user if tier.startswith('private') else None,
        )
        bus = astor_bus(tier=tier if tier in ('public', 'source') else 'public',
                        user_id=user if tier.startswith('private') else None)
        import json as _json
        # The actor is the identity bound to this request, not the user id
        # the request claims -- otherwise a caller could write the audit
        # row under someone else's name.
        try:
            from .._internal.acl import astor_current_acl
            actor_str = astor_current_acl().actor
        except Exception:
            actor_str = 'admin:admin'
        bus.write_audit(
            event='memory_defense_scan',
            actor=actor_str,
            target_type='memory_defense',
            target_id=str(fact_id) if fact_id else 'pre_write',
            new_state=_json.dumps(entry),
            severity=severity,
        )
    except Exception as _e:
        import sys as _sys
        _sys.stderr.write(f'[memory_defense.audit_log_record] bus write failed (non-fatal): {_e!r}\n')
    return entry


# ---------------------------------------------------------------------------
# v1.16.x: Personal content sniff (Layer 2 defense)
# ---------------------------------------------------------------------------
# Detects non-PII personal content that the 45-pattern PII scanner misses:
# names + events, locations, relationships, financial mentions. Severity is
# 'warn' — never blocks the write, only tags it via response header
# `X-Astor-Personal-Content` and persists category list into fact metadata.
# Layer 1 (PII gate) is still the authoritative block — Layer 2 helps the
# agent framework surface "are you sure this should be public?" prompts.
_PERSONAL_CONTENT_PATTERNS: dict[str, list] = {
    # 中文 2-4 字姓名 + 后续事件动词 (张三去了 / 张三家发生钱王) — 用反向匹配
    # 后续是"发生/说/做/去/来/在/到/跟/和"等动词时，前面 2-4 中文字符视为人名
    # v1.17.91: this is the Layer 2 sniff, not the authoritative gate
    # (Layer 1 blocks the real PII), so a false positive only costs a
    # shared-tier write its shared tier. The old shape matched ANY 2-4
    # Chinese chars followed by a verb, so ordinary technical prose read
    # as a person's name: "丢失发生在", "尚未定位到", "截断到" all
    # matched and pushed the fact into the private tier. Require a
    # common surname in front -- that is what a Chinese name looks like.
    'name_chinese': [
        re.compile(
            r'[张李王刘陈杨黄赵周吴徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘'
            r'于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹'
            r'薛段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤]'
            r'[一-鿿]{1,2}(?:家|说|做|去|来|在|到|跟|和|把|给|叫|想|觉得|发生|去世|结婚)'),
    ],
    # "我在 X" / "我去 X" / "她在 X" + 地点（中文 2-5 字地名，常见城市名 + 后缀可选）
    'location': [
        re.compile(r'[我她在](?:在|去|到|从)([一-鿿]{2,5})(?:市|省|县|区|路|街|公司|学校|医院|餐厅|机场|车站|酒店|家|工作)'),
        re.compile(r'[我她](?:住在|在)([一-鿿]{2,5})'),
        re.compile(r'(?:来自|出生于)([一-鿿]{2,5})'),
    ],
    # 私人关系 (女朋友/男朋友/老师/老板/父母/家人)
    'relationship': [
        re.compile(r'(?:我)?(?:女朋友|男朋友|老公|老婆|老师|老板|父母|父亲|母亲|爸爸|妈妈|爷爷|奶奶|同事|朋友|闺蜜)(?:[说做去叫]?)'),
    ],
    # 财务相关 (账户余额 / 工资收入 / 信用卡 / 贷款)
    'financial': [
        re.compile(r'(?:账户|余额|存款|工资|收入|资产|投资|月供|信用卡|贷款)(?:[余额总额数目]?)'),
    ],
}
_PERSONAL_CONTENT_COMPILED: dict[str, list] = {
    name: [rx for rx in rxs]
    for name, rxs in _PERSONAL_CONTENT_PATTERNS.items()
}


def detect_personal_content(content: str) -> list[str]:
    """v1.16.x (Plan "reactive consult + 三层内容防线"): Layer 2 sniff.

    Detects non-PII personal content categories that the 45-pattern PII
    scanner misses. Returns list of category names (e.g. ['name_chinese',
    'financial']); empty list if clean. Severity is always 'warn' — never
    blocks the write. Caller (server.py /v1/write) sets the X-Astor-Personal-
    Content response header and persists categories into fact metadata.

    Designed to be conservative: false positives should not block public
    methods/patterns from being shared. Agent frameworks / dashboards use
    the warning to prompt "are you sure this should be public?".
    """
    if not content:
        return []
    hits = []
    for name, rxs in _PERSONAL_CONTENT_COMPILED.items():
        if any(rx.search(content) for rx in rxs):
            hits.append(name)
    return hits