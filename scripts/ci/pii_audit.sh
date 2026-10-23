#!/usr/bin/env bash
# v1.16.26 PII audit (CI-level) — safe-to-ship version.
set -e
cd "$(git rev-parse --show-toplevel)"

# Default patterns — generic, no real IDs.
# The patterns below are GENERIC placeholders on purpose: they describe the
# shape of a leak (a placeholder that was never replaced) rather than any
# one operator's real identifier. Real identifiers cannot be listed here --
# putting one in would leak the very thing this script exists to catch.
#
# The shapes that must never appear in a tracked file:
#   <AUTHOR>          a personal name used as a handle
#   <repo-owner>      a GitHub account name
#   <USER>            a user handle
#   <AUTHOR>.com      a personal domain
#   C:\Users\<USER>  a Windows home path
#   <home-dir>        a home directory
#   /c/Users/<USER>   a POSIX-style home path
#   wechat openids    an "@im.wechat" suffix on a real openid
#   muse_chat_admin_demo / test_tunnel_admin: demo account ids
PATTERNS=(
  '\b<AUTHOR>\b'
  '\b<repo-owner>\b'
  '\b<USER>\b'
  '\b<AUTHOR>\.com\b'
  'C:\\Users\\<USER>'
  '<home-dir>'
  '/c/Users/<USER>'
  '[A-Za-z0-9]{8,}-[A-Za-z0-9]{15,}@im\.wechat'
  '[0-9a-f]{8,16}@im\.bot'
  'muse_chat_admin_demo'
  'test_tunnel_admin'
)

# Exclude self (the audit script) and installer — they contain patterns as data.
EXCLUDE_PATTERN='(scripts/ci/(pii_audit|install_pii_guard)\.sh$)'

TRACKED=$(git ls-files | grep -E '\.(py|md|bat|sh|txt|yaml|yml|toml|json|html|js|css|sql|cfg|ini)$' | grep -Ev "$EXCLUDE_PATTERN" || true)
hits=0
for pat in "${PATTERNS[@]}"; do
  matches=$(printf '%s\n' "$TRACKED" | xargs -I {} grep -PIn --label='{}' "$pat" {} 2>/dev/null || true)
  if [ -n "$matches" ]; then
    echo "PII LEAK: pattern '$pat':" >&2
    echo "$matches" | head -30 >&2
    echo "" >&2
    hits=$((hits+1))
  fi
done

if [ $hits -gt 0 ]; then
  echo "" >&2
  echo "FAIL: $hits PII pattern(s) found in TRACKED files." >&2
  echo "Use placeholders: <admin-telegram-chat-id>, <admin-muse-chat-id>, <home>, <user>, <repo-owner>, astor.example.com" >&2
  exit 1
fi
echo "PASS: no PII in tracked files"
exit 0
