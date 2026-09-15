# -*- coding: utf-8 -*-
"""bus_reflect.py — session 末 / cron 末 自动反思 (v1.0.5, 2026-09-14)

设计目标:
  - 不调 LLM, 纯 SQL 聚合今天新写入的 canonical facts
  - 输出结构化 reflection fact 写回 astor, 让下次 session recall 能命中
  - 给 5-plugin-evolution-survey 2026-09-14 的"super-hermes 反思器"一个轻量本机版

用法:
  # 默认扫最近 24h 私有层 (admin)
  python bus_reflect.py

  # 扫 7 天, 公共层
  python bus_reflect.py --since 7d --tier public

  # 干跑, 只打印不写
  python bus_reflect.py --dry-run

部署位置:
  - **只在 source tree 跑** (`<ASTOR_REPO>/scripts/`), runtime 不需要
    它 — runtime 已经运行 server, 反思是外部工具, 不是 server 本身
    的功能。
  - 跟 `astor_grounding_audit.py` / `astor_health_diagnose.py` 模式一致:
    这些脚本都是 source-only 开发工具。
  - 触发方式: 手动跑 / cron (待 ship `bus-reflect-daily-2359mdt`)。

v1.0.5 changelog (2026-09-14):
  - Replace all 5 utcnow() with now(timezone.utc) — utcnow is deprecated
    in py3.12+ (DeprecationWarning), may be removed in py3.13+
  - Add main() docstring
  - Format __window_end__ with explicit +00:00 → Z conversion

v1.0.4 changelog (2026-09-14):
  - Exclude kind='reflection' from collect_facts() — otherwise each
    reflection fact inflates the next day's count by 1
  - Remove dead global BUS_DB / BUS_DB_PRIVATE references (v1.0.3)
  - Fix Chinese tokenization: 2-3 char grams for CJK + word tokens
    for latin (v1.0.3)
  - Fix promote_candidate tier: use short form consistently (v1.0.3)
  - Snapshot UTC clock once per run so text + SQL window match (v1.0.3)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import timezone
from pathlib import Path

# Default runtime paths (跟其他 astor script 一致)
ADMIN_USER = 'admin'
DEFAULT_ASTOR_DIR = Path(os.environ.get(
    'ASTOR_DIR',
    Path.home() / '.astor',
))


def parse_since(s: str) -> dt.datetime:
    """'24h' / '7d' / '2026-09-14' / '2026-09-14T12:00:00' → UTC datetime."""
    s = s.strip()
    now = dt.datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC for SQL
    if s.endswith('h') and s[:-1].isdigit():
        return now - dt.timedelta(hours=int(s[:-1]))
    if s.endswith('d') and s[:-1].isdigit():
        return now - dt.timedelta(days=int(s[:-1]))
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d'):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f'cannot parse --since: {s!r}')


def find_bus_db(tier: str, user_id: str | None) -> Path:
    """Find the SQLite file containing canonical facts for the requested tier.

    Verified layout (multi-tenant 9-db schema, 2026-09-14):
      <ASTOR_DIR>/users/<user>/memory/astor_bus_<user>.db   — private
      <ASTOR_DIR>/<tier>/memory/astor_bus_<tier>.db         — public / source
    """
    if tier == 'private' or tier.startswith('private_'):
        if user_id is None:
            user_id = ADMIN_USER
        return (
            DEFAULT_ASTOR_DIR / 'users' / user_id / 'memory'
            / f'astor_bus_{user_id}.db'
        )
    return (
        DEFAULT_ASTOR_DIR / tier / 'memory'
        / f'astor_bus_{tier}.db'
    )


def _tokenize_cjk_latin(text: str, max_tokens: int = 200) -> list[str]:
    """Tokenize a fact's content for hot-topic counting.

    v1.0.3 fix: previous split()-based version treated each Chinese character
    as its own token AND only kept the first 30 — useless for hot-topic
    detection. This version emits:
      - CJK runs: 2-3 char grams (catches recurring compound words)
      - Latin/alphanumeric: word tokens (len 3-20)
    """
    if not text:
        return []
    tokens: list[str] = []
    # CJK: pull 2- and 3-grams
    cjk_runs = re.findall(r'[\u4e00-\u9fff]+', text)
    for run in cjk_runs:
        if len(run) >= 2:
            for i in range(len(run) - 1):
                tokens.append(run[i:i + 2])
        if len(run) >= 3:
            for i in range(len(run) - 2):
                tokens.append(run[i:i + 3])
    # Latin: word tokens (len 3-20, alphanumeric)
    for word in re.findall(r'[A-Za-z][A-Za-z0-9_-]{2,19}', text):
        tokens.append(word.lower())
    return tokens[:max_tokens]


def collect_facts(since: dt.datetime, tier: str, user_id: str | None) -> dict:
    """Aggregate canonical facts created in [since, now].

    Returns dict with: total, by_kind, by_tag, recent_sample, hot_text_tokens.
    """
    db_path = find_bus_db(tier, user_id)
    if not db_path.exists():
        return {
            'error': f'bus db not found: {db_path}',
            'total': 0, 'by_kind': {}, 'by_tag': {}, 'recent_sample': [],
        }
    since_iso = since.isoformat() + 'Z'
    # v1.0.3 fix: snapshot "now" once so collect() and build_reflection_text()
    # use the same timestamp. Previously each called utcnow() separately
    # → window_end in stats differed from window_end in text by ms.
    # v1.0.5 fix: now(timezone.utc) (utcnow is deprecated in py3.12+).
    now = dt.datetime.now(timezone.utc).replace(tzinfo=None)
    now_iso = now.isoformat() + 'Z'
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # v1.0.1 (2026-09-14): use promoted_at column. memory_canonical
        # has no created_at — promoted_at is the canonical insert time
        # (TEXT, ISO-8601 with Z suffix). Tombstoned = 0 (NOT tombstoned_at).
        # v1.0.4 fix: exclude kind='reflection' rows from the count. Without
        # this, each reflection fact inflates the next day's "new_facts" by 1
        # and adds itself to by_kind — circular noise.
        rows = conn.execute(
            """SELECT id, content, kind, importance, tags, metadata,
                      origin_session_id, promoted_at
               FROM memory_canonical
               WHERE promoted_at >= ? AND promoted_at <= ?
                 AND (tombstoned = 0 OR tombstoned IS NULL)
                 AND kind != 'reflection'
               ORDER BY promoted_at ASC""",
            (since_iso, now_iso),
        ).fetchall()
    finally:
        conn.close()

    by_kind = Counter()
    by_tag = Counter()
    text_tokens = Counter()
    sample = []
    for r in rows:
        by_kind[r['kind'] or 'fact'] += 1
        try:
            tags = json.loads(r['tags'] or '[]')
        except Exception:
            tags = []
        for t in tags:
            by_tag[t] += 1
        # v1.0.3 fix: char-grams for CJK + word tokens for latin
        for tok in _tokenize_cjk_latin(r['content'] or ''):
            text_tokens[tok] += 1
        if len(sample) < 3:
            sample.append({
                'id': r['id'],
                'kind': r['kind'],
                'content_preview': (r['content'] or '')[:120],
                'promoted_at': r['promoted_at'],
            })
    return {
        'total': len(rows),
        'by_kind': dict(by_kind.most_common()),
        'by_tag': dict(by_tag.most_common(10)),
        'hot_tokens': [
            w for w, c in text_tokens.most_common(20) if c >= 2
        ],
        'recent_sample': sample,
        'window_end_iso': now_iso,
    }


def build_reflection_text(stats: dict, since: dt.datetime, tier: str) -> str:
    """Compose the human-readable reflection fact text."""
    # v1.0.3 fix: reuse stats['window_end_iso'] when available so the
    # text timestamp matches the SQL window. Fall back to now() if absent.
    end_iso = stats.get('window_end_iso')
    now = (dt.datetime.fromisoformat(end_iso.rstrip('Z'))
           if end_iso else dt.datetime.now(timezone.utc).replace(tzinfo=None))
    lines = [
        f'[bus_reflect · {since.strftime("%Y-%m-%d %H:%M")} → '
        f'{now.strftime("%Y-%m-%d %H:%M")} UTC · tier={tier}]',
    ]
    if stats.get('error'):
        lines.append(f'⚠️ {stats["error"]}')
        return '\n'.join(lines)
    lines.append(f'new_facts: {stats["total"]}')
    if stats['by_kind']:
        kind_str = ', '.join(f'{k}={v}' for k, v in stats['by_kind'].items())
        lines.append(f'by_kind: {kind_str}')
    if stats['by_tag']:
        tag_str = ', '.join(f'{k}={v}' for k, v in list(stats['by_tag'].items())[:5])
        lines.append(f'top_tags: {tag_str}')
    if stats['hot_tokens']:
        lines.append(f'hot_topics: {", ".join(stats["hot_tokens"][:8])}')
    if stats['recent_sample']:
        lines.append('recent_sample:')
        for s in stats['recent_sample'][:3]:
            lines.append(f"  #{s['id']} [{s['kind']}] {s['content_preview']}")
    # Capture-pipeline gap heuristics. v1.0.3: clarified — these are
    # heuristics, not real "user prefs locked in window" counters (that
    # counter was a planned-but-unimplemented v1.0.4 feature).
    if stats['total'] == 0:
        lines.append('⚠️ 0 new facts in window — reflection has nothing to reflect on.')
    elif stats['total'] < 3:
        lines.append('⚠️ very few new facts — possible capture-pipeline gap, '
                     'check whether facts are being inserted into the right tier.')
    return '\n'.join(lines)


def write_reflection_fact(
    text: str,
    stats: dict,
    tier: str,
    user_id: str,
    since: dt.datetime,
) -> int:
    """Write the reflection fact directly via bus API. Returns fact_id or 0.

    v1.0.2 (2026-09-14): bypass `am write` CLI because --mode none makes
    the extractor return [] and `am write` only writes an event, no fact.
    Direct insert_candidate + promote_candidate guarantees the fact lands
    in memory_canonical with kind='reflection' (which we tag via metadata
    for queryability).
    """
    import sys as _sys
    from pathlib import Path as _Path
    # Make astor_memory importable when this script is run directly.
    _pkg_root = _Path(__file__).resolve().parent.parent
    if str(_pkg_root) not in _sys.path:
        _sys.path.insert(0, str(_pkg_root))
    try:
        from astor_memory.bus import astor_bus
        from astor_memory._internal.acl import astor_init_acl
    except Exception as e:
        print(f'[ERR] import astor_memory failed: {e}', file=_sys.stderr)
        return 0

    # Bind process-level ACL (admin tier, owner role). tier must be the
    # short form — 'private' / 'public' / 'source' / 'repo' — user_id is
    # what disambiguates the private_<user> bus db.
    try:
        acl_tier = tier.split('_', 1)[0] if tier.startswith('private_') else tier
        astor_init_acl(
            actor='admin:admin',
            role='admin',
            tier=acl_tier,
            user_id=user_id,
        )
    except Exception as e:
        print(f'[ERR] astor_init_acl failed: {e}', file=_sys.stderr)
        return 0

    # v1.0.5 fix: now(timezone.utc) (utcnow is deprecated in py3.12+).
    today = dt.datetime.now(timezone.utc).strftime('%Y-%m-%d')
    metadata = {
        '__reflection__': True,
        '__window_start__': since.isoformat() + 'Z',
        '__window_end__': dt.datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        '__new_fact_count__': stats.get('total', 0),
        '__hot_tokens__': stats.get('hot_tokens', [])[:10],
        '__by_kind__': stats.get('by_kind', {}),
        '__top_tags__': list(stats.get('by_tag', {}).keys())[:5],
        '__generator__': 'bus_reflect.py v1.0.5',
    }
    try:
        bus_tier = tier.split('_', 1)[0] if tier.startswith('private_') else tier
        bus = astor_bus(tier=bus_tier, user_id=user_id)
        # 1) append event so the fact has a foreign-key target
        event_id = bus.append_event(
            namespace=user_id,
            agent_id='cli.reflect',
            source='cli.reflect',
            action='reflect',
            content=text,
        )
        # 2) insert candidate with kind='reflection'
        cand_id = bus.insert_candidate(
            event_id=event_id,
            namespace='meta:session_reflection',
            content=text,
            kind='reflection',
            confidence=0.85,
            importance=0.7,
            tags=['reflection', 'auto', today],
            metadata=metadata,
            scene='reflection',
        )
        # 3) promote to canonical. v1.0.3 fix: pass short tier form (e.g.
        # 'private'), not 'private_admin' — bus writes to the canonical table
        # keyed on user_id, not the long suffix.
        fact_id = bus.promote_candidate(
            cand_id, promoted_by='cli.reflect',
            user_id=user_id, tier=bus_tier,
        )
        return fact_id
    except Exception as e:
        print(f'[ERR] reflection write failed: {type(e).__name__}: {e}',
              file=_sys.stderr)
        return 0


def main() -> int:
    """CLI entry point. Returns 0 on success, 1 on write failure, 2 on argparse error."""
    p = argparse.ArgumentParser(
        description='Aggregate new canonical facts and write a reflection fact.',
    )
    p.add_argument('--since', default='24h',
                   help='24h | 7d | YYYY-MM-DD | YYYY-MM-DDTHH:MM:SS')
    p.add_argument('--tier', default='private',
                   choices=['public', 'source', 'private'],
                   help='Tier to reflect on (default private_<admin>)')
    p.add_argument('--user-id', default=ADMIN_USER,
                   help='User id for private tier (default admin)')
    p.add_argument('--dry-run', action='store_true',
                   help='Print reflection but do not write')
    p.add_argument('--astor-dir', default=None,
                   help='ASTOR_DIR override (default: env ASTOR_DIR or ~/.astor)')
    args = p.parse_args()

    # v1.0.3 fix: --astor-dir just rebinds the dir var, no other globals
    # touched (BUS_DB / BUS_DB_PRIVATE were removed in this version).
    if args.astor_dir:
        DEFAULT_ASTOR_DIR = Path(args.astor_dir)

    since = parse_since(args.since)
    tier = args.tier
    if tier == 'private':
        tier = f'private_{args.user_id}'

    stats = collect_facts(since, tier, args.user_id)
    text = build_reflection_text(stats, since, tier)
    print(text)
    print('---')
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if args.dry_run:
        print('[dry-run] reflection not written')
        return 0
    fact_id = write_reflection_fact(text, stats, tier, args.user_id, since)
    if fact_id:
        print(f'[OK] reflection fact written: id={fact_id} tier={tier}')
    else:
        print('[WARN] reflection fact was not written (see stderr above)',
              file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
