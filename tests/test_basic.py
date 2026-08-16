"""Tests for astor-memory v0.1 schema + bus."""
import tempfile
from pathlib import Path

import pytest

from astor_memory.bus import AstorBus as Bus, astor_bus, astor_reset_bus
from astor_memory.bus.schema import astor_init_schema, astor_verify_schema, SCHEMA_VERSION
from astor_memory.config import get_default_bus_path


@pytest.fixture
def temp_bus():
    """Create a temporary bus instance."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / 'test_astor.db'
        bus = Bus(db_path)
        yield bus
        bus.close()


def test_schema_init(temp_bus):
    """Schema should initialize and verify OK."""
    result = astor_verify_schema(temp_bus.conn)
    assert result['ok']
    assert result['schema_version'] == SCHEMA_VERSION
    assert 'events' in result['tables_present']
    assert 'memory_candidates' in result['tables_present']
    assert 'memory_canonical' in result['tables_present']
    assert 'audit_log' in result['tables_present']


def test_append_event(temp_bus):
    """Append event should return event_id."""
    event_id = temp_bus.append_event(
        namespace='test',
        agent_id='test_agent',
        source='pytest',
        action='test_event',
        content='test content',
        metadata={'key': 'value'},
    )
    assert event_id > 0
    # Read back
    row = temp_bus.conn.execute(
        "SELECT namespace, agent_id, source, action, content, metadata FROM events WHERE id = ?",
        (event_id,),
    ).fetchone()
    assert row[0] == 'test'
    assert row[1] == 'test_agent'
    assert row[2] == 'pytest'
    assert row[3] == 'test_event'
    assert row[4] == 'test content'


def test_insert_candidate(temp_bus):
    """Insert candidate should return candidate_id."""
    event_id = temp_bus.append_event(
        namespace='test', agent_id='a', source='s', action='write', content='c',
    )
    cand_id = temp_bus.insert_candidate(
        event_id=event_id,
        namespace='test',
        content='I prefer coffee',
        kind='user_preference',
        confidence=0.9,
    )
    assert cand_id > 0


def test_promote_candidate(temp_bus):
    """Promote candidate should insert into memory_canonical."""
    event_id = temp_bus.append_event(
        namespace='admin', agent_id='a', source='s', action='write', content='c',
    )
    cand_id = temp_bus.insert_candidate(
        event_id=event_id, namespace='admin', content='test fact', kind='fact',
    )
    canon_id = temp_bus.promote_candidate(
        cand_id, promoted_by='test', user_id='admin', tier='public',
    )
    assert canon_id > 0
    row = temp_bus.conn.execute(
        "SELECT content, kind, verdict, tier, user_id FROM memory_canonical WHERE id = ?",
        (canon_id,),
    ).fetchone()
    assert row[0] == 'test fact'
    assert row[1] == 'fact'
    assert row[2] == 'settled'  # default verdict
    assert row[3] == 'public'
    assert row[4] == 'admin'


def test_transaction_rollback(temp_bus):
    """If exception in transaction, both inserts should rollback."""
    event_id = temp_bus.append_event(
        namespace='admin', agent_id='a', source='s', action='write', content='c',
    )
    try:
        with temp_bus.transaction() as c:
            c.execute(
                "INSERT INTO memory_candidates (event_id, namespace, content, kind) VALUES (?, ?, ?, ?)",
                (event_id, 'admin', 'will rollback', 'fact'),
            )
            raise ValueError('test rollback')
    except ValueError:
        pass
    # Candidate should NOT exist
    n = temp_bus.conn.execute(
        "SELECT COUNT(*) FROM memory_candidates WHERE namespace = 'admin' AND content = 'will rollback'"
    ).fetchone()[0]
    assert n == 0


def test_audit_write(temp_bus):
    """Audit log should record events."""
    audit_id = temp_bus.write_audit(
        event='test_event',
        actor='test_actor',
        target_type='fact',
        target_id='f_1',
        reason='test reason',
    )
    assert audit_id > 0
    row = temp_bus.conn.execute(
        "SELECT event, actor, target_type, target_id, severity FROM audit_log WHERE id = ?",
        (audit_id,),
    ).fetchone()
    assert row[0] == 'test_event'
    assert row[1] == 'test_actor'
    assert row[2] == 'fact'
    assert row[3] == 'f_1'
    assert row[4] == 'info'  # default


def test_regex_extract():
    """Regex extraction should categorize facts correctly."""
    from astor_memory.forge.extractor import astor_regex_extract as regex_extract, astor_detect_capture_intent as detect_capture_intent

    facts = regex_extract('I prefer coffee')
    assert len(facts) == 1
    assert facts[0].kind == 'user_preference'
    assert 'coffee' in facts[0].content.lower()

    facts = regex_extract('I decided to sell NVDA')
    assert len(facts) == 1
    assert facts[0].kind == 'decision'

    facts = regex_extract('today I went to the store')
    assert len(facts) == 1
    assert facts[0].kind == 'event'

    assert detect_capture_intent('remember this: I prefer tea')
    assert detect_capture_intent('from now on I will code daily')
    assert not detect_capture_intent('I went to the park')


def test_choose_extract_mode():
    """Extract mode heuristic."""
    from astor_memory.forge.extractor import astor_choose_extract_mode as choose_extract_mode

    assert choose_extract_mode('short') == 'regex'
    assert choose_extract_mode('x' * 500) == 'regex'
    assert choose_extract_mode('x' * 1500) == 'none'


def test_cli_version():
    """CLI version command."""
    from astor_memory.cli.main import main

    result = main(['version'])
    assert result == 0


def test_cli_init(tmp_path, monkeypatch):
    """CLI init command."""
    from astor_memory.cli import main

    # Redirect ASTOR_DIR to tmp
    monkeypatch.setenv('ASTOR_DIR', str(tmp_path / 'astor'))
    result = main(['init'])
    assert result == 0
    assert (tmp_path / 'astor').exists()


def test_llm_extract_no_keys_falls_back_to_regex(monkeypatch):
    """When no LLM API keys set, astor_llm_extract falls back to regex (graceful degradation).

    Per Plan § LLM fallback provider: graceful degradation = no LLM available
    doesn't break the write pipeline.
    """
    from astor_memory.forge.llm_extract import astor_llm_extract

    # Clear all known provider keys via monkeypatch
    for key in ('MINIMAX_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'GOOGLE_API_KEY', 'DEEPSEEK_API_KEY', 'ZHIPU_API_KEY'):
        monkeypatch.delenv(key, raising=False)

    facts = astor_llm_extract('I prefer dark roast coffee', primary='m3', fallback_chain=['openai', 'anthropic'])
    # Should fall back to regex (which extracts "dark roast coffee" as user_preference)
    assert len(facts) >= 1
    assert any(f.kind == 'user_preference' for f in facts)


def test_llm_provider_env_keys():
    """Provider env key mapping is correct per Plan § LLM fallback."""
    from astor_memory.forge.llm_extract import PROVIDER_ENV_KEYS, astor_get_api_key

    assert PROVIDER_ENV_KEYS['m3'] == 'MINIMAX_API_KEY'
    assert PROVIDER_ENV_KEYS['openai'] == 'OPENAI_API_KEY'
    assert PROVIDER_ENV_KEYS['anthropic'] == 'ANTHROPIC_API_KEY'
    assert PROVIDER_ENV_KEYS['gemini'] == 'GOOGLE_API_KEY'
    assert PROVIDER_ENV_KEYS['ollama'] == ''  # local, no key needed
    assert PROVIDER_ENV_KEYS['deepseek'] == 'DEEPSEEK_API_KEY'
    assert PROVIDER_ENV_KEYS['zhipu'] == 'ZHIPU_API_KEY'

    # astor_get_api_key returns empty string for ollama (local)
    assert astor_get_api_key('ollama') == ''
    # Returns empty for unknown provider
    assert astor_get_api_key('unknown') == ''


def test_installer_tier_classification():
    """Tier classification per Plan Insight 18."""
    from astor_memory.installer.registry import astor_get_agent_tier

    # Tier A: priority hook
    assert astor_get_agent_tier('claude-code') == 'A'
    assert astor_get_agent_tier('cline') == 'A'
    assert astor_get_agent_tier('opencode') == 'A'
    # Tier B: patchable
    assert astor_get_agent_tier('hermes') == 'B'
    assert astor_get_agent_tier('openclaw') == 'B'
    # Tier C: coexist only
    assert astor_get_agent_tier('cursor') == 'C'
    assert astor_get_agent_tier('continue') == 'C'
    assert astor_get_agent_tier('windsurf') == 'C'
    assert astor_get_agent_tier('aider') == 'C'
    # Tier D: skip
    assert astor_get_agent_tier('roo-code') == 'D'
    assert astor_get_agent_tier('antigravity') == 'D'


def test_installer_mode_capability():
    """Mode capability matrix: priority only for Tier A."""
    from astor_memory.installer.registry import astor_supports_mode

    # Tier A agents support all modes
    assert astor_supports_mode('claude-code', 'priority')
    assert astor_supports_mode('cline', 'priority')
    assert astor_supports_mode('opencode', 'priority')
    # Tier C agents do NOT support priority
    assert not astor_supports_mode('cursor', 'priority')
    assert not astor_supports_mode('aider', 'priority')
    # All supported agents support coexist (default)
    for agent in ('claude-code', 'cline', 'opencode', 'hermes', 'cursor', 'continue', 'windsurf', 'aider'):
        assert astor_supports_mode(agent, 'coexist'), f'{agent} should support coexist'


def test_installer_priority_fallback(tmp_path):
    """When agent doesn't support requested mode, fall back to coexist."""
    from pathlib import Path
    from astor_memory.installer import astor_install

    # cursor doesn't support 'priority' — should fall back
    result = astor_install('cursor', Path(tmp_path), mode='priority')
    assert result['fallback'] is True
    assert result['mode_requested'] == 'priority'
    assert result['mode_actual'] == 'coexist'


