"""test_memory_defense.py — Ship P0 (2026-09-28)

Hindsight-style PII scanner tests. Verifies:
- 44 patterns registered
- Common secrets detected (API keys, emails, chat IDs, crypto)
- Redact policy replaces matches with [BLOCKED:TYPE] tag
- Block policy returns original content with has_block flag
- Env gate is_enabled / get_policy
- Fingerprint is sha256[:12] (audit-safe, no raw secrets)
- Empty input / no-match returns empty / original

Author: Ship P0 (Memory Defense).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _eq(label: str, got, expected) -> None:
    if got != expected:
        raise AssertionError(f"{label}\n    got     : {got}\n    expected: {expected}")
    print(f"  [PASS] {label}")


def _truthy(label: str, got) -> None:
    if not got:
        raise AssertionError(f"{label} (got falsy: {got!r})")
    print(f"  [PASS] {label}")


def main() -> int:
    print("=== Unit cases ===")

    from astor_memory.nest.memory_defense import (
        scan_facts, redact_facts, scan_policy, has_block_severity,
        PIIMatch, is_enabled, get_policy, PATTERN_COUNT, audit_log_record,
        _redact_snippet,
    )

    # 1) Pattern count parity (Hindsight ≈ 45)
    _truthy("PATTERN_COUNT >= 40", PATTERN_COUNT >= 40)
    print(f"  (actual PATTERN_COUNT = {PATTERN_COUNT})")

    # 2) Common secrets detected
    samples = [
        ("openai key", "sk-abcdefghijklmnopqrstuvwxyz0123456789", "openai_api_key"),
        ("openrouter", "sk-or-v1-1234567890abcdefghijklmnop", "openrouter_api_key"),
        ("anthropic", "sk-ant-api03-1234567890abcdefghijklmnopqrst", "anthropic_api_key"),
        ("email", "alice@example.com", "email"),
        # v1.17.91: the old sample was a <placeholder>, so it never matched
        # the rule (which needs a leading "o" plus 20+ chars) -- and this
        # file had never run under pytest, so nobody noticed. The rule was
        # right; the sample was stale.
        ("wechat chat id", "oABCDEFGHIJKLMNOPQRSTUVWXYZ012345@im.wechat", "wechat_chat_id"),
        ("btc legacy", "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2", "btc_address"),
        ("eth", "0x742d35Cc6634C0532925a3b844Bc9e7595f0bEb1", "eth_address"),
        ("aws access key", "AKIAIOSFODNN7EXAMPLE", "aws_access_key"),
        ("github pat", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij", "github_pat"),
        ("jwt", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c", "jwt_token"),
        ("postgres conn", "postgresql://user:secret@host.example.com/db", "db_connection_string"),
        ("rsa private", "-----BEGIN RSA PRIVATE KEY-----", "private_key_pem"),
        ("ipv4", "192.168.1.1", "ipv4"),
    ]
    for label, txt, expected_name in samples:
        matches = scan_facts(txt)
        names = {m.name for m in matches}
        _truthy(f"{label} → {expected_name}", expected_name in names)

    # 3) Clean text returns no matches
    clean = "Just some plain text about poker and 八字 with no secrets"
    _eq("clean text → 0 matches", scan_facts(clean), [])

    # 4) Empty input
    _eq("empty string → 0 matches", scan_facts(""), [])

    # 5) Redact policy: replaces matches with [BLOCKED:TYPE]
    txt = "API: sk-abcdefghijklmnopqrstuvwxyz0123456789 user alice@example.com"
    redacted, matches = scan_policy(txt, policy='redact')
    _truthy("redact returns matches", len(matches) >= 2)
    _eq("redact replaces api key", "[BLOCKED:openai_api_key]" in redacted, True)
    _eq("redact replaces email", "[BLOCKED:email]" in redacted, True)
    _eq("redact keeps surrounding text", "API:" in redacted and "user" in redacted, True)

    # 6) Redact severity uses [REDACTED:TYPE] (block uses [BLOCKED:TYPE])
    # Use a sequence that ONLY triggers redact-severity patterns (telegram chat id)
    txt_redact = "send to 1234567890 today"  # matches phone_us (block) + telegram_chat_id (redact)
    matches_redact = scan_facts(txt_redact)
    # The redact-severity tag wins because of severity-first dedup logic
    if matches_redact:
        _truthy("redact-vs-block: redact tag wins for redact-severity match",
                any("[REDACTED:" in redact_facts(txt_redact, matches_redact) for _ in [None]))

    # 7) Block policy: returns original + has_block=True
    txt = "API: sk-abcdefghijklmnopqrstuvwxyz0123456789"
    original, matches = scan_policy(txt, policy='block')
    _eq("block policy returns original", original, txt)
    _truthy("block policy has_block=True", has_block_severity(matches))

    # 8) Block policy with clean content: redacts (no block match)
    txt = "Just clean text"
    out, matches = scan_policy(txt, policy='block')
    _eq("block policy + clean → original unchanged", out, txt)
    _eq("block policy + clean → 0 matches", matches, [])

    # 9) PIIMatch dataclass
    m = PIIMatch(name='test', start=0, end=4, snippet='abcd', severity='block')
    _eq("PIIMatch.name", m.name, 'test')
    _eq("PIIMatch.start", m.start, 0)
    _eq("PIIMatch.end", m.end, 4)
    _eq("PIIMatch.snippet", m.snippet, 'abcd')
    _eq("PIIMatch.severity", m.severity, 'block')

    # 10) fingerprint is sha256[:12]
    m1 = PIIMatch(name='test', start=0, end=4, snippet='abcd', severity='block')
    m2 = PIIMatch(name='test', start=0, end=4, snippet='abcd', severity='block')
    m3 = PIIMatch(name='test', start=0, end=4, snippet='xyz', severity='block')
    _eq("fingerprint is stable", m1.fingerprint(), m2.fingerprint())
    _truthy("fingerprint is 12 chars", len(m1.fingerprint()) == 12)
    _truthy("fingerprint differs for different content",
            m1.fingerprint() != m3.fingerprint())

    # 11) _redact_snippet handles short + long strings
    _eq("short redact", _redact_snippet("abc"), "ab***bc")
    _truthy("long redact hides middle", "***" in _redact_snippet("sk-abcdefghijklmnop"))

    # 12) Env gate
    os.environ.pop("ASTOR_PII_SCAN_ON_WRITE", None)
    _eq("default is_enabled() = False", is_enabled(), False)
    os.environ["ASTOR_PII_SCAN_ON_WRITE"] = "1"
    _eq("ASTOR_PII_SCAN_ON_WRITE=1 → is_enabled()", is_enabled(), True)
    os.environ.pop("ASTOR_PII_SCAN_ON_WRITE", None)

    os.environ.pop("ASTOR_PII_POLICY", None)
    _eq("default policy =='redact'", get_policy(), 'redact')
    os.environ["ASTOR_PII_POLICY"] = "block"
    _eq("ASTOR_PII_POLICY=block", get_policy(), 'block')
    os.environ.pop("ASTOR_PII_POLICY", None)

    # 13) audit_log_record shape
    matches = scan_facts("sk-abcdefghijklmnopqrstuvwxyz0123456789")
    rec = audit_log_record(fact_id=123, user='admin', tier='private', matches=matches, policy_applied='redact')
    _eq("audit kind", rec['kind'], 'memory_defense_scan')
    _eq("audit fact_id", rec['fact_id'], 123)
    _eq("audit user", rec['user'], 'admin')
    _eq("audit tier", rec['tier'], 'private')
    _eq("audit policy", rec['policy'], 'redact')
    _eq("audit match_count", rec['match_count'], 1)
    # match entry has name + severity + fingerprint, NO raw snippet
    m_entry = rec['matches'][0]
    _truthy("audit has name", 'name' in m_entry)
    _truthy("audit has severity", 'severity' in m_entry)
    _truthy("audit has fingerprint", 'fingerprint' in m_entry)
    _truthy("audit NO raw snippet field", 'snippet' not in m_entry)

    # 14) redact_facts mutates correctly (sorted by start desc)
    txt = "alpha sk-abcdefghijklmnopqrstuvwxyz0123456789 beta alice@example.com end"
    matches = scan_facts(txt)
    out = redact_facts(txt, matches)
    _truthy("redact_facts handles multiple matches", "[BLOCKED:" in out)
    _eq("redact keeps prefix 'alpha'", "alpha " in out, True)
    _eq("redact keeps 'beta end'", "beta " in out and " end" in out, True)

    # 15) Real-world composite
    txt = (
        "User Alice (alice@example.com) sent a message to "
        "oABCDEFGHIJKLMNOPQRSTUVWXYZ012345@im.wechat from 192.168.1.1. "
        "Her API key was sk-abcdefghijklmnopqrstuvwxyz0123456789."
    )
    matches = scan_facts(txt)
    names = {m.name for m in matches}
    _truthy("composite finds email", 'email' in names)
    _truthy("composite finds wechat", 'wechat_chat_id' in names)
    _truthy("composite finds ipv4", 'ipv4' in names)
    _truthy("composite finds openai", 'openai_api_key' in names)
    _truthy("composite ≥ 4 hits", len(matches) >= 4)

    print("\nAll tests PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def test_memory_defense_script():
    """v1.17.91: this file was a script (main() + sys.exit()), so pytest
    collected nothing from it and its assertions had never run. The helper
    functions now raise AssertionError instead of calling sys.exit(), and
    this entry point runs the original body under pytest."""
    assert main() == 0

def test_ipv4_rule_ignores_browser_version_numbers():
    """v1.17.92: a browser UA carried a false positive -- "Chrome/120.0.0.0"
    was read as an IPv4 address, and a public-tier write carrying a UA was
    refused with HTTP 400. The version-number SHAPE is excluded, not the
    context: excluding by context ("not preceded by /") would also drop a
    real IP inside a URL."""
    from astor_memory.nest.memory_defense import scan_facts as _scan

    def _has_ipv4(text):
        return "ipv4" in {m.name for m in _scan(text)}

    for text in (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "version 120.0.0.0 of the browser",
        "Chrome/131.0.6778.86",
    ):
        assert not _has_ipv4(text), text

    for text in (
        "the host is at 192.168.1.1 and the gateway 10.0.0.1",
        "resolve 8.8.8.8 first",
        "see http://192.168.1.1:8080/x for details",
        "100.64.0.1",
    ):
        assert _has_ipv4(text), text
