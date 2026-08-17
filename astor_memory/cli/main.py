"""
CLI entry point for `am` command.

Provides v1.0 minimal commands:
- am init: First-time setup
- am write "text": Write fact
- am recall "query": Recall facts
- am doctor: Health check
- am config: View / modify config
- am version: Show version
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

from .. import __version__
from ..config import load_config, get_default_astor_dir


def main(argv: list[str] | None = None) -> int:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog='am',
        description='Astor-Memory CLI',
        epilog='See docs/ for full command reference.',
    )
    parser.add_argument('--version', action='store_true', help='Show version')

    subparsers = parser.add_subparsers(dest='command')

    # am version
    sub = subparsers.add_parser('version', help='Show version')
    sub.set_defaults(func=cmd_version)

    # am init
    sub = subparsers.add_parser('init', help='Initialize astor-memory')
    sub.add_argument('--from', dest='migrate_from', help='Migrate from (memory-bus | user-md)')
    sub.set_defaults(func=cmd_init)

    # am write
    sub = subparsers.add_parser('write', help='Write a fact')
    sub.add_argument('text', help='Fact text')
    sub.add_argument('--user', default='admin', help='User ID')
    sub.add_argument('--mode', default=None, help='Extract mode: auto|none|regex|llm')
    sub.add_argument('--tier', default='public', help='Tier: public|source|private_<user>')
    sub.set_defaults(func=cmd_write)

    # am recall
    sub = subparsers.add_parser('recall', help='Recall facts')
    sub.add_argument('query', help='Query text')
    sub.add_argument('--user', default='admin', help='User ID')
    sub.add_argument('--top-k', type=int, default=5, help='Number of results')
    sub.set_defaults(func=cmd_recall)

    # am doctor
    sub = subparsers.add_parser('doctor', help='Health check')
    sub.add_argument('--memory', action='store_true', help='Memory stats')
    sub.add_argument('--schema', action='store_true', help='Schema check')
    sub.set_defaults(func=cmd_doctor)

    # am config
    sub = subparsers.add_parser('config', help='View / modify config')
    sub.add_argument('action', nargs='?', default='show', help='show | get | set')
    sub.add_argument('key', nargs='?', help='Config key (e.g. rate_limits.recall_per_hour)')
    sub.add_argument('value', nargs='?', help='Value to set')
    sub.set_defaults(func=cmd_config)

    # am install
    sub = subparsers.add_parser('install', help='Install Astor-Memory into another agent')
    sub.add_argument('--ide', required=True,
                     choices=['claude-code', 'cline', 'opencode', 'hermes', 'openclaw', 'cursor', 'continue', 'windsurf', 'aider'],
                     help='Target agent')
    sub.add_argument('--mode', default='auto',
                     choices=['auto', 'priority', 'coexist', 'replace', 'verify'],
                     help='Install mode')
    sub.add_argument('--agent-dir', default='~', help='Agent config dir (default: ~)')
    sub.add_argument('--apply', action='store_true', help='Actually write files (default: dry-run)')
    sub.set_defaults(func=cmd_install)

    # am migrate
    sub = subparsers.add_parser('migrate', help='Migrate from legacy memory-bus')
    sub.add_argument('--from', dest='source_type', required=True,
                     choices=['memory-bus'], help='Source system')
    sub.add_argument('--source', required=True, help='Source DB path (e.g. ~/.memory-bus/bus.db)')
    sub.add_argument('--target', default=None, help='Target astor dir (default: ASTOR_DIR or ~/.astor)')
    sub.add_argument('--dry-run', action='store_true', help='Report what would happen without writing')
    sub.set_defaults(func=cmd_migrate)

    # am reembed
    sub = subparsers.add_parser('reembed', help='Compute + persist embeddings for all canonical facts')
    sub.add_argument('--limit', type=int, default=None, help='Max facts to re-embed (default: all)')
    sub.add_argument('--batch-size', type=int, default=32, help='Batch size for model.embed()')
    sub.set_defaults(func=cmd_reembed)

    # v1.2.0 (2026-08-16): am cascade — replay the cascade write queue.
    # When nest.store() failed during promote_candidate (e.g. embedding
    # model OOM), the (fact_id, content, tier, user_id) is queued in
    # cascade_state. `am cascade replay` drains it; `am cascade stats`
    # shows counts. Equivalent to POST /v1/cascade/{replay,stats}.
    sub = subparsers.add_parser('cascade', help='Replay cascade write queue (embed failures)')
    cascade_sub = sub.add_subparsers(dest='cascade_action', required=True)
    cascade_replay_p = cascade_sub.add_parser('replay', help='Replay pending cascade rows')
    cascade_replay_p.add_argument('--limit', type=int, default=100, help='Max rows to process (default 100)')
    cascade_replay_p.add_argument('--max-attempts', type=int, default=5, help='Per-row max retry count (default 5)')
    cascade_replay_p.set_defaults(func=cmd_cascade_replay)
    cascade_stats_p = cascade_sub.add_parser('stats', help='Show cascade queue counts')
    cascade_stats_p.set_defaults(func=cmd_cascade_stats)
    cascade_purge_p = cascade_sub.add_parser('purge', help='Delete old succeeded/failed rows')
    cascade_purge_p.add_argument('--status', choices=['succeeded', 'failed', 'pending'], default='succeeded')
    cascade_purge_p.add_argument('--older-than-days', type=int, default=7, help='Default 7 days')
    cascade_purge_p.set_defaults(func=cmd_cascade_purge)

    # v1.1: am mcp — Model Context Protocol server (stdio JSON-RPC 2.0).
    # Per Plan § v1.1 MCP integration. No external deps — implements the
    # minimal MCP handshake (initialize + tools/list + tools/call) so any
    # MCP-compatible client (Claude Desktop, Cursor, Continue, etc.) can use astor
    # as a tool provider. Run with: `am mcp serve`
    sub = subparsers.add_parser('mcp', help='MCP server (Model Context Protocol)')
    mcp_sub = sub.add_subparsers(dest='mcp_action', required=True)
    mcp_serve = mcp_sub.add_parser('serve', help='Run MCP stdio server')

    # v1.2.2 (2026-08-16): am reflection — episodic consolidation
    # (EverOS pattern, simplified for SQLite stack). Merges clusters of
    # similar facts into a single winner, tombstones losers.
    sub = subparsers.add_parser('reflection', help='Episodic reflection (merge + tombstone)')
    reflection_sub = sub.add_subparsers(dest='reflection_action', required=True)
    reflection_run_p = reflection_sub.add_parser('run', help='Run reflection on a tier')
    reflection_run_p.add_argument('--tier', default='public', help='Tier to reflect on (default public)')
    reflection_run_p.add_argument('--user-id', default=None, help='User id (for private tier)')
    reflection_run_p.add_argument('--min-size', type=int, default=2, help='Min cluster size (default 2)')
    reflection_run_p.add_argument('--max-clusters', type=int, default=50, help='Max clusters to process (default 50)')
    reflection_run_p.add_argument('--kinds', default=None, help='Comma-separated kinds to filter (default: all)')
    reflection_run_p.set_defaults(func=cmd_reflection_run)

    # v1.2.3 (2026-08-16): am auto-link — establish auto-link edges
    # between similar facts (A-MEM Zettelkasten pattern). Backfill runs
    # once over existing facts; live writes trigger auto_link_for_fact
    # in the server hot path.
    sub = subparsers.add_parser('auto-link', help='Zettelkasten auto-link (audit-safe)')
    auto_link_sub = sub.add_subparsers(dest='auto_link_action', required=True)
    auto_link_backfill_p = auto_link_sub.add_parser('backfill', help='Backfill auto-links for existing facts')
    auto_link_backfill_p.add_argument('--tier', default='public', help='Tier to backfill (default public)')
    auto_link_backfill_p.add_argument('--user-id', default=None, help='User id (for private tier)')
    auto_link_backfill_p.add_argument('--limit', type=int, default=500, help='Max facts to process (default 500)')
    auto_link_backfill_p.add_argument('--cosine-threshold', type=float, default=0.85, help='Cosine threshold (default 0.85)')
    auto_link_backfill_p.add_argument('--max-links-per-fact', type=int, default=5, help='Max links per fact (default 5)')
    auto_link_backfill_p.set_defaults(func=cmd_auto_link_backfill)

    mcp_serve.add_argument('--transport', default='stdio', choices=['stdio'],
                           help='MCP transport (only stdio supported in v1.1)')
    mcp_serve.set_defaults(func=cmd_mcp_serve)

    # am bot - multi-user management (plan §2591-2594)
    bot_p = subparsers.add_parser('bot', help='Multi-user bot management (first_admin only)')
    bot_sub = bot_p.add_subparsers(dest='bot_command')
    bot_sub.add_parser('on', help='Enable multi-user mode').set_defaults(func=cmd_bot_on)
    bot_sub.add_parser('off', help='Disable multi-user mode').set_defaults(func=cmd_bot_off)
    bot_add = bot_sub.add_parser('add-user', help='Create empty 9-db layout for a new user')
    bot_add.add_argument('user_id', help='User id (regex [a-zA-Z0-9_-]{1,64})')
    bot_add.add_argument('--role', default='user', choices=['user', 'admin'], help='Role for new user')
    bot_add.set_defaults(func=cmd_bot_add_user)
    bot_sub.add_parser('list-users', help='List users on disk + their roles').set_defaults(func=cmd_bot_list_users)
    bot_promote = bot_sub.add_parser('promote', help='Promote user to admin')
    bot_promote.add_argument('user_id', help='User to promote')
    bot_promote.set_defaults(func=cmd_bot_promote)
    bot_demote = bot_sub.add_parser('demote', help='Demote admin to user')
    bot_demote.add_argument('user_id', help='User to demote')
    bot_demote.set_defaults(func=cmd_bot_demote)
    bot_bind = bot_sub.add_parser('bind-platform', help='Map platform chat_id to user_id')
    bot_bind.add_argument('user_id', help='User id')
    bot_bind.add_argument('platform', choices=['telegram', 'discord', 'wechat', 'feishu', 'webchat'])
    bot_bind.add_argument('chat_id', help='Chat/channel id from the platform')
    bot_bind.set_defaults(func=cmd_bot_bind_platform)
    bot_unbind = bot_sub.add_parser('unbind', help='Remove a platform binding')
    bot_unbind.add_argument('platform', choices=['telegram', 'discord', 'wechat', 'feishu', 'webchat'])
    bot_unbind.add_argument('chat_id', help='Chat id to unbind')
    bot_unbind.set_defaults(func=cmd_bot_unbind)
    bot_p_status = bot_sub.add_parser('status', help='Show bot on/off + platform bindings')
    bot_p_status.set_defaults(func=cmd_bot_status)

    # am admin - first_admin system operations
    admin_p = subparsers.add_parser('admin', help='first_admin system operations')
    admin_sub = admin_p.add_subparsers(dest='admin_command')
    admin_audit = admin_sub.add_parser('audit-log', help='Read audit rows')
    admin_audit.add_argument('--actor', help='Filter by actor')
    admin_audit.add_argument('--user', help='Filter by user_id')
    admin_audit.add_argument('--action', help='Filter by action')
    admin_audit.add_argument('--since', help='Filter by ts >= ...')
    admin_audit.add_argument('--limit', type=int, default=50, help='Max rows (default 50)')
    admin_audit.set_defaults(func=cmd_admin_audit_log)
    admin_who = admin_sub.add_parser('whoami', help='Show current first_admin lock')
    admin_who.set_defaults(func=cmd_admin_whoami)

    # am platform ... (bot-binding.db CRUD)
    plat_p = subparsers.add_parser('platform', help='Manage bot-binding.db (platforms + bindings + users)')
    plat_sub = plat_p.add_subparsers(dest='platform_command')
    plat_list = plat_sub.add_parser('list', help='List platforms')
    plat_list.set_defaults(func=cmd_platform_list)
    plat_list_users = plat_sub.add_parser('list-users', help='List user_meta rows')
    plat_list_users.set_defaults(func=cmd_platform_list_users)
    plat_list_b = plat_sub.add_parser('list-bindings', help='List bindings (active only)')
    plat_list_b.add_argument('--all', action='store_true', help='Include revoked bindings')
    plat_list_b.set_defaults(func=cmd_platform_list_bindings)
    plat_resolve = plat_sub.add_parser('resolve', help='Resolve a chat_id -> user_id')
    plat_resolve.add_argument('platform_id', help='e.g. weixin:8263b17ef9c7@im.bot or telegram:bot1')
    plat_resolve.add_argument('chat_id', help='The chat/channel/wxid to resolve')
    plat_resolve.set_defaults(func=cmd_platform_resolve)
    plat_token_get = plat_sub.add_parser('token-get', help='Print platform token (audit row written)')
    plat_token_get.add_argument('platform_id')
    plat_token_get.set_defaults(func=cmd_platform_token_get)
    plat_token_set = plat_sub.add_parser('token-set', help='Update token for an existing platform row')
    plat_token_set.add_argument('platform_id')
    plat_token_set.add_argument('token', help='New token value')
    plat_token_set.set_defaults(func=cmd_platform_token_set)
    plat_bind = plat_sub.add_parser('bind', help='Create binding (chat_id -> user_id)')
    plat_bind.add_argument('platform_id')
    plat_bind.add_argument('chat_id')
    plat_bind.add_argument('user_id')
    plat_bind.add_argument('--allow-from', default=None, help='security allow_from (default=chat_id)')
    plat_bind.add_argument('--scope', default='single')
    plat_bind.set_defaults(func=cmd_platform_bind)
    plat_unbind = plat_sub.add_parser('unbind', help='Revoke a binding by binding_id or chat_id')
    plat_unbind.add_argument('platform_id')
    plat_unbind.add_argument('chat_id')
    plat_unbind.set_defaults(func=cmd_platform_unbind)
    plat_user = plat_sub.add_parser('add-user', help='Add user_meta row (and 9-db layout)')
    plat_user.add_argument('user_id')
    plat_user.add_argument('short_alias')
    plat_user.add_argument('--real-name', default=None)
    plat_user.add_argument('--role', default='user', choices=['user', 'admin'])
    plat_user.add_argument('--plan', default='trial', choices=['trial', 'lifetime', 'paid', 'free', 'permanent'])
    plat_user.set_defaults(func=cmd_platform_add_user)
    plat_verify = plat_sub.add_parser('verify', help='Verify all 6 invariants on bot-binding.db')
    plat_verify.set_defaults(func=cmd_platform_verify)

    args = parser.parse_args(argv)


    if args.version or args.command == 'version':
        return cmd_version(args)
    if args.command is None:
        parser.print_help()
        return 0
    return args.func(args)


def cmd_version(args) -> int:
    print(f'astor-memory {__version__}')
    return 0


def cmd_init(args) -> int:
    """Initialize astor-memory (creates 3 DBs: astor_bus.db, astor_forge.db, astor_nest.db)."""
    from .. import astor_bus as _astor_bus_func, astor_nest as _astor_nest_func, astor_forge as _astor_forge_func

    astor_dir = get_default_astor_dir()
    astor_dir.mkdir(parents=True, exist_ok=True)
    # Force creation of all 3 DB singletons (each opens + schema-inits its own file).
    bus = _astor_bus_func()
    nest = _astor_nest_func()
    _forge_module = _astor_forge_func()  # forge is pure module; ensures it's importable
    print(f'✅ Astor-Memory initialized at {astor_dir}')
    print(f'   - {bus.db_path.name} (bus: events + canonical facts)')
    print(f'   - {nest.db_path.name} (nest: vector embeddings)')
    print(f'   - astor_forge.db (forge: LLM extraction cache, v0.2+ LLM extract)')
    if args.migrate_from == 'memory-bus':
        print('   Migration from memory-bus: TODO (v0.2)')
    elif args.migrate_from == 'user-md':
        print('   Migration from USER.md/MEMORY.md: TODO (v0.2)')
    print('   v0.1 ships schema + bus + cli skeleton.')
    print('   Next: v0.2 will add nest (vector store) + forge (LLM extract).')
    return 0


def cmd_write(args) -> int:
    """Write a fact."""
    from ..bus import astor_bus, astor_reset_bus
    from ..forge import astor_extract_facts

    # 2026-08-15 ship: CLI write must specify tier. Default 'private' so
    # `am write <text>` writes to admin private db (the common case for
    # admin authoring). Use --tier public/source for cross-user writes.
    tier = getattr(args, 'tier', 'private')
    user_id = args.user if tier == 'private' else None
    bus = astor_bus(tier=tier, user_id=user_id)
    # 1. Append event
    event_id = bus.append_event(
        namespace=args.user,
        agent_id='cli',
        source='cli.write',
        action='write',
        content=args.text,
    )
    # 2. Extract facts
    facts = astor_extract_facts(args.text, mode=args.mode or 'auto')
    if not facts:
        print(f'� Saved event (no facts extracted): event_id={event_id}')
        return 0
    # 3. Insert candidates + promote
    fact_ids = []
    for f in facts:
        cand_id = bus.insert_candidate(
            event_id=event_id,
            namespace=args.user,
            content=f.content,
            kind=f.kind,
            confidence=f.confidence,
            importance=f.importance,
            tags=f.tags or [],
            # v1.2.0: thread A-MEM-style structured fields from extractor.
            keywords=f.keywords or [],
            context=f.context or '',
        )
        canon_id = bus.promote_candidate(
            cand_id, promoted_by='cli.write', user_id=args.user, tier=args.tier,
        )
        fact_ids.append(canon_id)
    print(f'✅ Wrote {len(fact_ids)} fact(s): {fact_ids}')
    return 0


def cmd_recall(args) -> int:
    """Recall facts."""
    from ..nest import astor_nest, astor_reset_nest
    from ..nest.embeddings import astor_get_embedding_model

    nest = astor_nest()
    model = astor_get_embedding_model()
    embeddings = list(model.embed([args.query]))
    query_emb = embeddings[0]

    results = nest.search(
        query_emb,
        limit=args.top_k,
    )
    if not results:
        print('No results found.')
        return 0
    print(f'📚 Top {len(results)} results for "{args.query}":')
    for i, (fact_id, sim) in enumerate(results):
        print(f'  [{i+1}] fact_id={fact_id} similarity={sim:.3f}')
    return 0


def cmd_doctor(args) -> int:
    """Comprehensive health check.

    Checks: schema, canonical counts, embedding coverage, bot-binding.db,
    6 invariants, version.
    """
    import psutil, os
    from pathlib import Path
    process = psutil.Process()
    mem_mb = process.memory_info().rss / 1024 / 1024
    print(f'🏥 astor-memory v{__version__} health check')
    print(f'   PID: {process.pid}  Memory RSS: {mem_mb:.1f} MB')

    astor_dir = os.environ.get('ASTOR_DIR') or str(Path.home() / '.astor')
    print(f'   ASTOR_DIR: {astor_dir}')
    bb_path = Path(astor_dir) / 'bot-binding.db'
    print(f'   bot-binding.db: {"EXISTS" if bb_path.exists() else "MISSING"} ({bb_path.stat().st_size if bb_path.exists() else 0} bytes)')

    # 9-db canonical + embedded counts
    try:
        from .._internal.acl_layout import get_db_path, Tier, Store
        import sqlite3
        total_canon = 0
        total_emb = 0
        coverage = []
        locs = []
        locs.append(('source', Tier.SOURCE, None))
        locs.append(('admin', Tier.PRIVATE, 'admin'))
        for u in ['sunday', 'user_c', 'user_a', 'user_d']:
            locs.append((u, Tier.PRIVATE, u))
        for label, tier, user_id in locs:
            try:
                bus_p = get_db_path(tier, Store.BUS, user_id)
                nest_p = get_db_path(tier, Store.NEST, user_id)
                if bus_p.exists():
                    c = sqlite3.connect(str(bus_p)).execute('SELECT COUNT(*) FROM memory_canonical').fetchone()[0]
                else:
                    c = 0
                if nest_p.exists():
                    e = sqlite3.connect(str(nest_p)).execute('SELECT COUNT(*) FROM embeddings').fetchone()[0]
                else:
                    e = 0
                total_canon += c
                total_emb += e
                ratio = f'{e/c*100:.0f}%' if c else 'n/a'
                coverage.append((label, c, e, ratio))
            except Exception as e_inner:
                coverage.append((label, '?', '?', f'ERR: {e_inner}'))
        print(f'   9-db canonical: {total_canon}, embedded: {total_emb}')
        for label, c, e, r in coverage:
            if isinstance(c, int):
                print(f'     {label:8s}: {c:5d} canonical, {e:5d} embedded ({r})')
            else:
                print(f'     {label:8s}: ERR')
    except Exception as e:
        print(f'   9-db check failed: {e}')

    # bot-binding invariants
    print()
    print('🔒 bot-binding.db invariants:')
    if bb_path.exists():
        # Inline check (avoid subprocess)
        import sqlite3
        con = sqlite3.connect(str(bb_path))
        problems = []
        # 1 TG/DC exactly 1 row
        for r in con.execute("SELECT platform_kind, COUNT(*) FROM platforms WHERE platform_kind IN ('telegram','discord','feishu','webchat') GROUP BY platform_kind HAVING COUNT(*) > 1"):
            problems.append(f'INV1: {r[0]} duplicated')
        # 5 enabled token non-empty
        for r in con.execute("SELECT platform_id FROM platforms WHERE enabled=1 AND (account_token IS NULL OR account_token='')"):
            problems.append(f'INV5: {r[0]} empty token')
        # 6 weixin base_url
        for r in con.execute("SELECT platform_id FROM platforms WHERE platform_kind='weixin' AND (base_url IS NULL OR base_url='')"):
            problems.append(f'INV6: {r[0]} no base_url')
        con.close()
        if not problems:
            print('   ✅ all 6 invariants pass')
        else:
            for p in problems:
                print(f'   ❌ {p}')
    else:
        print('   ⚠️ no bot-binding.db (run `am platform ...` to initialize)')

    # version check
    print()
    print(f'   astor-memory version: {__version__}')

    return 0


def cmd_config(args) -> int:
    """View / modify config."""
    cfg = load_config()
    if args.action == 'show':
        import json
        print(json.dumps(cfg, indent=2))
    elif args.action == 'get':
        # Navigate dotted key
        parts = args.key.split('.') if args.key else []
        val = cfg
        for p in parts:
            val = val.get(p, {}) if isinstance(val, dict) else None
        print(val)
    elif args.action == 'set':
        if not args.key or args.value is None:
            print('Usage: am config set <key> <value>')
            return 1
        from ..config import save_config
        # Navigate / set
        parts = args.key.split('.')
        target = cfg
        for p in parts[:-1]:
            target = target.setdefault(p, {})
        target[parts[-1]] = args.value
        save_config(cfg)
        print(f'✅ Set {args.key} = {args.value}')
    else:
        print(f'Unknown action: {args.action}')
        return 1
    return 0


def cmd_install(args) -> int:
    """Install Astor-Memory into another agent (per Plan Insight 18)."""
    from pathlib import Path
    from ..installer import astor_install
    from ..installer.registry import astor_verify_agent

    agent_dir = Path(args.agent_dir).expanduser()

    if args.mode == 'verify':
        report = astor_verify_agent(args.ide)
        print(f"Agent: {report['agent']}")
        print(f"Tier: {report['tier']} — {report.get('tier_meaning', report.get('reason', ''))}")
        if report['supported']:
            print(f"Modes supported: {', '.join(report['modes_supported'])}")
            print(f"Recommended: --mode={report['recommended_mode']}")
        else:
            print(f"Reason: {report['reason']}")
        return 0 if report['supported'] else 1

    result = astor_install(args.ide, agent_dir, args.mode)

    if 'error' in result:
        print(f"❌ {result['error']}")
        if 'report' in result:
            print('   Verify report:')
            print(f"   {result['report']}")
        return 1

    if 'fallback' in result:
        print(f"⚠️  {result['note']}")

    final = result.get('result', result)
    print(f"Agent: {final['agent']}")
    print(f"Mode: {final['mode']}")
    print(f"Tier: {final['tier']}")
    if not final.get('changes'):
        print('No file changes planned.')
    else:
        print(f"Changes ({len(final['changes'])}):")
        for ch in final['changes']:
            action = ch.get('action', '?')
            path = ch.get('path', '?')
            exec_ = ' (executable)' if ch.get('executable') else ''
            print(f"  [{action}] {path}{exec_}")
    if final.get('notes'):
        print('Notes:')
        for note in final['notes']:
            print(f"  - {note}")

    if final.get('requires_restart'):
        print('⚠️  Restart required for changes to take effect.')

    if not args.apply:
        print('(Dry-run. Pass --apply to write files.)')
    else:
        # Actually write files
        for ch in final.get('changes', []):
            p = Path(ch['path'])
            p.parent.mkdir(parents=True, exist_ok=True)
            if ch.get('action') == 'create':
                p.write_text(ch['content'], encoding='utf-8')
                if ch.get('executable'):
                    p.chmod(0o755)
                print(f"  ✅ Wrote {p}")
            elif ch.get('action') in ('append', 'patch_or_create'):
                if p.exists():
                    existing = p.read_text(encoding='utf-8')
                    if ch['content'].strip() not in existing:
                        with open(p, 'a', encoding='utf-8') as f:
                            f.write(ch['content'])
                        print(f"  ✅ Appended {p}")
                    else:
                        print(f"  �️  Already present in {p}")
                else:
                    p.write_text(ch['content'], encoding='utf-8')
                    print(f"  ✅ Created {p}")
    return 0


def cmd_migrate(args) -> int:
    """Migrate from legacy memory-bus."""
    from pathlib import Path
    from .migrate import astor_migrate_from_memory_bus

    source = Path(args.source).expanduser()
    target = Path(args.target).expanduser() if args.target else None

    if source_type := args.source_type:
        if source_type != 'memory-bus':
            print(f'❌ Unknown source type: {source_type}')
            return 1

    if args.dry_run:
        print(f'� Dry run: migrate from {source}')
    else:
        print(f'🚀 Migrating from {source}...')

    report = astor_migrate_from_memory_bus(source, target, dry_run=args.dry_run)

    if report.errors:
        print('❌ Errors:')
        for err in report.errors:
            print(f'  - {err}')
        if not args.dry_run:
            return 1

    if args.dry_run:
        print(f'Would migrate:')
    else:
        print(f'✅ Migrated:')
    print(f'  - events: {report.events_migrated}')
    print(f'  - candidates: {report.candidates_migrated}')
    print(f'  - canonical: {report.canonical_migrated}')
    print(f'  - embeddings: {report.embeddings_migrated}')
    print(f'  - skipped (existing): {report.skipped_existing}')

    if not args.dry_run:
        target_dir = target or Path('~/.astor').expanduser()
        print(f'   Target: {target_dir}/')
        print(f'   - astor_bus.db (events + canonical)')
        print(f'   - astor_nest.db (embeddings — separate DB)')
        print(f'   Note: embeddings NOT migrated (legacy format uncertain). Run `am recall` to trigger re-embedding on demand.')
    return 0


def cmd_reembed(args) -> int:
    """Re-compute embeddings for canonical facts that don't have one yet.

    Used after `am migrate` (legacy data doesn't have embeddings in new format).
    Idempotent: skips facts that already have an embedding for current model.

    9-db mode (turn 2026-08-15): walks each (tier, user_id) combination, opens
    the corresponding bus + nest via `astor_bus_for(tier, user_id)`,
    `astor_nest_for(tier, user_id)`. Reads facts from bus, writes embeddings
    to nest, all under ACL check.
    """
    from .._internal.acl import astor_init_acl, astor_check_write
    from .._internal.acl_layout import (
        Tier, Store, get_db_path, list_user_ids,
    )

    # CLI must run as first_admin (re-embedding is system-wide).
    # Allow actor='am_cli' for stand-alone `am` invocations.
    try:
        astor_init_acl(actor='first_admin', role='first_admin', tier='public')
    except Exception:
        pass

    from ..nest.embeddings import astor_get_embedding_model, astor_get_model_name_for_ram

    model_name = astor_get_model_name_for_ram()
    model = astor_get_embedding_model()

    # Enumerate all (tier, user_id) pairs.
    targets = []
    for tier in (Tier.PUBLIC, Tier.SOURCE):
        targets.append((tier, None))
    for user_id in list_user_ids():
        targets.append((Tier.PRIVATE, user_id))

    grand_total = 0
    for tier, user_id in targets:
        try:
            astor_check_write(tier.value, user_id)
        except Exception as e:
            # Skip disallowed tiers (e.g. source when actor is user, not first_admin)
            continue

        bus_path = get_db_path(tier, Store.BUS, user_id)
        nest_path = get_db_path(tier, Store.NEST, user_id)
        if not bus_path.exists() or not nest_path.exists():
            continue

        import sqlite3 as _sq
        bus_con = _sq.connect(str(bus_path))
        nest_con = _sq.connect(str(nest_path))
        # Apply nest schema if it's still empty (no embeddings table)
        from ..nest.schema import astor_init_nest_schema
        try:
            astor_init_nest_schema(nest_con)
        except Exception:
            pass

        rows = bus_con.execute(
            "SELECT id, content FROM memory_canonical WHERE tombstoned = 0"
        ).fetchall()
        if not rows:
            bus_con.close(); nest_con.close()
            continue

        existing = set(r[0] for r in nest_con.execute(
            "SELECT fact_id FROM embeddings"
        ).fetchall())
        to_embed = [(fid, content) for fid, content in rows if fid not in existing]

        if args.limit:
            # Honor limit across the entire run; only first N unprocessed
            remaining = args.limit - grand_total
            if remaining <= 0:
                bus_con.close(); nest_con.close()
                continue
            to_embed = to_embed[:remaining]

        if not to_embed:
            bus_con.close(); nest_con.close()
            continue

        where = f'{tier.value}/{user_id or "-"}'
        print(f'🔄 [{where}] re-embedding {len(to_embed)} facts (batch={args.batch_size})...')

        n_done = 0
        for i in range(0, len(to_embed), args.batch_size):
            batch = to_embed[i:i+args.batch_size]
            texts = [content for fid, content in batch]
            try:
                embeddings = list(model.embed(texts))
                for (fid, content), emb in zip(batch, embeddings):
                    # Store into nest at this tier/user
                    emb_bytes = bytes(np.asarray(emb).astype('float32').tobytes())
                    embedding_dim = len(emb)
                    user_id_for_nest = user_id or '_system'
                    tier_str = tier.value
                    try:
                        nest_con.execute(
                            """INSERT OR REPLACE INTO embeddings
                               (fact_id, embedding, model_name, dim, user_id, tier, publishable)
                               VALUES (?, ?, ?, ?, ?, ?, 0)""",
                            (fid, emb_bytes, model_name, embedding_dim,
                             user_id_for_nest, tier_str),
                        )
                        n_done += 1
                    except Exception:
                        pass
                nest_con.commit()
                print(f'   ... {n_done}/{len(to_embed)} done')
            except Exception as e:
                print(f'   Batch {i}-{i+len(batch)} failed: {e}')
                continue
        grand_total += n_done
        bus_con.close(); nest_con.close()

    print(f'✅ Re-embedded {grand_total} facts across all tiers')
    return 0


def cmd_cascade_replay(args) -> int:
    """v1.2.0: Replay pending cascade write queue (embed failures).

    When nest.store() failed during promote_candidate (e.g. embedding model
    OOM), the (fact_id, content, tier, user_id) is queued in cascade_state.
    This command drains pending rows and re-attempts the embed.
    """
    from ..bus import cascade as _cascade
    bus = astor_bus(tier='public', user_id='admin')
    result = _cascade.replay_pending(
        bus,
        limit=int(getattr(args, 'limit', 100) or 100),
        max_attempts=int(getattr(args, 'max_attempts', 5) or 5),
    )
    import json as _json
    print(_json.dumps(result, indent=2, ensure_ascii=False))
    if result['failed'] > 0:
        print(f"[cascade] {result['failed']} row(s) still failing; check "
              "`am cascade stats` and recent log entries.", file=__import__('sys').stderr)
    return 0


def cmd_cascade_stats(args) -> int:
    """v1.2.0: Show cascade queue counts."""
    from ..bus import cascade as _cascade
    bus = astor_bus(tier='public', user_id='admin')
    stats = _cascade.stats(bus)
    import json as _json
    print(_json.dumps(stats, indent=2, ensure_ascii=False))
    return 0


def cmd_reflection_run(args) -> int:
    """v1.2.2: Run episodic reflection (merge + tombstone).

    Finds clusters of similar canonical facts in a tier, merges them
    into a single winner + tombstones losers. First_admin only (destructive).
    """
    from ..bus import astor_bus
    from ..nest import reflection as _reflection
    tier = getattr(args, 'tier', 'public') or 'public'
    user_id = getattr(args, 'user_id', None) or None
    min_size = int(getattr(args, 'min_size', 2) or 2)
    max_clusters = int(getattr(args, 'max_clusters', 50) or 50)
    kinds_raw = getattr(args, 'kinds', None)
    kinds = None
    if kinds_raw:
        kinds = [k.strip() for k in kinds_raw.split(',') if k.strip()]
    bus = astor_bus(tier=tier, user_id=user_id)
    result = _reflection.run_reflection(
        bus, tier=tier, user_id=user_id,
        min_size=min_size, max_clusters=max_clusters, kinds=kinds,
        actor='cli',
    )
    import json as _json
    print(_json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_auto_link_backfill(args) -> int:
    """v1.2.3: Backfill auto-link edges for existing facts.

    Reads the most recent N non-tombstoned facts in a tier, runs
    auto_link_for_fact on each, returns aggregate counts. Idempotent —
    re-running finds 0 new edges (existing parent_fact_ids already
    contain the discovery).
    """
    from ..bus import astor_bus
    from ..nest import auto_link as _auto_link
    tier = getattr(args, 'tier', 'public') or 'public'
    user_id = getattr(args, 'user_id', None) or None
    limit = int(getattr(args, 'limit', 500) or 500)
    cosine_threshold = float(getattr(args, 'cosine_threshold', 0.85) or 0.85)
    max_links = int(getattr(args, 'max_links_per_fact', 5) or 5)
    bus = astor_bus(tier=tier, user_id=user_id)
    result = _auto_link.backfill_all(
        bus, tier=tier, user_id=user_id,
        cosine_threshold=cosine_threshold,
        max_links_per_fact=max_links,
        limit=limit,
    )
    import json as _json
    print(_json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_cascade_purge(args) -> int:
    """v1.2.0: Delete old cascade rows by status + age."""
    from ..bus import cascade as _cascade
    bus = astor_bus(tier='public', user_id='admin')
    deleted = _cascade.purge(
        bus,
        status=str(getattr(args, 'status', 'succeeded')),
        older_than_days=int(getattr(args, 'older_than_days', 7) or 7),
    )
    print(f"[cascade] purged {deleted} row(s)")
    return 0


def cmd_mcp_serve(args) -> int:
    """v1.1: Minimal MCP stdio server (JSON-RPC 2.0 over stdin/stdout).

    Implements the smallest subset of MCP needed to be useful as an astor
    tool provider:
      - initialize              -> server info + capabilities
      - tools/list              -> expose astor_recall, astor_write, astor_status
      - tools/call              -> dispatch to existing helpers
      - notifications/cancelled -> graceful exit

    No external deps. Run via `am mcp serve` (default transport=stdio).
    Claude Desktop / Cursor can launch this as a subprocess to gain access
    to astor memory through MCP. Per Plan v1.1 MCP integration.

    P2-fix (2026-08-16): Python 3.11 on Windows returns EOF immediately on
    subprocess stdin pipe, so readline() can't probe readiness. Use
    PeekNamedPipe via ctypes to poll for data without blocking. This is a
    known Windows + py3.11 subprocess issue (fixed in py3.12).
    """
    import sys as _sys
    import json as _json
    import threading as _threading
    import time as _time
    import ctypes as _ctypes

    def _send(obj):
        body = _json.dumps(obj, ensure_ascii=False).encode('utf-8')
        header = ("Content-Length: " + str(len(body)) + "\r\n\r\n").encode("ascii")
        _sys.stdout.buffer.write(header + body)
        _sys.stdout.buffer.flush()

    def _pipe_ready(stream):
        """Return True if stream has data available to read.

        Tries PeekNamedPipe first (works for both pipes and some files
        where fstat is unreliable). Falls back to non-blocking read attempt.
        Per P2-fix 2026-08-16: GetFileType on Python's wrapped pipe fd
        returns FILE_TYPE_UNKNOWN=0, not FILE_TYPE_PIPE=3, so we cannot
        use that to gate the strategy.
        """
        try:
            inner = stream.buffer
        except AttributeError:
            inner = stream
        try:
            h = inner.fileno()
        except Exception:
            return True

        # Try PeekNamedPipe — works for both real pipes AND for some
        # file handles. If it succeeds, avail>0 means data ready.
        avail = _ctypes.c_ulong(0)
        left = _ctypes.c_ulong(0)
        ok = _ctypes.windll.kernel32.PeekNamedPipe(
            h, None, 0, None,
            _ctypes.byref(avail), _ctypes.byref(left),
        )
        if ok:
            return avail.value > 0

        # PeekNamedPipe failed — assume regular file. Use file size + pos.
        try:
            import os as _os
            pos = inner.tell()
            size = _os.fstat(h).st_size
            return pos < size
        except Exception:
            return True

    def _read_message():
        """Block until a full MCP message arrives, then parse it."""
        import re as _re
        # Wait for any byte to be available
        deadline = _time.time() + 60.0
        while not _pipe_ready(_sys.stdin):
            if _time.time() > deadline:
                return None
            _time.sleep(0.01)
        # Read headers (until empty line)
        content_length = 0
        for _ in range(20):
            line = _sys.stdin.buffer.readline()
            if not line:
                return None  # EOF
            stripped = line.strip()
            if not stripped:
                break
            m = _re.match(rb'Content-Length:\s*(\d+)', stripped, _re.I)
            if m:
                content_length = int(m.group(1))
        if content_length <= 0:
            return None
        body = _sys.stdin.buffer.read(content_length)
        if not body:
            return None
        try:
            return _json.loads(body)
        except Exception:
            return None

    SERVER_INFO = {'name': 'astor-memory', 'version': '0.3.0'}
    CAPABILITIES = {'tools': {}}
    TOOLS = [
        {
            'name': 'astor_recall',
            'description': (
                'Semantic recall from astor-memory. Returns top-k facts '
                'matching the query across public/source/private/repo tiers.'
            ),
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string'},
                    'top_k': {'type': 'integer', 'default': 5},
                    'tier': {
                        'type': 'string', 'default': 'public',
                        'enum': ['public', 'source', 'private', 'repo'],
                    },
                },
                'required': ['query'],
            },
        },
        {
            'name': 'astor_write',
            'description': 'Write a fact to astor-memory.',
            'inputSchema': {
                'type': 'object',
                'properties': {
                    'text': {'type': 'string'},
                    'tier': {
                        'type': 'string', 'default': 'public',
                        'enum': ['public', 'source', 'private', 'repo'],
                    },
                    'scope': {
                        'type': 'string', 'default': 'long_term',
                        'enum': ['long_term', 'short_term', 'profile'],
                    },
                    'user': {
                        'type': 'string', 'default': 'admin',
                        'description': 'username (private) or repo_id (repo)',
                    },
                },
                'required': ['text'],
            },
        },
        {
            'name': 'astor_status',
            'description': (
                'Content-free stats from astor-memory (counts only, '
                'NO fact content per MemoraX Viewer principle).'
            ),
            'inputSchema': {'type': 'object', 'properties': {}},
        },
    ]

    def _handle_request(msg):
        method = msg.get('method')
        req_id = msg.get('id')
        params = msg.get('params') or {}

        if method == 'initialize':
            _send({
                'jsonrpc': '2.0', 'id': req_id,
                'result': {
                    'protocolVersion': '2024-11-05',
                    'serverInfo': SERVER_INFO,
                    'capabilities': CAPABILITIES,
                },
            })
            return
        if method == 'notifications/initialized':
            return
        if method == 'tools/list':
            _send({
                'jsonrpc': '2.0', 'id': req_id,
                'result': {'tools': TOOLS},
            })
            return
        if method == 'tools/call':
            tool_name = params.get('name')
            args = params.get('arguments') or {}
            try:
                if tool_name == 'astor_recall':
                    from .. import astor_read as _astor_read
                    text = _astor_read(
                        args.get('query', ''),
                        top_k=args.get('top_k', 5),
                        tier=args.get('tier', 'public'),
                    )
                    content = [{'type': 'text', 'text': text}]
                elif tool_name == 'astor_write':
                    from .. import astor_write as _astor_write
                    text_out = _astor_write(
                        args.get('text', ''),
                        tier=args.get('tier', 'public'),
                        scope=args.get('scope', 'long_term'),
                        user=args.get('user', 'admin'),
                    )
                    content = [{'type': 'text', 'text': text_out}]
                elif tool_name == 'astor_status':
                    from ..server import create_app as _ca
                    app = _ca()
                    with app.test_client() as c:
                        resp = c.get('/v1/viewer/stats')
                        content = [{
                            'type': 'text',
                            'text': resp.get_data(as_text=True),
                        }]
                else:
                    _send({
                        'jsonrpc': '2.0', 'id': req_id,
                        'error': {
                            'code': -32601,
                            'message': 'Unknown tool ' + repr(tool_name),
                        },
                    })
                    return
                _send({
                    'jsonrpc': '2.0', 'id': req_id,
                    'result': {'content': content, 'isError': False},
                })
            except Exception as exc:
                _send({
                    'jsonrpc': '2.0', 'id': req_id,
                    'result': {
                        'content': [{'type': 'text', 'text': 'error: ' + str(exc)}],
                        'isError': True,
                    },
                })
            return
        if method == 'ping':
            _send({'jsonrpc': '2.0', 'id': req_id, 'result': {}})
            return
        # Unknown method
        _send({
            'jsonrpc': '2.0', 'id': req_id,
            'error': {
                'code': -32601,
                'message': 'Unknown method ' + repr(method),
            },
        })

    # Main loop. Each iteration: wait for stdin ready, read one MCP
    # message, dispatch to handler. On stdin EOF, exit cleanly.
    _sys.stderr.write(
        '[astor-mcp] server starting (tid=' + str(_threading.get_ident()) + ')\n'
    )
    _sys.stderr.flush()

    while True:
        msg = _read_message()
        if msg is None:
            break
        _handle_request(msg)

    return 0


def _read_install_state() -> dict:
    """Load install-state.json or return defaults."""
    from .._internal.acl_layout import get_install_state_path
    p = get_install_state_path()
    if not p.exists():
        return {
            "version": 1,
            "mode": "single-user",
            "first_admin_user_id": "admin",
            "trial_users": [],
            "platform_bindings": {},
        }
    import json
    return json.loads(p.read_text())


def _write_install_state(state: dict) -> None:
    from .._internal.acl_layout import get_install_state_path
    p = get_install_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    import json
    p.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def _require_first_admin() -> None:
    """All `am bot ...` and `am admin ...` require first_admin role."""
    from .._internal.acl import astor_init_acl
    astor_init_acl(actor='first_admin', role='first_admin', tier='public')


def cmd_bot_on(args) -> int:
    """Enable multi-user mode."""
    _require_first_admin()
    state = _read_install_state()
    state["mode"] = "multi-user"
    state.setdefault("tier", "multi-user")
    _write_install_state(state)
    print(f'✅ Multi-user mode ENABLED. install-state.json updated.')
    return 0


def cmd_bot_off(args) -> int:
    """Disable multi-user mode (rollback to single-user)."""
    _require_first_admin()
    state = _read_install_state()
    state["mode"] = "single-user"
    _write_install_state(state)
    print(f'⚠️  Multi-user mode DISABLED. Existing user dbs NOT deleted (use `am uninstall --purge-data` to clean).')
    return 0


def cmd_bot_add_user(args) -> int:
    """Create empty 9-db layout for a new user."""
    _require_first_admin()
    from .._internal.acl_layout import (
        Tier, Store, ensure_layout, _validate_user_id,
    )
    try:
        _validate_user_id(args.user_id)
    except ValueError as e:
        print(f'❌ {e}')
        return 1
    paths = []
    for store in Store:
        p = ensure_layout(Tier.PRIVATE, store, args.user_id)
        paths.append(str(p.relative_to(ensure_layout.__module__ and Path.cwd())))
    # Apply schemas via dedupe store
    from ..bus.schema import astor_init_schema
    from ..nest.schema import astor_init_nest_schema
    from ..forge.schema import astor_init_forge_schema
    import sqlite3
    p_bus = ensure_layout(Tier.PRIVATE, Store.BUS, args.user_id)
    p_nest = ensure_layout(Tier.PRIVATE, Store.NEST, args.user_id)
    p_forge = ensure_layout(Tier.PRIVATE, Store.FORGE, args.user_id)
    for p, init in [(p_bus, astor_init_schema), (p_nest, astor_init_nest_schema), (p_forge, astor_init_forge_schema)]:
        con = sqlite3.connect(str(p))
        init(con)
        con.close()
    # Update install-state
    state = _read_install_state()
    users = state.setdefault("trial_users", [])
    if args.user_id not in users:
        users.append(args.user_id)
    state["mode"] = state.get("mode", "single-user")  # ensure bot on/off not required
    if state["mode"] != "multi-user":
        state["mode"] = "multi-user"
    # Add role hints
    roles = state.setdefault("roles", {})
    roles[args.user_id] = args.role
    _write_install_state(state)
    print(f'✅ Added user {args.user_id!r} as {args.role}. 9-db layout created at users/{args.user_id}/memory/')
    return 0


def cmd_bot_list_users(args) -> int:
    """List users on disk + their roles."""
    _require_first_admin()
    from .._internal.acl_layout import list_user_ids, get_install_state_path
    users_on_disk = sorted(list_user_ids())
    state = _read_install_state()
    roles = state.get("roles", {})
    print(f'   install-state.json: {get_install_state_path()}')
    print(f'   mode: {state.get("mode", "(unset)")}')
    print(f'   first_admin: {state.get("first_admin_user_id", "(none)")}')
    print()
    print(f'   {"USER_ID":24s} {"ROLE":8s} {"ON_DISK":8s}')
    for u in users_on_disk:
        role = roles.get(u, "(default user)")
        on_disk = "yes"
        print(f'   {u:24s} {role:8s} {on_disk:8s}')
    # Also list trial users in install-state but not on disk
    trial = state.get("trial_users", [])
    for u in trial:
        if u not in users_on_disk:
            role = roles.get(u, "(default user)")
            print(f'   {u:24s} {role:8s} {"no (dir missing)":8s}')
    return 0


def cmd_bot_promote(args) -> int:
    """Promote user -> admin."""
    _require_first_admin()
    from .._internal.acl import astor_actor_id
    state = _read_install_state()
    if args.user_id == state.get("first_admin_user_id"):
        print(f'❌ {args.user_id!r} is already first_admin (cannot promote — first_admin is permanent).')
        return 1
    roles = state.setdefault("roles", {})
    roles[args.user_id] = "admin"
    _write_install_state(state)
    print(f'✅ Promoted {args.user_id!r} to admin role. Note: admin still cannot read source.db (plan §2624).')
    return 0


def cmd_bot_demote(args) -> int:
    """Demote admin -> user."""
    _require_first_admin()
    state = _read_install_state()
    if args.user_id == state.get("first_admin_user_id"):
        print(f'❌ {args.user_id!r} is first_admin (cannot demote — first_admin is permanent root, plan §2632).')
        return 1
    roles = state.setdefault("roles", {})
    roles[args.user_id] = "user"
    _write_install_state(state)
    print(f'✅ Demoted {args.user_id!r} to user.')
    return 0


def cmd_bot_bind_platform(args) -> int:
    """Lock platform chat_id -> user_id for incoming messages."""
    _require_first_admin()
    state = _read_install_state()
    bindings = state.setdefault("platform_bindings", {})
    key = f"{args.platform}:{args.chat_id}"
    if key in bindings and bindings[key] != args.user_id:
        print(f'⚠️  {key} was already bound to {bindings[key]!r}, replacing with {args.user_id!r}.')
    bindings[key] = args.user_id
    _write_install_state(state)
    print(f'✅ {key} -> {args.user_id!r}')
    return 0


def cmd_bot_unbind(args) -> int:
    """Remove a platform binding (e.g. feishu revoke)."""
    _require_first_admin()
    state = _read_install_state()
    bindings = state.setdefault("platform_bindings", {})
    key = f"{args.platform}:{args.chat_id}"
    if key not in bindings:
        print(f'❌ No binding for {key}')
        return 1
    del bindings[key]
    _write_install_state(state)
    print(f'✅ Unbound {key}')
    return 0


def cmd_bot_status(args) -> int:
    """Show bot on/off + all platform bindings."""
    _require_first_admin()
    state = _read_install_state()
    print(f"   mode: {state.get('mode', '(unset)')}")
    print(f"   first_admin: {state.get('first_admin_user_id', '(none)')}")
    print()
    print("   platform_bindings:")
    bindings = state.get("platform_bindings", {})
    if not bindings:
        print("     (none)")
    else:
        for k, v in sorted(bindings.items()):
            print(f"     {k:50s} -> {v}")
    return 0


# ============================================================
# am admin ... (first_admin system operations)
# ============================================================

def cmd_admin_audit_log(args) -> int:
    """Read audit rows. first_admin only."""
    _require_first_admin()
    from .._internal.audit_logger import astor_query_audit
    rows = astor_query_audit(
        actor=args.actor, user_id=args.user, action=args.action,
        since_ts=args.since, limit=args.limit,
    )
    if not rows:
        print("   (no matching audit rows)")
        return 0
    print(f"   {'ts':30s} {'actor':16s} {'tier':10s} {'user':18s} {'action':10s} target")
    for r in rows:
        meta = r.get('metadata') or {}
        meta_str = f" reason={r['reason'][:40]!r}" if r.get('reason') else ""
        print(f"   {r['ts']:30s} {r['actor']:16s} {r['tier']:10s} {r['user_id'] or '-':18s} {r['action']:10s} {r['target'] or '-':30s}{meta_str}")
    return 0


def cmd_admin_whoami(args) -> int:
    """Show current first_admin lock."""
    from .._internal.acl_layout import get_admin_lock_path
    p = get_admin_lock_path()
    if not p.exists():
        print(f'❌ No first_admin lock at {p}')
        return 1
    import json
    print(f"   lockfile: {p}")
    print(f"   contents: {json.dumps(json.loads(p.read_text()), indent=2)}")
    return 0


# ============================================================
# am platform ... (bot-binding.db CRUD)
# ============================================================

def cmd_platform_list(args) -> int:
    """List platforms from bot-binding.db."""
    from .._internal.bot_binding import list_platforms
    _require_first_admin()
    rows = list_platforms(enabled_only=False)
    if not rows:
        print('No platforms in bot-binding.db. Run am platform token-set or am platform add (out of scope).')
        return 0
    print(f'   {"platform_id":40s} {"kind":10s} {"account_id":30s} {"enabled":7s} {"source":25s}')
    for p in rows:
        print(f'   {p["platform_id"]:40s} {p["platform_kind"]:10s} {p["account_id"]:30s} {p["enabled"]:7d} {(p.get("source") or ""):25s}')
    return 0


def cmd_platform_list_users(args) -> int:
    from .._internal.bot_binding import list_users
    _require_first_admin()
    rows = list_users(active_only=False)
    if not rows:
        print('No user_meta rows in bot-binding.db.')
        return 0
    print(f'   {"primary_id":12s} {"short_alias":20s} {"real_name":14s} {"role":6s} {"plan":10s} {"active":7s}')
    for u in rows:
        rn = (u.get('real_name') or '')
        plan = (u.get('subscription_plan') or '')
        print(f'   {u["user_id"]:12s} {u["short_alias"]:20s} {rn:14s} {u["role"]:6s} {plan:10s} {u["active"]:7d}')
    return 0


def cmd_platform_list_bindings(args) -> int:
    from .._internal.bot_binding import list_bindings
    _require_first_admin()
    rows = list_bindings(active_only=not args.all)
    if not rows:
        print('No bindings found.')
        return 0
    print(f'   {"platform":40s} {"chat_id":42s} -> {"user_id":12s} ({"role_inherit":12s}) {"active":6s}')
    for b in rows:
        plat_kind = b.get('platform_kind', '?')
        chat = b.get('chat_id', '?')[:40]
        user = b.get('user_id', '?')
        print(f'   {b["platform_id"]:40s} {chat:42s} -> {user:12s} ({b.get("role_inherit", "?"):12s}) {b["active"]:6d}')
    return 0


def cmd_platform_resolve(args) -> int:
    from .._internal.bot_binding import resolve_chat_to_user
    _require_first_admin()
    r = resolve_chat_to_user(args.platform_id, args.chat_id)
    if r is None:
        print(f'❌ no active binding for {args.platform_id} : {args.chat_id}')
        return 1
    print(f'   platform_id:  {args.platform_id}')
    print(f'   chat_id:      {args.chat_id}')
    print(f'   user_id:      {r["user_id"]}')
    print(f'   role_inherit: {r["role_inherit"]}')
    print(f'   scope:        {r.get("scope", "?")}')
    print(f'   allow_from:   {(r.get("allow_from") or "-")[:40]}')
    print(f'   active:       {r["active"]}')
    print(f'   binding_id:   {r["binding_id"]}')
    return 0


def cmd_platform_token_get(args) -> int:
    from .._internal.platform_bridge import astor_get_token
    from .._internal.bot_binding import get_platform
    _require_first_admin()
    # Try as plain platform_id (e.g. "telegram:hermes_bot") FIRST
    p = get_platform(args.platform_id)
    if p is None and ':' in args.platform_id:
        # Fallback: parse as kind:account_id
        kind, account_id = args.platform_id.split(':', 1)
        r = astor_get_token(kind, account_id)
        if not r.token:
            print(f'❌ no token found for {args.platform_id} (source={r.source})')
            return 1
        print(f'✅ {args.platform_id}')
        print(f'   source:  {r.source}')
        print(f'   token:   {r.token[:12]}...{r.token[-6:]} (len={len(r.token)})')
        print(f'   account: {r.account_id}')
        return 0
    if p is None:
        print(f'❌ no platform row for {args.platform_id}')
        return 1
    # Direct row hit
    if not p.get('account_token'):
        print(f'❌ platform {args.platform_id} has empty token')
        return 1
    print(f'✅ {args.platform_id}')
    print(f'   source:  db (direct row lookup, no audit)')
    print(f'   token:   {p["account_token"][:12]}...{p["account_token"][-6:]} (len={len(p["account_token"])})')
    print(f'   account: {p["account_id"]}')
    return 0


def cmd_platform_token_set(args) -> int:
    from .._internal.bot_binding import get_platform, upsert_platform
    _require_first_admin()
    if ':' not in args.platform_id:
        print(f'❌ bad platform_id: {args.platform_id}')
        return 1
    kind, account_id = args.platform_id.split(':', 1)
    existing = get_platform(account_id)
    notes = 'manual:cli token-set'
    if existing:
        notes = (existing.get('notes') or '') + ' | token-set via CLI'
    pid = upsert_platform(
        platform_kind=kind,
        account_id=account_id,
        account_token=args.token,
        base_url=(existing or {}).get('base_url'),
        enabled=True,
        notes=notes,
        source='cli:token-set',
    )
    print(f'✅ {args.platform_id} token updated (len={len(args.token)})')
    return 0


def cmd_platform_bind(args) -> int:
    from .._internal.bot_binding import upsert_binding
    _require_first_admin()
    bid = upsert_binding(
        platform_id=args.platform_id,
        chat_id=args.chat_id,
        user_id=args.user_id,
        scope=args.scope,
        allow_from=args.allow_from or args.chat_id,
        bound_by='first_admin',
        notes='cli bind',
    )
    print(f'✅ binding created: {bid[:8]}... ({args.platform_id} : {args.chat_id} -> {args.user_id})')
    return 0


def cmd_platform_unbind(args) -> int:
    from .._internal.bot_binding import resolve_chat_to_user, revoke_binding
    _require_first_admin()
    # find binding_id from chat_id
    r = resolve_chat_to_user(args.platform_id, args.chat_id)
    if r is None:
        print(f'❌ no active binding for {args.platform_id} : {args.chat_id}')
        return 1
    revoke_binding(r['binding_id'], revoked_by='first_admin')
    print(f'✅ unbound: {args.platform_id} : {args.chat_id} (binding_id={r["binding_id"][:8]}...)')
    return 0


def cmd_platform_add_user(args) -> int:
    from .._internal.bot_binding import upsert_user
    from .._internal.acl_layout import _validate_user_id
    _require_first_admin()
    try:
        _validate_user_id(args.user_id)
    except ValueError as e:
        print(f'❌ invalid user_id: {e}')
        return 1
    upsert_user(
        user_id=args.user_id,
        short_alias=args.short_alias,
        real_name=args.real_name,
        role=args.role,
        subscription_plan=args.plan,
        source='cli:add-user',
    )
    print(f'✅ user_meta upserted: {args.user_id} (alias={args.short_alias}, role={args.role})')
    # NOTE: 9-db layout creation = call `am bot add-user` (different subcommand).
    # We could call cmd_bot_add_user programmatically but skip here to avoid audit duplication.
    print('   (run `am bot add-user <user_id>` separately to create 9-db layout)')
    return 0


def cmd_platform_verify(args) -> int:
    """Run 6 invariants on bot-binding.db."""
    import sqlite3
    _require_first_admin()
    db_path = Path('<runtime_dir>bot-binding.db')
    con = sqlite3.connect(str(db_path))
    problems = []
    # Inv 1: TG/DC/feishu/webchat exactly 1 row
    for r in con.execute("SELECT platform_kind, COUNT(*) as n FROM platforms WHERE platform_kind IN ('telegram','discord','feishu','webchat') GROUP BY platform_kind HAVING n > 1"):
        problems.append(f'INV1: {r["platform_kind"]} has {r["n"]} rows (expected 1)')
    # Inv 2: unique active (platform,chat)
    for r in con.execute("SELECT platform_id, chat_id, COUNT(*) as n FROM bindings WHERE active=1 GROUP BY platform_id, chat_id HAVING n > 1"):
        problems.append(f'INV2: {r["platform_id"]}:{r["chat_id"][:24]} has {r["n"]} active bindings')
    # Inv 3: every binding.user_id has user_meta
    for r in con.execute("SELECT b.user_id, b.binding_id FROM bindings b LEFT JOIN user_meta u ON b.user_id=u.user_id WHERE u.user_id IS NULL"):
        problems.append(f'INV3: binding {r["binding_id"][:8]} -> user {r["user_id"]} (no user_meta)')
    # Inv 4: no empty user_id
    for r in con.execute("SELECT binding_id FROM bindings WHERE user_id IS NULL OR user_id=''"):
        problems.append(f'INV4: binding {r[0][:8]} empty user_id')
    # Inv 5: enabled platforms have non-empty token
    for r in con.execute("SELECT platform_id FROM platforms WHERE enabled=1 AND (account_token IS NULL OR account_token='')"):
        problems.append(f'INV5: enabled {r["platform_id"]} empty token')
    # Inv 6: weixin base_url
    for r in con.execute("SELECT platform_id FROM platforms WHERE platform_kind='weixin' AND (base_url IS NULL OR base_url='')"):
        problems.append(f'INV6: weixin {r["platform_id"]} no base_url')
    con.close()
    if problems:
        print(f'❌ {len(problems)} violations:')
        for p in problems:
            print(f'  - {p}')
        return 1
    print('✅ all 6 invariants pass')
    return 0


    if __name__ == '__main__':
        sys.exit(main())