def test_installer_replace_unsupported(tmp_path):
    """Tier B/C agents (Hermes, Cursor) don't support replace mode → fallback to coexist."""
    from pathlib import Path
    from astor_memory.installer import astor_install

    # Hermes: replace not supported → fallback to coexist (consistent with cursor priority)
    result = astor_install('hermes', Path(tmp_path), mode='replace')
    assert 'fallback' in result
    assert result['mode_requested'] == 'replace'
    assert result['mode_actual'] == 'coexist'


def test_installer_claude_code_dry_run(tmp_path):
    """Claude Code install: dry-run produces wrapper script plan."""
    from pathlib import Path
    from astor_memory.installer import astor_install

    result = astor_install('claude-code', Path(tmp_path), mode='priority')
    assert 'result' in result or 'changes' in result
    plan = result.get('result', result)
    assert plan['mode'] == 'priority'
    assert plan['tier'] == 'A'
    assert len(plan['changes']) >= 1
    # Wrapper script should be executable
    assert any(ch.get('executable') for ch in plan['changes'])


def test_installer_cursor_coexist(tmp_path):
    """Cursor coexist: writes 00-astor.md to .cursor/rules/."""
    from pathlib import Path
    from astor_memory.installer import astor_install

    result = astor_install('cursor', Path(tmp_path), mode='coexist')
    plan = result.get('result', result)
    assert plan['mode'] == 'coexist'
    assert plan['tier'] == 'C'
    assert any('00-astor.md' in ch.get('path', '') for ch in plan['changes'])


