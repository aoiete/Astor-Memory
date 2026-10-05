"""v1.16.69 Ship C tests.

Covers:
  - schema v18 -> v19 adds explicit_user column on events
  - append_event(explicit_user=True/False) writes correctly
  - promote_candidate gates on explicit_user (default refuses auto-captured)
  - markdown_export renders valid Obsidian markdown with frontmatter
"""
import os
import sys
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest


@pytest.fixture
def tmp_astor_home(monkeypatch, tmp_path):
    env_dir = tmp_path / 'astor_home'
    env_dir.mkdir()
    monkeypatch.setenv('ASTOR_DIR', str(env_dir))
    yield env_dir


def test_v19_events_has_explicit_user(tmp_astor_home):
    from astor_memory.bus.store import astor_bus_for
    bus = astor_bus_for(tier='private', user_id='admin')
    cols = {row[1] for row in bus._conn.execute(
        "PRAGMA table_info(events)"
    ).fetchall()}
    assert 'explicit_user' in cols


def test_append_event_explicit_user_flag(tmp_astor_home):
    from astor_memory.bus.store import astor_bus_for
    bus = astor_bus_for(tier='private', user_id='admin')
    eid = bus.append_event(
        namespace='private:admin', agent_id='admin', source='test',
        action='test', content='user said X', metadata='',
    )
    row = bus._conn.execute(
        "SELECT explicit_user FROM events WHERE id = ?", (eid,)
    ).fetchone()
    assert row[0] == 0  # default False
    eid2 = bus.append_event(
        namespace='private:admin', agent_id='admin', source='test',
        action='test', content='user said Y', metadata='',
        explicit_user=True,
    )
    row = bus._conn.execute(
        "SELECT explicit_user FROM events WHERE id = ?", (eid2,)
    ).fetchone()
    assert row[0] == 1


def test_promote_candidate_gate_refuses_auto_captured(tmp_astor_home):
    """When require_explicit_user_for_promote=True, gate refuses auto."""
    from astor_memory.bus.store import astor_bus_for
    bus = astor_bus_for(tier='private', user_id='admin')
    bus.require_explicit_user_for_promote = True
    eid = bus.append_event(
        namespace='private:admin', agent_id='admin', source='auto-hook',
        action='test', content='auto captured', metadata='',
        explicit_user=False,
    )
    cid = bus.insert_candidate(
        event_id=eid, namespace='private:admin', content='auto captured',
        kind='fact',
    )
    with pytest.raises(Exception, match="3-of-3 gate"):
        bus.promote_candidate(candidate_id=cid, promoted_by='test')


def test_promote_candidate_gate_passes_explicit(tmp_astor_home):
    """When explicit_user=True (and gate on), promote succeeds."""
    from astor_memory.bus.store import astor_bus_for
    bus = astor_bus_for(tier='private', user_id='admin')
    bus.require_explicit_user_for_promote = True
    eid = bus.append_event(
        namespace='private:admin', agent_id='admin', source='user-msg',
        action='test', content='user said Z', metadata='',
        explicit_user=True,
    )
    cid = bus.insert_candidate(
        event_id=eid, namespace='private:admin', content='user said Z',
        kind='preference',
    )
    fid = bus.promote_candidate(
        candidate_id=cid, promoted_by='test',
        user_id='admin', tier='private',
    )
    assert fid > 0


def test_promote_gate_default_off_allows_auto(tmp_astor_home):
    """Default OFF: gate does not reject auto-captured events."""
    from astor_memory.bus.store import astor_bus_for
    bus = astor_bus_for(tier='private', user_id='admin')
    # Default behavior — no flag set on bus, gate defaults to False
    assert getattr(bus, 'require_explicit_user_for_promote', False) is False
    eid = bus.append_event(
        namespace='private:admin', agent_id='admin', source='auto-hook',
        action='test', content='auto', metadata='',
        # explicit_user=False (default)
    )
    cid = bus.insert_candidate(
        event_id=eid, namespace='private:admin', content='auto',
        kind='fact',
    )
    fid = bus.promote_candidate(
        candidate_id=cid, promoted_by='test',
        user_id='admin', tier='private',
    )
    assert fid > 0


def test_markdown_export_writes_obsidian_files(tmp_astor_home):
    """Export user facts as Obsidian-friendly .md files with frontmatter."""
    from astor_memory.bus.store import astor_bus_for
    from astor_memory.nest.markdown_export import export_user_facts
    bus = astor_bus_for(tier='private', user_id='admin')
    # Insert one explicit fact
    eid = bus.append_event(
        namespace='private:admin', agent_id='admin', source='user-msg',
        action='user-statement', content='user likes espresso',
        metadata='', explicit_user=True,
    )
    cid = bus.insert_candidate(
        event_id=eid, namespace='private:admin',
        content='user likes espresso', kind='preference',
    )
    fid = bus.promote_candidate(
        candidate_id=cid, promoted_by='test',
        user_id='admin', tier='private',
    )

    out_dir = tmp_astor_home / 'export_out'
    result = export_user_facts(bus, tier='private', user_id='admin',
                               out_dir=out_dir)
    assert result['exported'] == 1
    assert result['skipped'] == 0
    files = list(out_dir.glob('*.md'))
    assert len(files) == 1
    content = files[0].read_text(encoding='utf-8')
    # Frontmatter present
    assert content.startswith('---\n')
    assert 'fact_id:' in content
    assert 'status: active' in content
    assert 'kind: preference' in content
    # Body content
    assert 'user likes espresso' in content
    # Frontmatter well-formed (terminated by ---)
    fm_match = re.match(r'^---\n(.*?)\n---\n', content, re.DOTALL)
    assert fm_match is not None


def test_markdown_export_inactive_fact_flagged(tmp_astor_home):
    """Inactive facts export with status: inactive frontmatter."""
    from astor_memory.bus.store import astor_bus_for
    from astor_memory.nest.markdown_export import export_user_facts
    bus = astor_bus_for(tier='private', user_id='admin')
    eid = bus.append_event(
        namespace='private:admin', agent_id='admin', source='user-msg',
        action='user-statement', content='old preference', metadata='',
        explicit_user=True,
    )
    cid = bus.insert_candidate(
        event_id=eid, namespace='private:admin',
        content='old preference', kind='preference',
    )
    fid = bus.promote_candidate(
        candidate_id=cid, promoted_by='test',
        user_id='admin', tier='private',
    )
    bus._conn.execute(
        "UPDATE memory_canonical SET status='inactive' WHERE id = ?", (fid,)
    )
    bus._conn.commit()

    out_dir = tmp_astor_home / 'export_out2'
    export_user_facts(bus, tier='private', user_id='admin', out_dir=out_dir)
    files = list(out_dir.glob('*.md'))
    assert len(files) == 1
    content = files[0].read_text(encoding='utf-8')
    assert 'status: inactive' in content
    # Body has the explicit ⚠ INACTIVE callout
    assert 'INACTIVE' in content or 'inactive' in content.lower()