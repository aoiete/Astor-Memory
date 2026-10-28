"""The sync helper must say when it is copying onto itself.

An editable install (`pip install -e .`) points the package at the
checkout. A server started from that checkout is already running the
source code, so copying the source onto a runtime tree creates a second
copy of the package that nothing imports -- and the operator is left
believing the runtime tree is what the server runs, which is no longer
true.

The copy is not harmless: it is a second place to change the code and
a second thing to forget to sync. The helper says so rather than
quietly making the trees drift.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def sync_mod():
    spec = importlib.util.spec_from_file_location(
        "sync_to_runtime_mod", ROOT / "scripts" / "sync_to_runtime.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_editable_install_is_detected(sync_mod):
    """In this repo's own test environment, astor_memory is editable."""
    import astor_memory
    mod_path = Path(astor_memory.__file__).resolve()
    src = ROOT.resolve()
    assert mod_path.is_relative_to(src), (
        "this test assumes astor_memory is installed editable from the "
        "repo; got %s" % mod_path)
    assert sync_mod._is_editable_install(ROOT) is True


def test_a_tree_that_is_not_the_source_is_not_editable(sync_mod, tmp_path):
    assert sync_mod._is_editable_install(tmp_path) is False


def test_the_helper_warns_about_the_contradiction(sync_mod, tmp_path, capsys):
    """Running the helper against an editable install prints a warning."""
    import sys
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    old = sys.argv
    sys.argv = ["sync_to_runtime.py", "--src", str(ROOT),
                "--runtime", str(runtime)]
    try:
        sync_mod.main()
    except SystemExit:
        pass
    finally:
        sys.argv = old
    out = capsys.readouterr().out
    assert "editable" in out.lower(), (
        "no warning about the editable install was printed:\n%s" % out)