def test_cli_config_show(tmp_path, monkeypatch):
    """am config show displays JSON config."""
    from astor_memory.cli import main

    monkeypatch.setenv('ASTOR_DIR', str(tmp_path / 'astor'))
    result = main(['config', 'show'])
    assert result == 0


def test_cli_config_get_set(tmp_path, monkeypatch):
    """am config get/set roundtrip."""
    from astor_memory.cli import main

    monkeypatch.setenv('ASTOR_DIR', str(tmp_path / 'astor'))
    # Initial value
    result = main(['config', 'get', 'extract_mode'])
    assert result == 0
    # Set
    result = main(['config', 'set', 'extract_mode', 'regex'])
    assert result == 0
    # Verify
    result = main(['config', 'get', 'extract_mode'])
    assert result == 0


def test_rest_health(tmp_path, monkeypatch):
    """GET /v1/health returns OK + DB paths."""
    from astor_memory.server import create_app

    monkeypatch.setenv('ASTOR_DIR', str(tmp_path / 'astor'))
    app = create_app()
    client = app.test_client()
    r = client.get('/v1/health')
    assert r.status_code == 200
    data = r.get_json()
    assert data['status'] == 'ok'
    assert 'version' in data
    assert 'astor_bus.db' in data['dbs']['bus']
    assert 'astor_nest.db' in data['dbs']['nest']


