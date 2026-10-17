"""
Per-agent install handlers.

Each handler implements:
- verify(agent_dir) -> dict: probe agent config + report
- install(agent_dir, mode) -> dict: install per mode (writes/returns files)

For v0.2, handlers return the planned changes (write_file paths + contents)
rather than writing directly — caller decides. This makes the install
dry-run-able and testable without filesystem side-effects.
"""
from __future__ import annotations

import json
from pathlib import Path
from .registry import (
    PRIORITY_MARKER,
    astor_get_agent_tier,
    astor_supports_mode,
    astor_verify_agent,
)


def _result(agent: str, mode: str, changes: list[dict], notes: list[str] | None = None) -> dict:
    """Standardized install result."""
    return {
        'agent': agent,
        'mode': mode,
        'tier': astor_get_agent_tier(agent),
        'changes': changes,  # [{path, action: 'create'|'patch'|'append', content}, ...]
        'notes': notes or [],
        'requires_restart': True,
    }


def astor_install_claude_code(agent_dir: Path, mode: str) -> dict:
    """Claude Code: Tier A — uses --append-system-prompt slot (wrap CLI).

    For v0.2, we plan the patch: write a wrapper script at
    `~/.claude/astor_wrapper.sh` that injects the priority marker into
    `--append-system-prompt`. User runs `claude` via this wrapper.
    """
    changes = []
    notes = []
    wrapper = agent_dir / 'astor_wrapper.sh'
    content = (
        "#!/bin/bash\n"
        "# Wrapper that injects Astor-Memory priority marker into Claude Code\n"
        f"exec claude --append-system-prompt \"$(cat <<'EOF'\n{PRIORITY_MARKER}\nEOF\n)\" \"$@\"\n"
    )
    changes.append({'path': str(wrapper), 'action': 'create', 'content': content, 'executable': True})
    if mode == 'priority':
        notes.append('Claude Code priority: wrapper script. Use `astor_wrapper.sh` instead of `claude`.')
    elif mode == 'coexist':
        notes.append('Coexist mode: priority marker injected via wrapper. Native memory still loads.')
    elif mode == 'replace':
        notes.append('Replace mode: wrapper adds --append-system-prompt + CLAUDE.md memory section.')
        # Also write CLAUDE.md override
        claude_md = agent_dir / 'CLAUDE.md'
        changes.append({'path': str(claude_md), 'action': 'append', 'content': f'\n{PRIORITY_MARKER}\n'})
    return _result('claude-code', mode, changes, notes)


def astor_install_cline(agent_dir: Path, mode: str) -> dict:
    """Cline: Tier A — Hooks + .clinerules/00-astor.md (lexical priority)."""
    changes = []
    notes = []
    rules_file = agent_dir / '.clinerules' / '00-astor.md'
    rules_file.parent.mkdir(parents=True, exist_ok=True)
    changes.append({'path': str(rules_file), 'action': 'create', 'content': PRIORITY_MARKER})
    if mode == 'priority':
        # Also write PreToolUse hook config
        hooks_file = agent_dir / '.clinerules' / 'hooks.json'
        changes.append({'path': str(hooks_file), 'action': 'create', 'content':
            '{\n  "PreToolUse": [\n    {"command": "am read \\"$QUERY\\""}\n  ]\n}\n'})
        notes.append('Cline priority: 00-astor.md + PreToolUse hook. Hooks run before every tool call.')
    return _result('cline', mode, changes, notes)


def astor_install_opencode(agent_dir: Path, mode: str) -> dict:
    """OpenCode: Tier A — opencode.json instructions array (first-class)."""
    changes = []
    notes = []
    config_file = agent_dir / 'opencode.json'
    snippet = (
        "{\n"
        "  \"instructions\": [\n"
        # PRIORITY_MARKER already starts with "## Memory source priority";
        # prefixing another heading produced "# Memory source
        # priority\n## Memory source priority" in opencode.json.
        f"    {json.dumps(PRIORITY_MARKER)}\n"
        "  ]\n"
        "}\n"
    )
    changes.append({'path': str(config_file), 'action': 'patch_or_create', 'content': snippet})
    notes.append('OpenCode priority: instructions array takes precedence over conversation.')
    return _result('opencode', mode, changes, notes)


