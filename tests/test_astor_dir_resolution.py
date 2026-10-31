"""Where the data directory is, and how that decision is made.

Two directories, and only two: the code you installed and the data it
writes. The data directory has to be resolvable the same way from every
command, or a backup that looked at one path would miss the databases
another command wrote to.

This pins the resolution order, because it is where a rename goes
wrong. An install that predates the rename must keep its data where it
is -- the directory name is cosmetic, the data is not -- and a fresh
install must land on the new default rather than on an empty leftover
directory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from astor_memory import config


@pytest.fixture()
def clean_env(monkeypatch, tmp_path):
    """No ASTOR_DIR, no ASTOR_DIR_NAME, a home of our own."""
    monkeypatch.delenv("ASTOR_DIR", raising=False)
    monkeypatch.delenv("ASTOR_DIR_NAME", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("ASTOR_HOME", str(home))
    return home


def _source(tmp_path, monkeypatch):
    """A checkout with no runtime next to it."""
    src = tmp_path / "Astor-Code"
    src.mkdir()
    monkeypatch.setattr(config, "_source_dir", lambda: src)
    return src


def test_env_wins_over_everything(clean_env, tmp_path, monkeypatch):
    _source(tmp_path, monkeypatch)
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path / "explicit"))
    assert config.get_default_astor_dir() == tmp_path / "explicit"


def test_env_name_is_honoured(clean_env, tmp_path, monkeypatch):
    _source(tmp_path, monkeypatch)
    monkeypatch.setenv("ASTOR_DIR_NAME", "somewhere")
    assert config.get_default_astor_dir() == clean_env / "somewhere"


def test_fresh_install_lands_on_the_new_default(clean_env, tmp_path, monkeypatch):
    src = _source(tmp_path, monkeypatch)
    assert config.get_default_astor_dir() == src.parent / "Astor-Data"


def test_install_record_wins_over_the_default(clean_env, tmp_path, monkeypatch):
    src = _source(tmp_path, monkeypatch)
    saved = tmp_path / "chosen"
    record = clean_env / ".astor" / "install.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"astor_dir": str(saved)}), encoding="utf-8")
    assert config.get_default_astor_dir() == saved


def test_a_legacy_install_keeps_its_data(clean_env, tmp_path, monkeypatch):
    """The directory name is cosmetic; the data is not.

    An install that predates the rename has its data under the old
    name. A rename must not make that data unreachable.
    """
    src = _source(tmp_path, monkeypatch)
    legacy = src.parent / "Astor-Memory-Runtime"
    legacy.mkdir()
    assert config.get_default_astor_dir() == legacy


def test_a_legacy_home_install_keeps_its_data(clean_env, tmp_path, monkeypatch):
    _source(tmp_path, monkeypatch)
    legacy = clean_env / ".astor"
    legacy.mkdir()
    (legacy / "astor_bus.db").write_bytes(b"")
    assert config.get_default_astor_dir() == legacy


def test_an_empty_legacy_home_is_not_treated_as_an_install(
        clean_env, tmp_path, monkeypatch):
    """`am init` leaves an install record behind; that is not data.

    The record alone must not pull a fresh install back to the old
    location.
    """
    src = _source(tmp_path, monkeypatch)
    legacy = clean_env / ".astor"
    legacy.mkdir()
    (legacy / "install.json").write_text("{}", encoding="utf-8")
    assert config.get_default_astor_dir() == src.parent / "Astor-Data"


def test_the_two_resolvers_agree(clean_env, tmp_path, monkeypatch):
    """One rule, not two.

    The server resolved the data directory through one function and the
    CLI through another; they could disagree, and a backup that read
    one path would miss what the other wrote.
    """
    _source(tmp_path, monkeypatch)
    import sys
    from astor_memory._internal.acl_layout import get_astor_dir
    assert sys.modules.get("astor_memory.config") is config, (
        "two copies of astor_memory.config are loaded -- the resolver "
        "the server uses and the one the CLI uses are different modules")
    assert get_astor_dir() == config.get_default_astor_dir()