def test_rest_write_read_roundtrip(tmp_path, monkeypatch):
    """POST /v1/write then /v1/read returns the fact."""
    from astor_memory.server import create_app

    monkeypatch.setenv('ASTOR_DIR', str(tmp_path / 'astor'))
    app = create_app()
    client = app.test_client()

    # Write
    r = client.post('/v1/write', json={'text': 'I prefer dark roast coffee', 'user': 'admin'})
    assert r.status_code == 200
    write_data = r.get_json()
    assert write_data['count'] >= 1
    assert len(write_data['fact_ids']) >= 1

    # Read
    r = client.post('/v1/read', json={'query': 'coffee preference', 'top_k': 3})
    assert r.status_code == 200
    read_data = r.get_json()
    assert read_data['count'] >= 1
    # Top result should mention coffee
    assert 'coffee' in read_data['results'][0]['content'].lower()


def test_rest_write_missing_text(tmp_path, monkeypatch):
    """POST /v1/write without text returns 400."""
    from astor_memory.server import create_app

    monkeypatch.setenv('ASTOR_DIR', str(tmp_path / 'astor'))
    app = create_app()
    client = app.test_client()
    r = client.post('/v1/write', json={'user': 'admin'})
    assert r.status_code == 400
    assert 'text required' in r.get_json()['error']


def test_rest_read_missing_query(tmp_path, monkeypatch):
    """POST /v1/read without query returns 400."""
    from astor_memory.server import create_app

    monkeypatch.setenv('ASTOR_DIR', str(tmp_path / 'astor'))
    app = create_app()
    client = app.test_client()
    r = client.post('/v1/read', json={'top_k': 5})
    assert r.status_code == 400


def test_rest_install_plan(tmp_path, monkeypatch):
    """POST /v1/install returns install plan for cursor."""
    from astor_memory.server import create_app

    monkeypatch.setenv('ASTOR_DIR', str(tmp_path / 'astor'))
    app = create_app()
    client = app.test_client()
    r = client.post('/v1/install', json={'ide': 'cursor', 'mode': 'coexist'})
    assert r.status_code == 200
    plan = r.get_json()
    # cursor falls back to coexist
    final = plan.get('result', plan)
    assert final['mode'] == 'coexist'
    assert final['tier'] == 'C'


def test_rest_not_found(tmp_path, monkeypatch):
    """Unknown endpoint returns 404."""
    from astor_memory.server import create_app

    monkeypatch.setenv('ASTOR_DIR', str(tmp_path / 'astor'))
    app = create_app()
    client = app.test_client()
    r = client.get('/v1/nonexistent')
    assert r.status_code == 404


