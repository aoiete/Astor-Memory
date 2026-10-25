"""tests/test_hermes_install.py — the Hermes installer must do what the docs say.

docs/agent-adapters.md lines 682-693 promise four things for
`am install --ide=hermes --mode=priority`:

    1. creates plugins/memory/astor_memory/ with __init__.py
    2. writes plugins/memory/astor_memory/plugin.yaml
    3. edits config.yaml: memory.provider, and (priority only)
       memory_enabled / user_profile_enabled
    4. points at `am doctor --check-priority`

Until v1.16.99 only half of step 3 happened and steps 1 and 2 never
did, so the install reported success while leaving no plugin behind.

These tests pin the behaviour against a throwaway home directory, and
they pin the two guards: an existing plugin is never overwritten, and
config.yaml is backed up before it is edited.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from astor_memory.installer import handlers


CONFIG = """\
model:
  provider: custom:minimax-global
memory:
  memory_enabled: true
  user_profile_enabled: true
  memory_char_limit: 80000
  provider: auto
plugins:
  enabled: true
"""


def _write_config(home: Path, body: str = CONFIG) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    cfg = home / "config.yaml"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def _home(tmp_path: Path, *, bundled: bool = True) -> Path:
    """A throwaway hermes home. `bundled` fakes a hermes-agent tree."""
    home = tmp_path / "hermes"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(CONFIG, encoding="utf-8")
    if bundled:
        (home / "hermes-agent" / "plugins" / "memory").mkdir(parents=True)
    return home


def _apply(changes: list[dict]) -> None:
    """The installer returns a change list; the caller writes it."""
    for ch in changes:
        p = Path(ch["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(ch["content"], encoding="utf-8")


def test_installs_the_plugin_and_writes_plugin_yaml(tmp_path):
    """With a hermes-agent tree present the plugin goes into the bundled
    location, which hermes imports in-process."""
    home = _home(tmp_path)
    cfg = home / "config.yaml"

    res = handlers.astor_install_hermes(home, "priority")
    assert res["agent"] == "hermes"
    _apply(res["changes"])

    plugin = home / "hermes-agent" / "plugins" / "memory" / "astor_memory"
    assert (plugin / "__init__.py").exists(), "plugin __init__.py was not written"
    assert (plugin / "plugin.yaml").exists(), "plugin.yaml was not written"

    src = (plugin / "__init__.py").read_text(encoding="utf-8")
    assert "AstorMemoryProvider" in src
    # The hermes loader looks for a MemoryProvider subclass.
    assert "MemoryProvider" in src

    y = (plugin / "plugin.yaml").read_text(encoding="utf-8")
    assert "name: astor_memory" in y
    assert "config_schema" in y


def test_config_gets_provider_and_priority_disables_builtin(tmp_path):
    home = _home(tmp_path)
    cfg = home / "config.yaml"

    res = handlers.astor_install_hermes(home, "priority")
    _apply(res["changes"])

    text = cfg.read_text(encoding="utf-8")
    assert "provider: astor_memory" in text
    assert "memory_enabled: false" in text
    assert "user_profile_enabled: false" in text
    # Everything else in the file survives.
    assert "memory_char_limit: 80000" in text
    assert "custom:minimax-global" in text


def test_coexist_mode_leaves_the_builtin_store_on(tmp_path):
    home = _home(tmp_path)
    cfg = home / "config.yaml"

    res = handlers.astor_install_hermes(home, "coexist")
    _apply(res["changes"])

    text = cfg.read_text(encoding="utf-8")
    assert "provider: astor_memory" in text
    assert "memory_enabled: true" in text
    assert "user_profile_enabled: false" not in text


def test_plugin_falls_back_to_the_user_directory(tmp_path):
    """Without a hermes-agent tree the plugin goes to <home>/plugins/.

    Hermes imports user plugins in-process unless plugins.isolation is
    set to "host", so the adapter still resolves agent.memory_provider
    there -- which is what the MemoryProvider subclass check needs.
    """
    home = _home(tmp_path, bundled=False)
    res = handlers.astor_install_hermes(home, "priority")
    _apply(res["changes"])

    plugin = home / "plugins" / "astor_memory"
    assert (plugin / "__init__.py").exists()
    assert (plugin / "plugin.yaml").exists()


def test_existing_plugin_is_never_overwritten(tmp_path):
    home = _home(tmp_path)
    plugin = home / "hermes-agent" / "plugins" / "memory" / "astor_memory"
    plugin.mkdir(parents=True)
    hand_edited = "# hand-edited by the operator\nclass AstorMemoryProvider: pass\n"
    (plugin / "__init__.py").write_text(hand_edited, encoding="utf-8")

    res = handlers.astor_install_hermes(home, "priority")
    _apply(res["changes"])

    assert (plugin / "__init__.py").read_text(encoding="utf-8") == hand_edited
    assert any("never overwritten" in n for n in res["notes"])


def test_config_is_backed_up_before_editing(tmp_path):
    home = _home(tmp_path)
    cfg = home / "config.yaml"
    original = cfg.read_text(encoding="utf-8")

    res = handlers.astor_install_hermes(home, "priority")
    _apply(res["changes"])

    backups = list(home.glob("config.yaml.bak*"))
    assert backups, "config.yaml was edited without a backup"
    assert backups[0].read_text(encoding="utf-8") == original


def test_install_is_idempotent(tmp_path):
    home = _home(tmp_path)
    cfg = home / "config.yaml"

    first = handlers.astor_install_hermes(home, "priority")
    _apply(first["changes"])
    after_first = cfg.read_text(encoding="utf-8")

    second = handlers.astor_install_hermes(home, "priority")
    _apply(second["changes"])
    after_second = cfg.read_text(encoding="utf-8")

    assert after_first == after_second
    # The second pass has nothing left to change in config.yaml.
    assert not [c for c in second["changes"] if c["path"].endswith("config.yaml")]


def test_replace_mode_is_refused(tmp_path):
    home = _home(tmp_path)
    res = handlers.astor_install_hermes(home, "replace")
    assert res["changes"] == []
    assert any("priority" in n for n in res["notes"])


def test_installed_plugin_is_valid_python(tmp_path):
    home = _home(tmp_path)
    res = handlers.astor_install_hermes(home, "priority")
    _apply(res["changes"])
    init = home / "hermes-agent" / "plugins" / "memory" / "astor_memory" / "__init__.py"
    ast.parse(init.read_text(encoding="utf-8"))


def test_plugin_yaml_is_valid_yaml(tmp_path):
    yaml = pytest.importorskip("yaml")
    home = _home(tmp_path)
    res = handlers.astor_install_hermes(home, "priority")
    _apply(res["changes"])
    p = home / "hermes-agent" / "plugins" / "memory" / "astor_memory" / "plugin.yaml"
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert doc["name"] == "astor_memory"
    assert "config_schema" in doc