def astor_install_hermes(agent_dir: Path, mode: str) -> dict:
    """Hermes 0.20+: Tier B -- select astor_memory as the memory provider.

    v1.16.77 (2026-10-16): this handler used to emit "patch
    system_prompt.py lines 483-499 (5-line change)" and write no files
    at all. That instruction is wrong: docs/agent-adapters.md recorded on
    2026-08-15 that it does not match hermes-agent 0.20.0 source
    (checked against commit 1649a4d7e) and removed it. Following the old
    note left the user with an install that wrote nothing and pointed at
    the wrong file.

    What actually takes effect is `memory.provider: astor_memory` in
    config.yaml. The hook entries under the same file are per-user
    wiring and are not safe to generate blindly, so they stay manual.

    Rewrites config.yaml in place (surgical, idempotent) and reports
    what it changed. Does not touch anything else in the file.
    """
    import re as _re

    if mode == 'replace':
        return _result(
            'hermes', mode, [],
            ['Hermes does not support replace mode -- its own '
             'memory_store is hardcoded. Use priority mode instead.'])

    notes: list[str] = []
    changes: list[dict] = []

    cfg = agent_dir / 'config.yaml'
    if not cfg.exists():
        cfg = Path.home() / '.hermes' / 'config.yaml'
    if not cfg.exists():
        return _result('hermes', mode, [], [
            f'Could not find config.yaml (looked in {agent_dir} and '
            f'~/.hermes). Set memory.provider: astor_memory manually, '
            f'and make sure astor_memory is importable by the hermes '
            f'venv (pip install -e . or PYTHONPATH).',
            'Do NOT patch agent/system_prompt.py -- that recipe was '
            'removed in docs/agent-adapters.md (2026-08-15) after being '
            'found not to match hermes-agent 0.20.0.',
        ])

    text = cfg.read_text(encoding='utf-8')
    m = _re.search(r'^(memory:\s*\n(?:[ \t]+\S.*\n)*)', text, _re.M)
    if not m:
        return _result('hermes', mode, [], [
            f'No top-level `memory:` block found in {cfg}. Add one '
            f'containing `provider: astor_memory`, or run with an agent '
            f'config that has a memory section.',
            'Do NOT patch agent/system_prompt.py -- that recipe was '
            'removed in docs/agent-adapters.md (2026-08-15).',
        ])

    block = m.group(1)
    pm = _re.search(r'^([ \t]+)provider:[ \t]*\S+', block, _re.M)
    if pm and pm.group(0).split(':', 1)[1].strip() == 'astor_memory':
        notes.append(f'{cfg} already selects provider: astor_memory '
                     '-- nothing to change.')
    elif pm:
        indent = pm.group(1)
        new_block = block[:pm.start()] + f'{indent}provider: astor_memory'
        new_block += block[pm.end():]
        changes.append({
            'path': str(cfg), 'action': 'patch',
            'content': text[:m.start(1)] + new_block + text[m.end(1):],
        })
        notes.append(f'{cfg}: provider switched from '
                     f'{pm.group(0).split(":", 1)[1].strip()} to '
                     f'astor_memory.')
    else:
        indent = _re.match(r'^(memory:\s*\n)([ \t]+)', block, _re.M)
        pad = indent.group(2) if indent else '  '
        new_block = block + f'{pad}provider: astor_memory\n'
        changes.append({
            'path': str(cfg), 'action': 'patch',
            'content': text[:m.start(1)] + new_block + text[m.end(1):],
        })
        notes.append(f'{cfg}: added `provider: astor_memory` to the '
                     f'memory block.')

    notes.append('Restart the hermes gateway afterwards (R218: never '
                 'restart it for the user).')
    notes.append('Autorecord hooks (config.yaml hooks:) are per-user '
                 'wiring and are NOT generated here -- add them by hand '
                 'if you want conversation capture.')
    return _result('hermes', mode, changes, notes)


def astor_install_openclaw(agent_dir: Path, mode: str) -> dict:
    """OpenClaw: Tier B — patch workspace startup_script via plugins.slots.contextEngine."""
    if mode == 'replace':
        return _result('openclaw', mode, [], ['OpenClaw does not support replace mode.'])
    changes = []
    notes = []
    target = agent_dir / '.openclaw' / 'openclaw.json'
    notes.append(f'OpenClaw priority: patch {target} plugins.slots.contextEngine = "astor_memory".')
    notes.append('Plugin path: write astor_openclaw_plugin to ~/.openclaw/plugins/astor_memory/.')
    return _result('openclaw', mode, changes, notes)


