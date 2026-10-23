"""版本号一致性检查的测试 (v1.16.89).

The release version is declared in three places and read by three
different callers:

  - astor_memory.__version__           what /v1/health reports
  - pyproject.toml [project] version    what pip installs
  - pyproject.toml [tool.astor] version what the am CLI reports

A bump that touches two of them leaves the third stale, which is how a
running server ended up claiming a version no tag matched. The sync
helper now refuses to sync a tree whose declarations disagree.

These tests use a deliberately broken copy, because a check that has
never rejected anything has not been shown to work.
"""
import importlib.util
import re
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_sync():
    spec = importlib.util.spec_from_file_location(
        "sync_to_runtime_mod", ROOT / "scripts" / "sync_to_runtime.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def sync_mod():
    return _load_sync()


@pytest.fixture()
def tree(tmp_path):
    """A copy of the source tree with a mutable pyproject and __init__."""
    shutil.copytree(ROOT / "astor_memory", tmp_path / "astor_memory",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    return tmp_path


def _versions_in(tree_dir: Path) -> dict:
    versions = {}
    init_py = tree_dir / "astor_memory" / "__init__.py"
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']',
                  init_py.read_text(encoding="utf-8"))
    if m:
        versions["__init__"] = m.group(1)
    txt = (tree_dir / "pyproject.toml").read_text(encoding="utf-8")
    for section, key in (("project", "[project] version"),
                         ("tool.astor", "[tool.astor] version")):
        m = re.search(r"\[%s\](.*?)(?=\n\[|\Z)" % re.escape(section), txt, re.S)
        if m:
            m2 = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', m.group(1), re.M)
            if m2:
                versions[key] = m2.group(1)
    return versions


def test_real_tree_is_consistent(sync_mod):
    """The shipped tree declares one version in all three places."""
    versions = _versions_in(ROOT)
    assert len(set(versions.values())) == 1, f"version sites disagree: {versions}"
    assert sync_mod._check_version_consistency(ROOT) is True


def test_mismatch_is_rejected(sync_mod, tree):
    """A tree whose sites disagree must be rejected."""
    pt = tree / "pyproject.toml"
    txt = pt.read_text(encoding="utf-8")
    cur = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']',
                    (tree / "astor_memory" / "__init__.py").read_text(encoding="utf-8"))
    assert cur, "could not read the current version"
    pt.write_text(txt.replace(f'version = "{cur.group(1)}"',
                              'version = "0.0.1"', 1), encoding="utf-8")
    assert sync_mod._check_version_consistency(tree) is False


def test_stale_tool_astor_section_is_rejected(sync_mod, tree):
    """The [tool.astor] section is a separate site and must be checked."""
    pt = tree / "pyproject.toml"
    txt = pt.read_text(encoding="utf-8")
    cur = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']',
                    (tree / "astor_memory" / "__init__.py").read_text(encoding="utf-8"))
    v = cur.group(1)
    # change only the [tool.astor] occurrence
    head, sep, tail = txt.partition("[tool.astor]")
    assert sep, "pyproject has no [tool.astor] section"
    tail = tail.replace(f'version = "{v}"', 'version = "9.9.9"', 1)
    pt.write_text(head + sep + tail, encoding="utf-8")
    assert sync_mod._check_version_consistency(tree) is False


def test_missing_sections_do_not_crash(sync_mod, tmp_path):
    """A tree missing the files must not raise."""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert sync_mod._check_version_consistency(empty) is True