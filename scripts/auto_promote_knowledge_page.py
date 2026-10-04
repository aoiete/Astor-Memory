"""auto_promote_knowledge_page.py — A1 (2026-09-29) S25 cron.

Why: knowledge_pages API exists since v1.15.39 but the slot was empty (0 rows
across all tiers). This cron auto-creates topic-aggregating knowledge_pages
that link the top 10 facts per topic — gives mental_model callers a "table of
contents" / directory view to navigate related facts.

Run monthly:  `python auto_promote_knowledge_page.py --execute`
Dry-run:     `python auto_promote_knowledge_page.py` (default)

Topics seeded (deterministic, no LLM):
  1. astor-architecture  — 9-DB layout, 3-tier × 3-store, memory_class taxonomy
  2. hermes-runtime     — gateway / hooks / skill routing
  3. maker-pocket        — live trading loop + state machine
  4. trading-policy     — portfolio / rebalance / TFSA+CASH / R-class rules
  5. moomoo-bridge       — OpenD / SDK / order flow

Each knowledge_page is created via the upsert_knowledge_page() pipeline so
it gets a proper event_id, candidate_id, entities, embedding — same as a
manual `am knowledge-page upsert`. Re-runs idempotent (supersedes prior
same-slug via tombstone).

v1.15.51 A1 (2026-09-29).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from astor_memory._internal.acl import astor_init_acl
from astor_memory.bus.store import astor_bus
from astor_memory.nest.knowledge_pages import upsert_knowledge_page


def query_evidence(bus_db: str, sql: str, limit: int = 10) -> list[dict]:
    """Read top evidence rows directly from bus DB."""
    conn = sqlite3.connect(bus_db)
    try:
        cur = conn.execute(sql + f" LIMIT {limit}")
        cols = [r[0] for r in cur.description] if cur.description else []
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


# === Topic definitions (deterministic body synthesis) ===

TOPICS = [
    {
        'slug': 'astor-architecture',
        'title': 'Astor-Memory Architecture',
        'evidence_sql': (
            "SELECT id, content FROM memory_canonical "
            "WHERE tombstoned = 0 AND "
            "(content LIKE '%9-DB%' OR content LIKE '%tier ×%' OR content LIKE '%3-tier%' "
            " OR content LIKE '%memory_class%' OR content LIKE '%bus.promote%') "
            "AND importance >= 0.5 "
            "ORDER BY importance DESC, created_at DESC"
        ),
        'body_template': (
            "# Astor-Memory Architecture\n\n"
            "## 9-DB Layout (3-tier × 3-store)\n"
            "- **public** tier: shared facts (570 active)\n"
            "- **source** tier: operator rules (87 active)\n"
            "- **private_<user>** tier: per-user memory (~4300 active for admin)\n\n"
            "Each tier has 3 stores: bus (event log + canonical), nest (vector embeddings), "
            "forge (extractor state).\n\n"
            "## 4-Class memory_class taxonomy (S23)\n"
            "After v1.15.49 auto-mapping, every canonical fact is classified:\n"
            "- **world_fact** (default, ~96% of facts)\n"
            "- **mental_model** (3.7%, Hindsight-style answer sheets — S24 auto-promotes 5)\n"
            "- **experience** (2.7%, success/failure patterns + lessons)\n"
            "- **observation** (currently 0, reserved for episodic facts)\n\n"
            "## Key Endpoints\n"
            "- POST /v1/write — promote a fact (insert_candidate → promote_candidate)\n"
            "- POST /v1/read — hybrid recall (BM25 + vector + MMR)\n"
            "- POST /v1/forget — soft-tombstone (default) or hard-delete\n"
            "- POST /v1/backfill_memory_class — S23 one-shot backfill\n"
            "- GET  /v1/mental_model — S25 mental_model server endpoint (in flight)\n\n"
            "## Lifecycle\n"
            "capture_intent → forge.extract → bus.promote → recall → "
            "decay-sweep → optional reflection merge."
        ),
    },
    {
        'slug': 'hermes-runtime',
        'title': 'Hermes Runtime Architecture',
        'evidence_sql': (
            "SELECT id, content FROM memory_canonical "
            "WHERE tombstoned = 0 AND "
            "(content LIKE '%hermes-agent%' OR content LIKE '%gateway%' "
            " OR content LIKE '%hook%' OR content LIKE '%astor-extract%') "
            "AND importance >= 0.5 "
            "ORDER BY importance DESC, created_at DESC"
        ),
        'body_template': (
            "# Hermes Runtime\n\n"
            "## Components\n"
            "- **gateway** — multi-platform messaging (Telegram/Discord/WeChat/Slack)\n"
            "- **agent** — LLM-driven orchestration\n"
            "- **hooks** — pre/post tool-call gates\n"
            "- **hermes_adapter** — bus<->hermes bridge (one-shot, no streaming)\n\n"
            "## Hard Rules\n"
            "- **R218**: hermes-agent no auto-restart (user-only)\n"
            "- **R218 source-tier** (R354): source tier writes are admin-only\n"
            "- **R236**: silent ACL deny (no policy leak)\n\n"
            "## Hook Chain (per /v1/write or capture_intent)\n"
            "1. post_tool_call: capture_intent → astor_write\n"
            "2. on_session_end: astor_extract + skill_evolve + daily_sweep\n"
            "3. on_session_reset: astor_daily_sweep\n"
            "4. api_request_error: astor_api_error\n"
            "5. pre_tool_call: memory-astor-gate (blocks memory writes when astor alive)"
        ),
    },
    {
        'slug': 'maker-pocket',
        'title': 'Maker Pocket Trading Loop',
        'evidence_sql': (
            "SELECT id, content FROM memory_canonical "
            "WHERE tombstoned = 0 AND "
            "(content LIKE '%maker_pocket%' OR content LIKE '%regime%' "
            " OR content LIKE '%TP%' OR content LIKE '%SL%' "
            " OR content LIKE '%ENTERED%' OR content LIKE '%IDLE%') "
            "AND importance >= 0.5 "
            "ORDER BY importance DESC, created_at DESC"
        ),
        'body_template': (
            "# Maker Pocket Live Trading Loop\n\n"
            "## State Machine\n"
            "IDLE → ARMED → ENTERED → CLOSE_PROFIT/LOSS/TIMEOUT → IDLE\n\n"
            "## Risk Parameters (current)\n"
            "- Pocket size: $3000 USDC\n"
            "- Daily DD limit: 1.5%\n"
            "- Total DD terminal STOPPED: 9%\n"
            "- Per-trade DD: 0.4%\n\n"
            "## Decision Layers\n"
            "- Regime router (RANGE / TREND / WEAK_DOWN / STRONG_DOWN)\n"
            "- v0.62.5+ safety: real-time balance check, no state cache\n"
            "- v0.55+ dynamic TP/SL: rebracket based on 7d/30d high + MA100\n\n"
            "## v0.63.5 Push Filter (today's ship)\n"
            "- ENTERED→IDLE only pushes for real TP/SL fill (CLOSE_PROFIT/LOSS)\n"
            "- Orphan / timeout / manual cancel are silent (don't spam Discord)"
        ),
    },
    {
        'slug': 'trading-policy',
        'title': 'Trading & Portfolio Policy',
        'evidence_sql': (
            "SELECT id, content FROM memory_canonical "
            "WHERE tombstoned = 0 AND "
            "(content LIKE '%portfolio%' OR content LIKE '%TFSA%' OR content LIKE '%CASH%' "
            " OR content LIKE '%trim%' OR content LIKE '%rebalance%' "
            " OR content LIKE '%R130%' OR content LIKE '%R106%') "
            "AND importance >= 0.5 "
            "ORDER BY importance DESC, created_at DESC"
        ),
        'body_template': (
            "# Trading & Portfolio Policy\n\n"
            "## Account Structure\n"
            "- **TFSA**: Canadian tax-sheltered, long-term holds\n"
            "- **CASH**: margin account, active rebalance\n\n"
            "## Hard Rules\n"
            "- **R130**: cross-account trim → trim the LOWER basis account\n"
            "- **R106**: position sizing — no single stock over concentration limit\n"
            "- **11928**: trim decisively, no half-measures (cut 2/3 or all)\n\n"
            "## Tools\n"
            "- `opend_cli.py positions --market US --security-firm FUTUCA`\n"
            "- `opend_cli.py place-order --time-in-force GTC`\n"
            "- moomoo SDK directly for fills, opend_cli for ops\n\n"
            "## Workflow\n"
            "1. Pull positions via opend_cli (4 parallel sources — R128)\n"
            "2. Read finance-analysis skill for thesis check\n"
            "3. User decides trim vs hold\n"
            "4. Place GTC limit (default 30-60 day)\n"
            "5. Verify via broker app SSoT (R129)"
        ),
    },
    {
        'slug': 'moomoo-bridge',
        'title': 'Moomoo / OpenD Bridge',
        'evidence_sql': (
            "SELECT id, content FROM memory_canonical "
            "WHERE tombstoned = 0 AND "
            "(content LIKE '%opend%' OR content LIKE '%moomoo%' "
            " OR content LIKE '%OpenD%' OR content LIKE '%11111%') "
            "AND importance >= 0.5 "
            "ORDER BY importance DESC, created_at DESC"
        ),
        'body_template': (
            "# Moomoo / OpenD Bridge\n\n"
            "## Components\n"
            "- **OpenD** — Moomoo's local daemon, listens on :11111\n"
            "- **opend_cli.py** — `<host>/opend/` — agent-friendly CLI wrapper\n"
            "- **moomoo SDK** — `<venv>/Lib/site-packages/moomoo/`\n\n"
            "## Critical Rules\n"
            "- OpenD must be running before any `opend_cli.py positions` call\n"
            "- Default `--market US --security-firm FUTUCA` for Canadian accounts\n"
            "- `opend_cli.py place-order --time-in-force GTC` bypasses DAY TIF default\n"
            "- CA-market trades blocked via API (manual app only) — pitfall in moomoo-currency-pitfall skill\n\n"
            "## NSSM Service\n"
            "`OpenD-AutoStart` (DisplayName 'OpenD AutoStart (moomoo bridge)', Start=Auto) — "
            "registered 9/9 after Windows Update wiped the previous watchdog. "
            "24h guard loop, port 11111 monitoring, auto-restart on disconnect.\n\n"
            "## 4 Source Verification (R128)\n"
            "When pulling portfolio state: positions + live orders + fills + cash — "
            "4 parallel sources. Broker app is SSoT if conflicts."
        ),
    },
]


def synthesize_body(topic: dict, evidence: list) -> str:
    template = topic['body_template']
    if not evidence:
        return template + "\n\n_(no recent evidence rows in bus)_\n"
    ids = [r['id'] for r in evidence]
    return template + f"\n\n## Source Facts\n\nReferenced: {ids}\n"


def main():
    ap = argparse.ArgumentParser(description='A1 auto-promote knowledge_page cron')
    ap.add_argument('--tier', default='source', choices=['public', 'source', 'private'])
    ap.add_argument('--user-id', default='admin')
    ap.add_argument('--dry-run', action='store_true', default=True)
    ap.add_argument('--execute', action='store_true')
    ap.add_argument('--bus-db',
                    default=str(os.environ.get('ASTOR_DIR') or Path.home() / '.astor') + '/users/admin/memory/astor_bus_admin.db',
                    help='admin bus DB path for evidence queries')
    args = ap.parse_args()

    if args.execute:
        args.dry_run = False

    results = []
    for topic in TOPICS:
        evidence = query_evidence(args.bus_db, topic['evidence_sql'], limit=10)
        body = synthesize_body(topic, evidence)
        n_evidence = len(evidence)
        ids = [r['id'] for r in evidence]
        if args.dry_run:
            results.append({
                'slug': topic['slug'],
                'title': topic['title'],
                'body_preview': body[:120] + '...',
                'evidence_count': n_evidence,
                'evidence_ids': ids[:5],
                'action': 'preview',
            })
            continue
        # Execute: direct upsert via astor_init_acl + upsert_knowledge_page
        try:
            astor_init_acl(
                actor='admin:admin', role='admin', tier=args.tier,
                user_id=args.user_id if args.tier == 'private' else None,
            )
            bus = astor_bus(tier=args.tier,
                            user_id=args.user_id if args.tier == 'private' else None)
            fact_id = upsert_knowledge_page(
                bus,
                slug=topic['slug'],
                title=topic['title'],
                body=body,
                tier=args.tier,
                user_id=args.user_id if args.tier == 'private' else None,
                parent_fact_ids=ids,
                confidence=0.7,
            )
            results.append({
                'slug': topic['slug'],
                'fact_id': fact_id,
                'evidence_count': n_evidence,
                'action': 'upserted',
            })
        except Exception as exc:
            results.append({
                'slug': topic['slug'],
                'error': str(exc),
                'action': 'failed',
            })
    summary = {
        'tier': args.tier,
        'user_id': args.user_id,
        'dry_run': args.dry_run,
        'topic_count': len(TOPICS),
        'results': results,
        'ts': datetime.now(timezone.utc).isoformat(),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