def astor_install_cursor(agent_dir: Path, mode: str) -> dict:
    """Cursor: Tier C — lexical ordering only. Write 00-astor.md."""
    changes = []
    notes = []
    rules_file = agent_dir / '.cursor' / 'rules' / '00-astor.md'
    rules_file.parent.mkdir(parents=True, exist_ok=True)
    changes.append({'path': str(rules_file), 'action': 'create', 'content': PRIORITY_MARKER})
    notes.append('Cursor: lexical ordering (00- prefix loads first) but NOT a real priority hook. Use coexist mode.')
    return _result('cursor', mode, changes, notes)


def astor_install_continue(agent_dir: Path, mode: str) -> dict:
    """Continue.dev: Tier C — same as Cursor (lexical via 00- prefix)."""
    changes = []
    notes = []
    rules_file = agent_dir / '.continue' / 'rules' / '00-astor.md'
    rules_file.parent.mkdir(parents=True, exist_ok=True)
    changes.append({'path': str(rules_file), 'action': 'create', 'content': PRIORITY_MARKER})
    notes.append('Continue.dev: lexical ordering only. No real priority hook.')
    return _result('continue', mode, changes, notes)


def astor_install_windsurf(agent_dir: Path, mode: str) -> dict:
    """Windsurf: Tier C — same as Cursor/Continue."""
    changes = []
    notes = []
    rules_file = agent_dir / '.windsurf' / 'rules' / '00-astor.md'
    rules_file.parent.mkdir(parents=True, exist_ok=True)
    changes.append({'path': str(rules_file), 'action': 'create', 'content': PRIORITY_MARKER})
    notes.append('Windsurf: lexical ordering only.')
    return _result('windsurf', mode, changes, notes)


def astor_install_aider(agent_dir: Path, mode: str) -> dict:
    """Aider: Tier C — .aider.conf.yml read list (no priority mechanism)."""
    changes = []
    notes = []
    conf_file = agent_dir / '.aider.conf.yml'
    snippet = (
        f"read:\n"
        f"  - ~/.astor/PRIORITY_MARKER.md\n"
    )
    changes.append({'path': str(conf_file), 'action': 'patch_or_create', 'content': snippet})
    notes.append('Aider: read-list inclusion. No real priority over conversation.')
    notes.append('Also write PRIORITY_MARKER.md to ~/.astor/ for Aider to load.')
    return _result('aider', mode, changes, notes)


# Dispatch table
INSTALLERS: dict[str, callable] = {
    'claude-code': astor_install_claude_code,
    'cline': astor_install_cline,
    'opencode': astor_install_opencode,
    'hermes': astor_install_hermes,
    'openclaw': astor_install_openclaw,
    'cursor': astor_install_cursor,
    'continue': astor_install_continue,
    'windsurf': astor_install_windsurf,
    'aider': astor_install_aider,
}


def astor_install(agent_id: str, agent_dir: Path, mode: str = 'auto') -> dict:
    """Dispatch to the right installer.

    Per Plan § Insight 18 default behavior:
    - mode='auto' → run verify first → ask user which mode → install
    - mode='verify' → return capability report (no install)
    - mode=priority|coexist|replace → install if supported, else fallback to coexist + warn
    """
    if mode == 'verify':
        return astor_verify_agent(agent_id)

    handler = INSTALLERS.get(agent_id)
    if handler is None:
        return {'error': f'Unknown agent: {agent_id}', 'supported_agents': list(INSTALLERS.keys())}

    if mode == 'auto':
        # Default: recommend mode + plan install (no execute in v0.2)
        report = astor_verify_agent(agent_id)
        if not report['supported']:
            return {'error': f'Agent {agent_id} is not supported (Tier D)', 'report': report}
        mode = report['recommended_mode']

    if not astor_supports_mode(agent_id, mode):
        # Fallback: priority → coexist with note
        return {
            'agent': agent_id,
            'mode_requested': mode,
            'mode_actual': 'coexist',
            'fallback': True,
            'note': f'Agent {agent_id} does not support {mode}. Falling back to coexist.',
            'result': handler(agent_dir, 'coexist'),
        }

    return handler(agent_dir, mode)


__all__ = ['INSTALLERS', 'astor_install']
