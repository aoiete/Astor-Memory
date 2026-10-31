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
import re
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


def _hermes_plugin_candidates(agent_dir: Path) -> list[Path]:
    """Where a Hermes memory provider plugin may live, best first.

    Hermes discovers memory providers in this order (plugins/memory/
    __init__.py::_iter_provider_dirs): bundled plugins under
    ``<hermes home>/hermes-agent/plugins/memory/``, then user plugins
    under ``<hermes home>/plugins/``, then project-local ones.

    Bundled comes first on purpose. A plugin outside the bundled tree
    is imported inside an isolated host (plugins/plugin_loader.py
    refuses in-process imports for user code), and the adapter needs
    ``agent.memory_provider`` from the running hermes. Inside the
    bundled tree it is imported in-process, so the ABC resolves.
    """
    return [
        agent_dir / 'hermes-agent' / 'plugins' / 'memory' / 'astor_memory',
        agent_dir / 'plugins' / 'astor_memory',
    ]


def _hermes_plugin_yaml() -> str:
    from astor_memory import __version__ as _v
    return (
        'name: astor_memory\n'
        'version: %s\n'
        'description: "Astor Memory - 3-tier x 3-store SQLite memory system '
        'with hybrid recall (vector + BM25 + jaccard). Replaces built-in '
        'MEMORY.md / USER.md when set as memory.provider. Requires the '
        'astor_memory server (default http://127.0.0.1:7803)."\n'
        'config_schema:\n'
        '  astor_dir:\n'
        '    type: string\n'
        '    default: "<source>/../Astor-Data"\n'
        '  default_tier:\n'
        '    type: string\n'
        '    enum: ["public", "source", "private"]\n'
        '    default: "public"\n' % _v
    )


def _hermes_python_version(agent_dir: Path) -> str | None:
    """The Python version the Hermes install runs on, as '3.14'.

    Hermes ships its own interpreter, so the version that matters is
    the one in the hermes venv, not the one running this installer.
    """
    for rel in (('.venv', 'Scripts', 'python.exe'),
                ('hermes-agent', '.venv', 'Scripts', 'python.exe')):
        py = agent_dir.joinpath(*rel)
        if not py.exists():
            continue
        try:
            import subprocess
            out = subprocess.run([str(py), '-c',
                                  'import sys; print("%d.%d" % sys.version_info[:2])'],
                                 capture_output=True, text=True, timeout=20)
            v = (out.stdout or '').strip()
            if re.match(r'^\d+\.\d+$', v):
                return v
        except Exception:
            continue
    return None


def _requires_python_range() -> str | None:
    """The requires-python spec astor-memory declares, e.g. '>=3.10,<3.14'."""
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib
        except Exception:
            return None
    try:
        root = Path(__file__).resolve().parents[2]
        data = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))
        return (data.get('project') or {}).get('requires-python')
    except Exception:
        return None


def _python_in_range(version: str, spec: str) -> bool:
    """Does `version` satisfy a requires-python spec such as '>=3.10,<3.14'?

    Only the comparators packaging understands are handled: >=, >, <=,
    <, ==, !=. Anything else is treated as satisfied -- refusing to
    install on a spec we cannot parse would be worse than trying.
    """
    try:
        major, minor = (int(x) for x in version.split('.')[:2])
    except Exception:
        return True
    cur = (major, minor)
    for part in (spec or '').split(','):
        part = part.strip()
        if not part:
            continue
        m = re.match(r'^(>=|<=|==|!=|>|<)\s*(\d+)(?:\.(\d+))?$', part)
        if not m:
            continue
        op = m.group(1)
        want = (int(m.group(2)), int(m.group(3) or 0))
        if op == '>=' and not cur >= want:
            return False
        if op == '>' and not cur > want:
            return False
        if op == '<=' and not cur <= want:
            return False
        if op == '<' and not cur < want:
            return False
        if op == '==' and cur != want:
            return False
        if op == '!=' and cur == want:
            return False
    return True


