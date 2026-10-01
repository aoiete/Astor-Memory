#!/usr/bin/env bash
# v1.16.26 PII audit (CI-level) — safe-to-ship version.
set -e
cd "$(git rev-parse --show-toplevel)"

# Default patterns — generic, no real IDs.
PATTERNS=(
  '\bflopworld\b'
  '\baoiete\b'
  '\bTheNuts\b'
  '\bflopworld\.com\b'
  'C:\\Users\\TheNuts'
  'C:/Users/TheNuts'
  '/c/Users/TheNuts'
  'o9cq80[A-Za-z0-9_-]{20,}'
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
