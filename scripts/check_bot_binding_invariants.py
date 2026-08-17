"""Invariant checker for bot-binding.db.

Runs the 6 invariants and writes a single audit row per run.

Designed for cron + on-demand invocation. Exit code 0 = pass, 1 = violations.

Invariants:
  1. Exactly 1 row per platform_kind (TG/DC/feishu/webchat) — weixin allows N
  2. Unique active (platform_id, chat_id) — at most 1 active binding per pair
  3. Every binding.user_id has user_meta row
  4. No binding.user_id is empty
  5. enabled=1 platforms have non-empty token
  6. weixin platforms have base_url set
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

os.environ.setdefault('ASTOR_DIR', str(Path.home() / '.astor'))
sys.path.insert(0, os.environ.get('ASTOR_SOURCE_PATH') or str(Path.cwd()))

from astor_memory._internal.acl import astor_init_acl
from astor_memory._internal.audit_logger import astor_audit

astor_init_acl(actor='first_admin', role='first_admin', tier='public')


def check_all(db_path: str = str(Path(os.environ.get('ASTOR_DIR') or Path.home() / '.astor') / 'bot-binding.db')) -> tuple[int, list[str]]:
    if not Path(db_path).exists():
        return 1, [f'INV0: bot-binding.db does not exist at {db_path}']
    con = sqlite3.connect(db_path)
    problems = []
    try:
        # Inv 1: TG/DC/feishu/webchat exactly 1 row
        for r in con.execute(
            "SELECT platform_kind, COUNT(*) as n FROM platforms "
            "WHERE platform_kind IN ('telegram','discord','feishu','webchat') "
            "GROUP BY platform_kind HAVING n > 1"
        ):
            problems.append(f'INV1: {r[0]} has {r[1]} rows (expected 1)')

        # Inv 2: unique active (platform,chat)
        for r in con.execute(
            "SELECT platform_id, chat_id, COUNT(*) as n FROM bindings "
            "WHERE active=1 GROUP BY platform_id, chat_id HAVING n > 1"
        ):
            chat_short = r[1][:24]
            problems.append(f'INV2: {r[0]}:{chat_short} has {r[2]} active bindings')

        # Inv 3: every binding.user_id has user_meta
        for r in con.execute(
            "SELECT b.user_id, b.binding_id FROM bindings b "
            "LEFT JOIN user_meta u ON b.user_id=u.user_id "
            "WHERE u.user_id IS NULL"
        ):
            bid_short = r[1][:8] if r[1] else '?'
            problems.append(f'INV3: binding {bid_short} -> user {r[0]} (no user_meta)')

        # Inv 4: no empty user_id
        for r in con.execute(
            "SELECT binding_id FROM bindings WHERE user_id IS NULL OR user_id=''"
        ):
            bid_short = r[0][:8] if r[0] else '?'
            problems.append(f'INV4: binding {bid_short} empty user_id')

        # Inv 5: enabled platforms have non-empty token
        for r in con.execute(
            "SELECT platform_id FROM platforms WHERE enabled=1 AND (account_token IS NULL OR account_token='')"
        ):
            problems.append(f'INV5: enabled {r[0]} empty token')

        # Inv 6: weixin base_url
        for r in con.execute(
            "SELECT platform_id FROM platforms "
            "WHERE platform_kind='weixin' AND (base_url IS NULL OR base_url='')"
        ):
            problems.append(f'INV6: weixin {r[0]} no base_url')
    finally:
        con.close()
    return (0 if not problems else 1), problems


def main() -> int:
    rc, problems = check_all()
    if rc == 0:
        print('✅ all 6 invariants pass')
        try:
            astor_audit(
                actor='first_admin',
                tier='private',
                action='admin_op',
                target='bot-binding.db/invariants',
                reason='invariant check passed',
                metadata={'problems': []},
            )
        except Exception:
            pass
        return 0
    print(f'❌ {len(problems)} violations:')
    for p in problems:
        print(f'  - {p}')
    try:
        astor_audit(
            actor='first_admin',
            tier='private',
            action='admin_op',
            target='bot-binding.db/invariants',
            reason='invariant check failed',
            metadata={'problems': problems},
        )
    except Exception:
        pass
    return 1


if __name__ == '__main__':
    sys.exit(main())