def astor_install_hermes(agent_dir: Path, mode: str) -> dict:
    """Hermes 0.20+: Tier B -- install the provider plugin and select it.

    What the docs promised and this handler used to skip
    (docs/agent-adapters.md lines 682-693): create the plugin directory,
    drop the adapter in as __init__.py, write plugin.yaml, and edit
    config.yaml. Until v1.16.99 only the config.yaml half happened, so
    ``hermes memory status`` showed the provider selected while no
    plugin existed -- the install looked done and was not.

    Two guards, because a provider plugin is code that runs:

    - An existing plugin is never overwritten. Operators hand-edit
      these (this repo's own installation carries a local user-field
      extension), and silently replacing a working plugin with the
      packaged copy is a downgrade, not an install.
    - config.yaml is backed up before the first edit.

    In priority mode the built-in file memory is switched off
    (memory_enabled / user_profile_enabled), which is the point of
    priority mode: one memory system, not two. In coexist mode the
    built-in store keeps running alongside astor.
    """
    import re as _re
    import shutil as _shutil

    if mode == 'replace':
        return _result(
            'hermes', mode, [],
            ['Hermes does not support replace mode -- its own '
             'memory_store is hardcoded. Use priority mode instead.'])

    notes: list[str] = []
    changes: list[dict] = []

    # --- 1. the plugin itself -------------------------------------------
    adapter = Path(__file__).resolve().parents[1] / 'hermes_adapter.py'
    if not adapter.exists():
        notes.append('Could not find hermes_adapter.py next to this '
                     'package -- the plugin was not installed. The '
                     'config.yaml change below still applies.')
    else:
        # v1.17.3 (2026-10-27): hermes_adapter.py imports astor_memory
        # directly (astor_memory._internal.acl, astor_memory.nest.*), so
        # the plugin only works if astor_memory is importable by the
        # interpreter that runs hermes. astor-memory declares
        # requires-python >=3.10,<3.14; Hermes 0.22 ships on Python 3.14.
        # Installing the direct-import adapter there produces a plugin
        # that fails to import, and `hermes memory status` reports the
        # provider as unavailable -- an install that looks done and is not.
        #
        # The HTTP plugin (docs/agent_plugin_template/frameworks/hermes)
        # talks to the running server over REST and has no astor_memory
        # dependency at all, so it works on any Python. Use it when the
        # target interpreter is out of range, and say so.
        _http_tpl = (Path(__file__).resolve().parents[2] / 'docs'
                     / 'agent_plugin_template' / 'frameworks' / 'hermes'
                     / '__init__.py')
        _adapter_src = adapter
        _py_ver = _hermes_python_version(agent_dir)
        _needs_python = _requires_python_range()
        if _py_ver and _needs_python and not _python_in_range(_py_ver, _needs_python):
            if _http_tpl.exists():
                _adapter_src = _http_tpl
                notes.append(
                    'Hermes runs on Python %s, outside astor-memory\'s '
                    'supported range %s. Installed the HTTP plugin instead '
                    'of the direct-import adapter -- it talks to the '
                    'running astor server over REST and needs no '
                    'astor_memory install.' % (_py_ver, _needs_python))
            else:
                notes.append(
                    'Hermes runs on Python %s, outside astor-memory\'s '
                    'supported range %s, and the HTTP plugin template was '
                    'not found. The direct-import adapter was installed '
                    'anyway and will fail to import there. Provide your own '
                    'HTTP adapter under <hermes home>/plugins/ instead.'
                    % (_py_ver, _needs_python))
        cands = _hermes_plugin_candidates(agent_dir)
        target = next((c for c in cands if (c / '__init__.py').exists()), None)
        if target is None:
            # Prefer the bundled tree, but only if it is really there.
            bundled = cands[0]
            target = bundled if bundled.parent.is_dir() else cands[1]
            init_dst = target / '__init__.py'
            yaml_dst = target / 'plugin.yaml'
            if init_dst.exists():
                notes.append('%s already exists -- left untouched.' % init_dst)
            else:
                changes.append({
                    'path': str(init_dst), 'action': 'write',
                    'content': _adapter_src.read_text(encoding='utf-8'),
                })
                notes.append('Installed the provider plugin at %s.' % target)
            if yaml_dst.exists():
                notes.append('%s already exists -- left untouched.' % yaml_dst)
            else:
                changes.append({
                    'path': str(yaml_dst), 'action': 'write',
                    'content': _hermes_plugin_yaml(),
                })
        else:
            init_dst = target / '__init__.py'
            yaml_dst = target / 'plugin.yaml'
            notes.append('Provider plugin already present at %s -- '
                         'nothing written there (a hand-edited plugin is '
                         'never overwritten).' % target)
            if not yaml_dst.exists():
                changes.append({
                    'path': str(yaml_dst), 'action': 'write',
                    'content': _hermes_plugin_yaml(),
                })
                notes.append('Wrote the missing %s.' % yaml_dst)
        if target is not None and target.parent.name == 'memory':
            notes.append('The plugin lives inside the hermes-agent source '
                         'tree, so a hermes upgrade may overwrite it. '
                         'Re-run `am install --ide=hermes` afterwards.')
        elif target is not None:
            notes.append('The plugin lives under <hermes home>/plugins/. '
                         'Hermes imports user plugins in-process unless '
                         'plugins.isolation is set to "host"; under "host" '
                         'the adapter cannot import agent.memory_provider '
                         'and will not register.')

    # --- 2. config.yaml -------------------------------------------------
    cfg = agent_dir / 'config.yaml'
    if not cfg.exists():
        cfg = Path.home() / '.hermes' / 'config.yaml'
    if not cfg.exists():
        return _result('hermes', mode, changes, notes + [
            f'Could not find config.yaml (looked in {agent_dir} and '
            f'~/.hermes). Set memory.provider: astor_memory manually, '
            f'and make sure astor_memory is importable by the hermes '
            f'venv (pip install -e . or PYTHONPATH).',
        ])

    text = cfg.read_text(encoding='utf-8')
    # The block is every indented line under `memory:`. The last one may
    # have no trailing newline, and it must still belong to the block:
    # leaving it out makes the appended keys land ABOVE it, which puts
    # two `provider:` keys in one map and makes the install run again
    # on every call.
    m = _re.search(r'^(memory:[ \t]*\n)((?:[ \t]+[^\n]*\n?)*)', text, _re.M)
    if not m:
        return _result('hermes', mode, changes, notes + [
            f'No top-level `memory:` block found in {cfg}. Add one '
            f'containing `provider: astor_memory`, or run with an agent '
            f'config that has a memory section.',
        ])

    block = m.group(2)
    new_block = block
    pm = _re.search(r'^([ \t]+)provider:[ \t]*\S+', block, _re.M)
    if pm and pm.group(0).split(':', 1)[1].strip() == 'astor_memory':
        notes.append(f'{cfg} already selects provider: astor_memory.')
    elif pm:
        indent = pm.group(1)
        new_block = (block[:pm.start()] + f'{indent}provider: astor_memory'
                     + block[pm.end():])
        notes.append(f'{cfg}: provider switched from '
                     f'{pm.group(0).split(":", 1)[1].strip()} to '
                     f'astor_memory.')
    else:
        indent = _re.match(r'^(memory:[ \t]*\n)([ \t]+)', block, _re.M)
        pad = indent.group(2) if indent else '  '
        tail = '' if block.endswith('\n') else '\n'
        new_block = block + tail + f'{pad}provider: astor_memory\n'
        notes.append(f'{cfg}: added `provider: astor_memory` to the '
                     f'memory block.')

    # Priority mode hands memory over to astor: the built-in file store
    # must stop injecting MEMORY.md / USER.md or the agent sees two
    # memories. Coexist mode leaves both on.
    if mode == 'priority':
        for key in ('memory_enabled', 'user_profile_enabled'):
            km = _re.search(r'^([ \t]+)%s:[ \t]*\S+' % key, new_block, _re.M)
            if km:
                if km.group(0).split(':', 1)[1].strip() == 'false':
                    continue
                indent = km.group(1)
                new_block = (new_block[:km.start()]
                             + f'{indent}{key}: false' + new_block[km.end():])
            else:
                indent = _re.match(r'^(memory:[ \t]*\n)([ \t]+)', new_block, _re.M)
                pad = indent.group(2) if indent else '  '
                tail = '' if new_block.endswith('\n') else '\n'
                new_block = new_block + tail + f'{pad}{key}: false\n'
        notes.append(f'{cfg}: priority mode disables the built-in file '
                     f'memory (memory_enabled / user_profile_enabled). '
                     f'Set them back to true to keep both.')

    if new_block != block:
        bak = cfg.with_suffix(cfg.suffix + '.bak-pre-astor-install')
        try:
            _shutil.copy2(cfg, bak)
            notes.append('Backed up config.yaml to %s.' % bak.name)
        except Exception as exc:
            notes.append('Could not back up config.yaml (%s).' % exc)
        changes.append({
            'path': str(cfg), 'action': 'patch',
            'content': text[:m.start(2)] + new_block + text[m.end(2):],
        })

    notes.append('Verify with `am doctor --check-priority`.')
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