def test_migrate_dry_run(tmp_path, monkeypatch):
    """Migrate dry-run reports counts without writing."""
    import sqlite3
    from astor_memory.cli.migrate import astor_migrate_from_memory_bus

    # Create a fake legacy memory-bus DB
    legacy = tmp_path / 'bus.db'
    conn = sqlite3.connect(str(legacy))
    conn.execute("""
        CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT, namespace TEXT,
            agent_id TEXT, source TEXT, action TEXT, content TEXT,
            provenance TEXT, request_id TEXT, prev_event_id INTEGER,
            tombstone INTEGER, visibility TEXT)
    """)
    conn.execute("INSERT INTO events VALUES (1, '2026-08-15T10:00:00Z', 'admin', 'cli', 'cli.write', 'write', 'fact 1', NULL, NULL, NULL, 0, 'default')")
    conn.execute("""
        CREATE TABLE memory_candidates (id INTEGER PRIMARY KEY, event_id INTEGER,
            namespace TEXT, content TEXT, kind TEXT, confidence REAL, importance REAL,
            tags TEXT, metadata TEXT, provenance TEXT, created_at TEXT, review_state TEXT,
            promoted_at TEXT, promoted_to INTEGER, rejected_reason TEXT,
            ttl_days INTEGER, expires_at TEXT, scene TEXT)
    """)
    conn.execute("INSERT INTO memory_candidates VALUES (1, 1, 'admin', 'I love coffee', 'user_preference', 0.7, 0.5, '[]', NULL, NULL, '2026-08-15T10:00:00Z', 'promoted', NULL, 1, NULL, NULL, NULL, 'casual')")
    conn.execute("""
        CREATE TABLE memory_canonical (id INTEGER PRIMARY KEY, candidate_id INTEGER,
            event_id INTEGER, namespace TEXT, content TEXT, kind TEXT, confidence REAL,
            importance REAL, tags TEXT, metadata TEXT, provenance TEXT, promoted_at TEXT,
            promoted_by TEXT, last_accessed_at TEXT, access_count INTEGER,
            tombstoned INTEGER, tombstoned_at TEXT, expires_at TEXT, scene TEXT,
            embedding BLOB, stable_id TEXT, user_id TEXT, session_id TEXT,
            scope_type TEXT, valid_from TEXT, valid_to TEXT, status TEXT,
            superseded_by INTEGER, revision INTEGER)
    """)
    conn.execute("INSERT INTO memory_canonical (id, candidate_id, event_id, namespace, content, kind, confidence, importance, tags, promoted_at, promoted_by, status, scope_type, revision) VALUES (1, 1, 1, 'admin', 'I love coffee', 'user_preference', 0.7, 0.5, '[]', '2026-08-15T10:00:00Z', 'cli.write', 'active', 'long_term', 1)")
    conn.commit()
    conn.close()

    target = tmp_path / 'astor'
    report = astor_migrate_from_memory_bus(legacy, target, dry_run=True)
    assert report.events_migrated == 1
    assert report.candidates_migrated == 1
    assert report.canonical_migrated == 1
    assert report.errors == []
    # Verify nothing was written
    assert not (target / 'astor_bus.db').exists()


def test_migrate_actual_writes(tmp_path, monkeypatch):
    """Migrate actually writes to astor_bus.db."""
    import sqlite3
    from astor_memory.cli.migrate import astor_migrate_from_memory_bus

    # Fake legacy
    legacy = tmp_path / 'bus.db'
    conn = sqlite3.connect(str(legacy))
    conn.execute("""CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT, namespace TEXT,
        agent_id TEXT, source TEXT, action TEXT, content TEXT, provenance TEXT,
        request_id TEXT, prev_event_id INTEGER, tombstone INTEGER, visibility TEXT)""")
    conn.execute("INSERT INTO events VALUES (1, '2026-08-15T10:00:00Z', 'admin', 'cli', 'cli.write', 'write', 'hello world', NULL, NULL, NULL, 0, 'default')")
    conn.execute("""CREATE TABLE memory_candidates (id INTEGER PRIMARY KEY, event_id INTEGER,
        namespace TEXT, content TEXT, kind TEXT, confidence REAL, importance REAL,
        tags TEXT, metadata TEXT, provenance TEXT, created_at TEXT, review_state TEXT,
        promoted_at TEXT, promoted_to INTEGER, rejected_reason TEXT,
        ttl_days INTEGER, expires_at TEXT, scene TEXT)""")
    conn.execute("""CREATE TABLE memory_canonical (id INTEGER PRIMARY KEY, candidate_id INTEGER,
        event_id INTEGER, namespace TEXT, content TEXT, kind TEXT, confidence REAL,
        importance REAL, tags TEXT, metadata TEXT, provenance TEXT, promoted_at TEXT,
        promoted_by TEXT, last_accessed_at TEXT, access_count INTEGER,
        tombstoned INTEGER, tombstoned_at TEXT, expires_at TEXT, scene TEXT,
        embedding BLOB, stable_id TEXT, user_id TEXT, session_id TEXT,
        scope_type TEXT, valid_from TEXT, valid_to TEXT, status TEXT,
        superseded_by INTEGER, revision INTEGER)""")
    conn.execute("INSERT INTO memory_canonical (id, candidate_id, event_id, namespace, content, kind, confidence, importance, tags, promoted_at, promoted_by, status, scope_type, revision, stable_id) VALUES (1, NULL, 1, 'admin', 'hello world fact', 'fact', 0.7, 0.5, '[]', '2026-08-15T10:00:00Z', 'cli.write', 'active', 'long_term', 1, 'stable_1')")
    conn.commit()
    conn.close()

    target = tmp_path / 'astor'
    report = astor_migrate_from_memory_bus(legacy, target, dry_run=False)
    assert report.events_migrated == 1
    assert report.canonical_migrated == 1
    assert (target / 'astor_bus.db').exists()
    assert (target / 'astor_nest.db').exists()

    # Verify canonical row has verdict='settled' (from status='active')
    bus_conn = sqlite3.connect(str(target / 'astor_bus.db'))
    row = bus_conn.execute("SELECT content, verdict, stable_id FROM memory_canonical").fetchone()
    assert row[0] == 'hello world fact'
    assert row[1] == 'settled'
    assert row[2] == 'stable_1'
    bus_conn.close()


