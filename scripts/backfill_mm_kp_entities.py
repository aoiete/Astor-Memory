"""backfill_mm_kp_entities.py — S27 (2026-09-30) one-shot utility.

Why: 10 source-tier facts (5 mental_models + 5 knowledge_pages written by
S24/S25 auto-promote crons) + 11 pre-feature astor peer-identity rules
lacked `entities_json`.

Effect: dashboard entities_coverage.source = 0.75 instead of ~1.0.

Fix: backfill entities_json from each fact's `metadata` field + content:
  - `parent_fact_ids`     → ["evidence:504", ...]
  - `__keywords__`        → ["keyword:astor", ...]
  - `__topic__`           → ["topic:astor-peer-qa-1"]
  - `slug` (KP)           → ["slug:astor-architecture"]
  - `question` (MM)       → ["question:<slugified question>"]
  - `tags`                → ["tag:..."]
  - content fallback      → noun-phrase extraction (English capitalized + Chinese 2-4 char)

Run once: `python backfill_mm_kp_entities.py [--execute]`
Default: dry-run. --execute to actually write.

v1.15.53 S27 (2026-09-30).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ASTOR_DIR = Path(os.environ.get('ASTOR_DIR', r'D:\AI\Astor-Memory-Runtime'))


def _slugify(s: str) -> str:
    s = re.sub(r'[^a-zA-Z0-9]+', '-', s).strip('-').lower()
    return s[:80]


def _extract_entities(metadata_json: str, kind: str, content: str = '') -> list[str]:
    """Build entities_json from a fact's stored metadata + content.

    Sources (in order, dedup'd, capped at 32):
      - `parent_fact_ids`     → evidence:N
      - `__keywords__`        → keyword:K
      - `__topic__`           → topic:K (rounds 1-N)
      - `slug` (KP)           → slug:K
      - `question` (MM)       → question:K
      - `tags` array          → tag:K
      - content noun scan     → keyword:K (fallback for facts without metadata)
    """
    entities: list[str] = []
    if metadata_json and metadata_json != '{}':
        try:
            m = json.loads(metadata_json)
        except (ValueError, TypeError):
            m = {}
        for pid in m.get('parent_fact_ids', []):
            entities.append(f'evidence:{int(pid)}')
        for kw in m.get('__keywords__', []):
            if isinstance(kw, str):
                entities.append(f'keyword:{kw}')
        topic = m.get('__topic__', '')
        if isinstance(topic, str) and topic:
            entities.append(f'topic:{topic}')
        if kind == 'knowledge_page' and m.get('slug'):
            entities.append(f'slug:{m["slug"]}')
        if kind == 'mental_model' and m.get('question'):
            entities.append(f'question:{_slugify(m["question"])}')
        for tag in m.get('tags', []):
            if isinstance(tag, str):
                entities.append(f'tag:{tag}')
    # Fallback: noun phrase extraction from content if still empty
    if not entities and content:
        entities.extend(_extract_keywords_from_content(content))
    # Dedup + cap
    seen = set()
    out = []
    for e in entities:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out[:32]


# Common words to skip in keyword extraction
_STOPWORDS = {
    'a', 'an', 'the', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for',
    'of', 'with', 'by', 'from', 'as', 'is', 'was', 'are', 'were', 'be',
    'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will',
    'would', 'should', 'could', 'may', 'might', 'can', 'must', 'shall',
    'this', 'that', 'these', 'those', 'i', 'you', 'he', 'she', 'it', 'we',
    'they', 'me', 'him', 'her', 'us', 'them', 'my', 'your', 'his', 'its',
    'our', 'their', 'what', 'which', 'who', 'when', 'where', 'why', 'how',
    'all', 'each', 'every', 'both', 'few', 'more', 'most', 'other', 'some',
    'such', 'no', 'not', 'only', 'own', 'same', 'so', 'than', 'too', 'very',
    'just', '也', '的', '了', '是', '在', '有', '和', '与', '或', '但',
    '不', '也', '就', '都', '而', '及', '着', '过', '以', '把', '被',
    '让', '给', '对', '从', '到', '用', '于', '为', '上', '下', '前',
    '后', '里', '外', '中', '一个', '一些', '这是', '那是', '我们',
    '你们', '他们', '可以', '应该', '需要', '现在', '然后', '因为',
    '所以', '如果', '但是', '虽然', '然而', '而且', '并且', '因此',
}


def _extract_keywords_from_content(content: str) -> list[str]:
    """Pull noun-ish keywords from content (English + Chinese)."""
    if not content:
        return []
    out: list[str] = []
    # English: capitalized words or hyphenated phrases
    for m in re.finditer(r'\b([A-Z][a-zA-Z]{2,}(?:[-_][a-zA-Z]+)*)\b', content):
        kw = m.group(1).lower()
        if kw not in _STOPWORDS and len(kw) <= 40:
            out.append(f'keyword:{kw}')
    # Chinese: 2-4 char noun-like runs (no spaces between CJK)
    for m in re.finditer(r'([\u4e00-\u9fff]{2,4})', content):
        kw = m.group(1)
        if kw not in _STOPWORDS:
            out.append(f'keyword:{kw}')
    return out[:12]


def backfill_one(db_path: Path, kind_filter: tuple, execute: bool) -> dict:
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        f"SELECT id, kind, metadata, content FROM memory_canonical "
        f"WHERE kind IN ({','.join('?' * len(kind_filter))}) "
        f"AND tombstoned = 0 AND (entities_json IS NULL OR entities_json = '' OR entities_json = '[]')",
        kind_filter,
    )
    rows = cur.fetchall()
    candidates = []
    for fid, kind, meta, content in rows:
        ents = _extract_entities(meta or '', kind, content or '')
        if ents:
            candidates.append((fid, ents))
    if not execute:
        conn.close()
        return {
            'candidates': len(candidates),
            'sample': candidates[:2],
            'action': 'preview',
        }
    with conn:
        for fid, ents in candidates:
            cur.execute(
                "UPDATE memory_canonical SET entities_json = ? "
                "WHERE id = ? AND (entities_json IS NULL OR entities_json = '' OR entities_json = '[]')",
                (json.dumps(ents, ensure_ascii=False), fid),
            )
    conn.close()
    return {
        'updated': len(candidates),
        'action': 'upserted',
    }


def main():
    ap = argparse.ArgumentParser(description='S27 backfill entities_json for source-tier MM/KP')
    ap.add_argument('--execute', action='store_true', default=False,
                    help='Actually write (default: dry-run)')
    args = ap.parse_args()
    src_db = ASTOR_DIR / 'source' / 'memory' / 'astor_bus_source.db'
    if not src_db.exists():
        print(json.dumps({'error': 'source bus DB not found', 'path': str(src_db)}, indent=2))
        return
    r = backfill_one(src_db, ('mental_model', 'knowledge_page', 'rule'), args.execute)
    summary = {
        'dry_run': not args.execute,
        'astor_dir': str(ASTOR_DIR),
        'source_db': str(src_db),
        'result': r,
        'ts': datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
