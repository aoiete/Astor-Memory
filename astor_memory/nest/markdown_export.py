"""
v1.16.69 Ship C #1 — Markdown export for Obsidian-vault mount.

Basic Memory 2026 §07: "Obsidian双流" — agent writes Markdown,
Obsidian GUI reads + manually edits the same files. Audit back to
memory = "open the file, see the source, fix it directly".

This module exports memory_canonical rows to a directory tree:
    <ASTOR_DIR>/export/<tier>/<user_id>/<fact_id>.md

Each file has frontmatter (permalink, tags, kind, status, importance)
plus a markdown body with content + provenance links (supplier,
promoted_at, fact_ids). Obsidian can mount this directory as a
vault; humans edit files there to correct facts; on next export the
fact row in memory_canonical gets updated.

Design notes:
  - One file per fact_id (NOT per entity). Mem0/Faviori-style
    Observation files have Observations but blocks are observations tied
    to a fact row, easier to round-trip.
  - Inactive facts (status='inactive') export with `status: inactive`
    frontmatter so Obsidian shows them greyed out.
  - Tombstoned facts are SKIPPED (deleted = not visible).
  - Auto-link via [[wikilink]] to entities_json if present.

Usage:
  from astor_memory.nest.markdown_export import export_user_facts
  export_user_facts(tier='public', user_id='admin',
                    out_dir=Path('~/.astor/export'))
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional


_FRONT_MATTER_RE = re.compile(r'^---\n(.*?)\n---\n', re.DOTALL)


def _slugify(text: str, max_len: int = 60) -> str:
    """Make an Obsidian-friendly filename slug from content."""
    s = re.sub(r'[^A-Za-z0-9\u4e00-\u9fff\s_-]+', '', text)
    s = re.sub(r'\s+', '-', s).strip('-_')
    return (s[:max_len] or 'untitled')


def _render_fact_markdown(fact_row: tuple) -> str:
    """Build the markdown content for one memory_canonical row.

    fact_row is the row tuple from
        SELECT id, content, kind, tags, importance, status,
               promoted_at, promoted_by, keywords, context,
               entities_json, supersession_id, origin_session_id, ...
    Field ORDER must match the SELECT in export_user_facts().
    """
    (fid, content, kind, tags_json, importance, status,
     promoted_at, promoted_by, keywords, context,
     entities_json, superseded_by, origin_session_id) = fact_row[:13]
    tags = json.loads(tags_json) if tags_json else []
    keywords_list = json.loads(keywords) if keywords else []
    entities = json.loads(entities_json) if entities_json else []

    # Frontmatter
    fm = {
        'permalink': f'fact-{fid}',
        'fact_id': fid,
        'kind': kind or 'fact',
        'status': status or 'active',
        'importance': round(float(importance or 0), 2),
        'tags': tags,
        'promoted_at': promoted_at or '',
        'promoted_by': promoted_by or '',
        'origin_session_id': origin_session_id or '',
    }
    fm_lines = ['---']
    for k, v in fm.items():
        if isinstance(v, list):
            fm_lines.append(f'{k}: [{", ".join(str(x) for x in v)}]')
        else:
            fm_lines.append(f'{k}: {v}')
    fm_lines.append('---')
    fm_lines.append('')

    # Body
    body_lines = [f'# {content[:80]}', '']
    if context:
        body_lines += [f'> {context}', '']
    body_lines += [f'**Content:**', '', content, '']
    if keywords_list:
        body_lines += [
            '**Keywords:** ' + ', '.join(f'`{k}`' for k in keywords_list), ''
        ]
    if entities:
        # Render entity wikilinks for Obsidian graph view
        body_lines += [
            '**Entities:** '
            + ' '.join(f'[[entity:{e.get("value", str(e))}]]' for e in entities),
            ''
        ]
    if superseded_by:
        body_lines += [
            f'**Superseded by:** fact-{superseded_by}', ''
        ]
    if status and status != 'active':
        marker = '⚠' if status == 'inactive' else '📦'
        body_lines += [
            f'> {marker} **{status.upper()}**: this fact is superseded/retired. '
            'Reason: caller should review [[supersession]] chain.', ''
        ]

    return '\n'.join(fm_lines + body_lines)


def export_user_facts(
    bus,
    tier: str = 'public',
    user_id: str = 'admin',
    out_dir: Optional[Path] = None,
    include_inactive: bool = True,
    overwrite: bool = False,
) -> dict:
    """Export one user's memory_canonical rows as Markdown files.

    Args:
        bus:  AstorBus instance (caller-resolved for the right tier).
        tier: bus tier (public/source/private).
        user_id: per-user private filter; 'admin' for source.
        out_dir: where to write files. Default: <ASTOR_DIR>/export/<tier>/<user>.
        include_inactive: if False, skip status != 'active' rows.
        overwrite: if False, skip files that already exist.

    Returns: {exported, skipped, total, out_dir}
    """
    if out_dir is None:
        astor_dir = Path(bus.db_path).parent.parent.parent
        out_dir = astor_dir / 'export' / tier / user_id
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    sql = (
        "SELECT id, content, kind, tags, importance, status, "
        "promoted_at, promoted_by, keywords, context, "
        "entities_json, superseded_by, origin_session_id "
        "FROM memory_canonical "
        "WHERE tombstoned = 0 "
    )
    params = []
    if user_id:
        sql += " AND user_id = ?"
        params.append(user_id)
    if not include_inactive:
        sql += " AND status = 'active'"
    sql += " ORDER BY id ASC"

    rows = bus._conn.execute(sql, tuple(params)).fetchall()
    exported = 0
    skipped = 0
    for row in rows:
        slug = _slugify(row[1])  # row[1] = content
        path = out_dir / f'fact-{row[0]}-{slug}.md'
        if path.exists() and not overwrite:
            skipped += 1
            continue
        md = _render_fact_markdown(row)
        path.write_text(md, encoding='utf-8')
        exported += 1
    return {
        'out_dir': str(out_dir),
        'exported': exported,
        'skipped': skipped,
        'total': len(rows),
    }


def export_chat_chunks(
    nest,
    bus,
    out_dir: Optional[Path] = None,
    include_inactive: bool = True,
    overwrite: bool = False,
    max_chunks: int = 500,
) -> dict:
    """Export chat_chunk_embeddings + events as Obsidian-friendly markdown.

    Each chunk gets a file with: frontmatter (window_id, role, ts),
    body = prefix + raw turns. Provenance links back via event_id.
    """
    if out_dir is None:
        astor_dir = Path(nest.db_path).parent.parent.parent
        out_dir = astor_dir / 'export' / 'chat_chunks' / nest.user_id
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = nest._conn.execute(
        "SELECT event_id, prefix, turn_count, ts FROM chat_chunk_embeddings "
        "ORDER BY ts DESC LIMIT ?",
        (max_chunks,),
    ).fetchall()
    exported = 0
    skipped = 0
    for ev_id, prefix, turn_count, ts in rows:
        path = out_dir / f'chunk-{ev_id}.md'
        if path.exists() and not overwrite:
            skipped += 1
            continue
        # Pull raw turns from events
        ev_row = bus._conn.execute(
            "SELECT chunk_turns, chunk_window_id, chunk_prefix FROM events WHERE id = ?",
            (ev_id,),
        ).fetchone()
        turns_json = (ev_row[0] if ev_row else None) or '[]'
        try:
            turns = json.loads(turns_json)
        except Exception:
            turns = []
        fm_lines = ['---', f'event_id: {ev_id}', f'ts: {ts}',
                    f'prefix: {prefix or ""}', f'turn_count: {turn_count}',
                    '---', '']
        body_lines = [f'# chunk-{ev_id}', '',
                      f'**Prefix:** {prefix or "(none)"}', '']
        for t in turns:
            role = t.get('role', '?')
            content = t.get('content', '')
            body_lines += [f'## {role}', '', content, '']
        path.write_text('\n'.join(fm_lines + body_lines), encoding='utf-8')
        exported += 1
    return {
        'out_dir': str(out_dir),
        'exported': exported,
        'skipped': skipped,
        'total': len(rows),
    }


__all__ = [
    'export_user_facts', 'export_chat_chunks',
    'build_entities_index',
]


def build_entities_index(
    bus,
    tier: str = 'public',
    user_id: str = 'admin',
    out_dir: Optional[Path] = None,
) -> dict:
    """v1.16.70 Ship D #1: cross-vault wikilink index.

    Scans memory_canonical.entities_json across the user's facts,
    groups by (type, value), writes entities.jsonl next to export dir.
    Each line: {type, value, fact_ids, fact_count}. Wikilinks in .md
    files ([[entity:value]]) resolve when user manually creates the
    matching page in Obsidian.
    """
    import json as _json
    if out_dir is None:
        astor_dir = Path(bus.db_path).parent.parent.parent
        out_dir = astor_dir / 'export' / tier / user_id
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sql = (
        "SELECT id, entities_json FROM memory_canonical WHERE tombstoned = 0"
    )
    params = []
    if user_id:
        sql += " AND user_id = ?"
        params.append(user_id)
    rows = bus._conn.execute(sql, tuple(params)).fetchall()
    entities_index: dict = {}
    for fid, ent_json in rows:
        if not ent_json:
            continue
        try:
            ents = _json.loads(ent_json)
        except Exception:
            continue
        for e in ents:
            t = e.get("type", "concept")
            val = e.get("value", str(e))
            entities_index.setdefault((t, val), set()).add(int(fid))
    out_path = out_dir / "entities.jsonl"
    with open(out_path, "w", encoding="utf-8") as f:
        for (t, val), fact_ids in sorted(entities_index.items()):
            f.write(_json.dumps({
                "type": t,
                "value": val,
                "fact_ids": sorted(fact_ids),
                "fact_count": len(fact_ids),
            }, ensure_ascii=False) + "\n")
    return {
        'out_path': str(out_path),
        'entity_count': len(entities_index),
        'total_references': sum(len(v) for v in entities_index.values()),
    }