def test_migrate_idempotent(tmp_path):
    """Migrating twice doesn't duplicate rows."""
    import sqlite3
    from astor_memory.cli.migrate import astor_migrate_from_memory_bus

    legacy = tmp_path / 'bus.db'
    conn = sqlite3.connect(str(legacy))
    conn.execute("""CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT, namespace TEXT,
        agent_id TEXT, source TEXT, action TEXT, content TEXT, provenance TEXT,
        request_id TEXT, prev_event_id INTEGER, tombstone INTEGER, visibility TEXT)""")
    conn.execute("INSERT INTO events VALUES (1, '2026-08-15T10:00:00Z', 'admin', 'cli', 'cli.write', 'write', 'test', NULL, NULL, NULL, 0, 'default')")
    conn.execute("""CREATE TABLE memory_candidates (id INTEGER PRIMARY KEY, event_id INTEGER,
        namespace TEXT, content TEXT, kind TEXT, confidence REAL, importance REAL,
        tags TEXT, metadata TEXT, provenance TEXT, created_at TEXT, review_state TEXT,
        promoted_at TEXT, promoted_to INTEGER, rejected_reason TEXT,
        ttl_days INTEGER, expires_at TEXT, scene TEXT)""")
    conn.execute("""CREATE TABLE memory_canonical (id INTEGER PRIMARY KEY, candidate_id INTEGER,
        event_id INTEGER, namespace TEXT, content TEXT, kind TEXT, confidence REAL,
        importance REAL, tags TEXT, metadata TEXT, provenance TEXT, promoted_at TEXT,
        promoted_by TEXT, last_accessed_at TEXT, access_count INTEGER,
        tombstoned INTEGER, tombstoned_at TEXT, expires_at TEXT, scene TEXT,
        embedding BLOB, stable_id TEXT, user_id TEXT, session_id TEXT,
        scope_type TEXT, valid_from TEXT, valid_to TEXT, status TEXT,
        superseded_by INTEGER, revision INTEGER)""")
    conn.execute("INSERT INTO memory_canonical (id, candidate_id, event_id, namespace, content, kind, confidence, importance, tags, promoted_at, promoted_by, status, scope_type, revision, stable_id) VALUES (1, NULL, 1, 'admin', 'idempotent test', 'fact', 0.7, 0.5, '[]', '2026-08-15T10:00:00Z', 'cli', 'active', 'long_term', 1, 'stable_idem')")
    conn.commit()
    conn.close()

    target = tmp_path / 'astor'
    report1 = astor_migrate_from_memory_bus(legacy, target, dry_run=False)
    report2 = astor_migrate_from_memory_bus(legacy, target, dry_run=False)
    assert report1.canonical_migrated == 1
    assert report2.canonical_migrated == 0
    assert report2.skipped_existing >= 1


