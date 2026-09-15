# -*- coding: utf-8 -*-
"""bus_reflect.py — session 末 / cron 末 自动反思 (v1.1.0, 2026-09-14)

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

v1.1.0 changelog (2026-09-14, Ship D):
  - Zone-partitioned reflection: instead of 1 mixed reflection, write 1
    fact per zone (success / failure / lesson). Skip neutral (fact/rule).
  - Zone configs:
      success → kind=reflection_success, importance=0.85, conf=0.90,
                namespace=meta:reflection:success
      failure → kind=reflection_failure, importance=0.90, conf=0.85,
                namespace=meta:reflection:failure
      lesson  → kind=reflection_lesson,  importance=0.99, conf=0.95,
                namespace=meta:reflection:lesson
  - Zone importance follows astor iron-rule convention (fact 3752)
  - collect_facts() returns new 'zones' dict with success/failure/lesson/neutral
  - Self-exclusion extended: also filter reflection_success/failure/lesson
    from collect (same circular-noise reason as v1.0.4)

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
        # v1.1.0 Ship D: also exclude reflection_success / reflection_failure /
        # reflection_lesson (zone-partitioned reflection facts) — same reason.
        rows = conn.execute(
            """SELECT id, content, kind, importance, tags, metadata,
                      origin_session_id, promoted_at
               FROM memory_canonical
               WHERE promoted_at >= ? AND promoted_at <= ?
                 AND (tombstoned = 0 OR tombstoned IS NULL)
                 AND kind NOT IN ('reflection', 'reflection_success',
                                  'reflection_failure', 'reflection_lesson')
               ORDER BY promoted_at ASC""",
            (since_iso, now_iso),
        ).fetchall()
    finally:
        conn.close()

    by_kind = Counter()
    by_tag = Counter()
    text_tokens = Counter()
    sample = []
    # v1.1.0 (2026-09-14, Ship D): partition facts by zone for downstream
    # reflection. Per astor 3-zone architecture (fact 11938):
    #   success   → user_preference / success_pattern → public
    #   failure   → failure_pattern                   → public
    #   lesson    → postmortem / lesson               → private (LESSON prefix)
    #   neutral   → fact / rule / decision / etc.     → no zone promotion
    success_kinds = {'user_preference', 'success_pattern'}
    failure_kinds = {'failure_pattern'}
    lesson_kinds = {'postmortem', 'lesson'}
    success_facts: list[dict] = []
    failure_facts: list[dict] = []
    lesson_facts: list[dict] = []
    neutral_facts: list[dict] = []
    for r in rows:
        kind = r['kind'] or 'fact'
        by_kind[kind] += 1
        try:
            tags = json.loads(r['tags'] or '[]')
        except Exception:
            tags = []
        for t in tags:
            by_tag[t] += 1
        # v1.0.3 fix: char-grams for CJK + word tokens for latin
        for tok in _tokenize_cjk_latin(r['content'] or ''):
            text_tokens[tok] += 1
        rec = {
            'id': r['id'],
            'kind': kind,
            'content_preview': (r['content'] or '')[:120],
            'promoted_at': r['promoted_at'],
        }
        if kind in success_kinds:
            success_facts.append(rec)
        elif kind in failure_kinds:
            failure_facts.append(rec)
        elif kind in lesson_kinds:
            lesson_facts.append(rec)
        else:
            neutral_facts.append(rec)
        if len(sample) < 3:
            sample.append(rec)
    return {
        'total': len(rows),
        'by_kind': dict(by_kind.most_common()),
        'by_tag': dict(by_tag.most_common(10)),
        'hot_tokens': [
            w for w, c in text_tokens.most_common(20) if c >= 2
        ],
        'recent_sample': sample,
        'window_end_iso': now_iso,
        # v1.1.0 Ship D: zone-partitioned facts
        'zones': {
            'success': success_facts,
            'failure': failure_facts,
            'lesson': lesson_facts,
            'neutral': neutral_facts,
        },
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
    """Write per-zone reflection facts directly via bus API.

    v1.1.0 (2026-09-14, Ship D): zone-partitioned reflection. Instead of
    one mixed reflection, write one fact per zone (success / failure /
    lesson) and skip neutral. Each zone uses:
      - kind='reflection' (so they all show up in one recall bucket)
      - importance matched to the zone's iron-rule importance
        (success 0.85 / failure 0.90 / lesson 0.99)
      - namespace encodes the zone: meta:reflection:success / failure / lesson
      - tags include the zone name for filterable queries
    Returns the FIRST fact_id written, or 0 if all failed.
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
    now_iso = dt.datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

    # v1.1.0 Ship D: zone config table. importance follows the iron-rule
    # convention from fact 3752.
    zones_cfg = [
        # (zone_name, importance, confidence, kind_for_zone)
        ('success', 0.85, 0.90, 'reflection_success'),
        ('failure', 0.90, 0.85, 'reflection_failure'),
        ('lesson',  0.99, 0.95, 'reflection_lesson'),
    ]
    zones_data = (stats.get('zones') or {})
    first_fact_id = 0
    try:
        bus_tier = tier.split('_', 1)[0] if tier.startswith('private_') else tier
        bus = astor_bus(tier=bus_tier, user_id=user_id)
        for zone_name, imp, conf, kind in zones_cfg:
            zone_facts = zones_data.get(zone_name) or []
            if not zone_facts:
                continue
            # v1.1.0 Ship D: zone-prefixed text. Include a header line
            # so the fact is self-describing.
            zone_text = (
                f'[bus_reflect · {zone_name} · '
                f'{since.strftime("%Y-%m-%d %H:%M")} → {now_iso.rstrip("Z")} UTC] '
                f'count={len(zone_facts)}'
            )
            for f in zone_facts[:5]:  # cap per-zone sample at 5
                zone_text += (
                    f"\n  #{f['id']} [{f['kind']}] {f['content_preview']}"
                )
            metadata = {
                '__reflection__': True,
                '__zone__': zone_name,
                '__window_start__': since.isoformat() + 'Z',
                '__window_end__': now_iso,
                '__zone_count__': len(zone_facts),
                '__by_kind__': stats.get('by_kind', {}),
                '__hot_tokens__': stats.get('hot_tokens', [])[:10],
                '__generator__': 'bus_reflect.py v1.1.0',
            }
            event_id = bus.append_event(
                namespace=user_id,
                agent_id='cli.reflect',
                source='cli.reflect',
                action=f'reflect.{zone_name}',
                content=zone_text,
            )
            cand_id = bus.insert_candidate(
                event_id=event_id,
                namespace=f'meta:reflection:{zone_name}',
                content=zone_text,
                kind=kind,
                confidence=conf,
                importance=imp,
                tags=['reflection', zone_name, 'auto', today],
                metadata=metadata,
                scene='reflection',
            )
            fact_id = bus.promote_candidate(
                cand_id, promoted_by='cli.reflect',
                user_id=user_id, tier=bus_tier,
            )
            if fact_id and not first_fact_id:
                first_fact_id = fact_id
            print(
                f'[OK] reflection.{zone_name} written: id={fact_id} '
                f'count={len(zone_facts)} imp={imp}',
                file=_sys.stderr,
            )
        return first_fact_id
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