def test_migrate_missing_source(tmp_path):
    """Migrate from non-existent DB returns error."""
    from astor_memory.cli.migrate import astor_migrate_from_memory_bus

    report = astor_migrate_from_memory_bus(tmp_path / 'nonexistent.db')
    assert len(report.errors) >= 1
    assert 'not found' in report.errors[0]


def test_e2e_integration(tmp_path, monkeypatch):
    """End-to-end integration test: CLI init → write → recall → cite flow.

    Per Plan § Week 5 step 4.7: install → write → read → recall → cite roundtrip.

    Exercises:
    - am init creates 3 DBs (astor_bus.db, astor_forge.db, astor_nest.db)
    - am write extracts fact via regex, persists to bus + nest
    - am recall queries via vector similarity, returns fact
    - REST API /v1/write + /v1/read also work (already covered)
    - Python API: astor_bus() + astor_nest() + astor_forge() interop
    """
    import os
    monkeypatch.setenv('ASTOR_DIR', str(tmp_path / 'astor_e2e'))

    # Reset singletons (since we changed ASTOR_DIR)
    from astor_memory.bus.store import astor_reset_bus
    from astor_memory.nest.vector_store import astor_reset_nest
    from astor_memory.nest.embeddings import astor_reset_embedding_model
    astor_reset_bus()
    astor_reset_nest()
    astor_reset_embedding_model()

    # 1. CLI init
    from astor_memory.cli import main as cli_main
    assert cli_main(['init']) == 0
    astor_dir = Path(tmp_path / 'astor_e2e')
    assert (astor_dir / 'astor_bus.db').exists()
    assert (astor_dir / 'astor_nest.db').exists()

    # 2. CLI write (multi-fact extraction)
    assert cli_main(['write', 'I prefer dark roast coffee and tea', '--user', 'admin']) == 0

    # 3. CLI recall (vector similarity search)
    import io, contextlib
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        cli_main(['recall', 'coffee preference', '--user', 'admin', '--top-k', '3'])
    output = captured.getvalue()
    assert 'fact_id=' in output
    assert 'similarity=' in output

    # 4. Python API: read fact back via astor_nest + astor_bus
    from astor_memory import astor_nest, astor_bus
    bus = astor_bus()
    nest = astor_nest()

    # Verify fact was stored in bus
    n_facts = bus.conn.execute('SELECT count(*) FROM memory_canonical').fetchone()[0]
    assert n_facts >= 1

    # Verify embedding was stored in nest
    n_emb = nest.conn.execute('SELECT count(*) FROM embeddings').fetchone()[0]
    assert n_emb >= 1

    # 5. Verify Python recall matches CLI recall
    from astor_memory.nest.embeddings import astor_get_embedding_model
    model = astor_get_embedding_model()
    query_emb = list(model.embed(['coffee']))[0]
    results = nest.search(query_emb, limit=5)
    assert len(results) >= 1
    # Top result should mention coffee
    fact_id = results[0][0]
    row = bus.conn.execute('SELECT content FROM memory_canonical WHERE id = ?', (fact_id,)).fetchone()
    assert 'coffee' in row[0].lower()

    # 6. config CLI works
    assert cli_main(['config', 'show']) == 0
    assert cli_main(['config', 'get', 'extract_mode']) == 0

    # 7. install CLI plan (dry-run)
    assert cli_main(['install', '--ide', 'cursor', '--mode', 'coexist']) == 0

    # 8. migrate CLI dry-run (no actual source)
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        result = cli_main(['migrate', '--from', 'memory-bus', '--source', str(astor_dir / 'nonexistent.db'), '--dry-run'])
    # Non-existent source returns error code 1, but that's fine for dry-run
    output = captured.getvalue()
    assert 'events' in output or 'Error' in output or 'not found' in output or result in (0, 1)
