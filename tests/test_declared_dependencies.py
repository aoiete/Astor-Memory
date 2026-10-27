"""
Every declared dependency is actually reachable from a declared entry point.

Two undeclared runtime dependencies shipped in a row before this test existed:
`psutil`, which `am doctor` imported unconditionally while it lived only in the
dev extra, and `cryptography`, reached through a conditional import inside
peer_identity that no manifest backed. A clean install had neither, and the
failure surfaced as an ImportError in whichever command happened to need it.

Reading pyproject.toml is not enough to catch that class. A dependency can be
declared and still unused, used and still undeclared, and a package can be
listed in `packages` while not existing on disk. What catches it is building
an environment from the manifest alone and importing the real entry points
from it.

Two layers, cheapest first:

  1. Static — every module named by [project.scripts] and
     [project.entry-points."astor_memory.mcp_servers"] must be importable from
     the repo, and every directory named in the build `packages` list must
     exist. Catches the packaging half of the problem with no install.

  2. Dynamic — create a fresh virtualenv, install the project into it, then
     import each entry point and walk the package graph for third-party
     modules that the manifest does not provide. Skipped when the environment
     cannot be created (no network, no uv), and marked slow because it
     installs the world.
"""

from __future__ import annotations

# v1.16.83: pyproject declares requires-python >=3.10, so this module has to
# import a TOML parser that exists on 3.10 too. tomllib is stdlib only from
# 3.11; tomli is its exact backport and is declared in the dev extra with a
# python_version < '3.11' marker. Importing tomllib unconditionally at module
# scope broke collection on the 3.10 CI leg, and a collection error aborts the
# whole run rather than failing one test.
try:
    import tomllib as _toml
except ModuleNotFoundError:  # Python 3.10
    import tomli as _toml

import ast as pyast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _manifest() -> dict:
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        return _toml.load(fh)


def _entry_point_targets() -> list[tuple[str, str]]:
    """(entry point name, module) for every declared console script."""
    proj = _manifest()["project"]
    out = []
    for name, target in (proj.get("scripts") or {}).items():
        out.append((name, target.partition(":")[0]))
    return out


# --------------------------------------------------------------------------
# Layer 1: static checks
# --------------------------------------------------------------------------

def test_every_entry_point_module_exists() -> None:
    """`am` and `astor-mcp` must point at importable modules.

    Regression: astor-mcp pointed at astor_memory_mcp.server while the
    directory astor_memory_mcp/ was never in the repo, so a clean install
    created a console script that failed on first run with ModuleNotFoundError
    — and `packages` listed the same non-existent directory, so the build did
    not complain either.
    """
    missing = []
    for name, module in _entry_point_targets():
        try:
            __import__(module)
        except ImportError as exc:
            missing.append(f"{name} -> {module} ({exc})")
    assert not missing, "entry points that cannot be imported:\n  " + "\n  ".join(missing)


def test_declared_packages_exist_on_disk() -> None:
    """Every directory in the build `packages` list must be a real package."""
    build = _manifest().get("tool", {}).get("hatch", {}).get("build", {})
    declared: list[str] = []
    for _target, cfg in build.items():
        if isinstance(cfg, dict) and "packages" in cfg:
            declared.extend(cfg["packages"])
    # Hatch accepts glob patterns; only check literal directory names.
    literal = [p for p in declared if "*" not in p]
    absent = [p for p in literal if not (REPO_ROOT / p).is_dir()]
    assert not absent, (
        "declared in build packages but not present on disk: "
        + ", ".join(absent)
    )


def test_entry_point_modules_are_importable_without_optional_extras() -> None:
    """Importing the entry points must not require an extra to be installed.

    `extras` exist for genuinely optional integrations. If an entry point
    cannot import without one, the extra is not optional — it is a runtime
    dependency that happens to be named like a feature flag.
    """
    # Imported here rather than at module scope so the static tests above can
    # still report a useful failure if the manifest itself is unreadable.
    import astor_memory.cli  # noqa: F401

    proj = _manifest()["project"]
    optional = set()
    for group in (proj.get("optional-dependencies") or {}).values():
        for req in group:
            optional.add(_requirement_name(req))

    if not optional:
        pytest.skip("no optional-dependencies declared")

    for name, module in _entry_point_targets():
        try:
            __import__(module)
        except ImportError as exc:
            missing = _requirement_name(str(exc))
            assert missing not in optional, (
                f"{name} ({module}) needs the optional extra {missing!r} at "
                f"import time — it must be a runtime dependency"
            )
            raise


# --------------------------------------------------------------------------
# Layer 2: clean-environment install
# --------------------------------------------------------------------------

def _requirement_name(spec: str) -> str:
    """Extract the distribution name from a requirement string."""
    for sep in ("==", ">=", "<=", "~=", "!=", ">", "<", "[", ";", " "):
        spec = spec.split(sep)[0]
    return spec.strip().lower().replace("_", "-")


def _stdlib_names() -> set[str]:
    import sysconfig
    stdlib = sysconfig.get_paths().get("stdlib", "")
    names = set(sys.stdlib_module_names)
    return names


def _module_uses_third_party(tree: pyast.AST, local: set[str], stdlib: set[str]) -> set[str]:
    """Top-level module names imported by `tree` that are neither local nor stdlib."""
    found: set[str] = set()
    for node in pyast.walk(tree):
        if isinstance(node, pyast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0].lower()
                if root not in local and root not in stdlib:
                    found.add(root)
        elif isinstance(node, pyast.ImportFrom):
            if node.level:  # relative import — always local
                continue
            if node.module:
                root = node.module.split(".")[0].lower()
                if root not in local and root not in stdlib:
                    found.add(root)
    return found


@pytest.mark.slow
# v1.17.2: this test installs the whole package from the network inside a bare
# venv. With a warm uv cache that is seconds; on a cold CI runner competing
# with the other matrix legs for bandwidth it has blown the default 180s
# per-test timeout (b064aa0 failed exactly this way on the 3.10 and 3.12
# legs while 3.11/3.13 passed). The work itself is correct — give it room.
@pytest.mark.timeout(600)
def test_clean_install_provides_every_imported_third_party_module(tmp_path: Path) -> None:
    """Build a venv from pyproject alone, then walk the package for imports.

    This is the check that would have caught psutil and cryptography. It is
    slow (a full dependency install) and environment-dependent, so it skips
    rather than fails when the environment cannot be created.
    """
    venv_dir = tmp_path / "cleanenv"

    uv = None
    for cand in ("uv", "uv.exe"):
        try:
            subprocess.run([cand, "--version"], capture_output=True, check=True)
            uv = cand
            break
        except (OSError, subprocess.CalledProcessError):
            continue
    if uv is None:
        pytest.skip("uv not available; cannot build a clean environment")

    r = subprocess.run(
        [uv, "venv", str(venv_dir), "--python", f"{sys.version_info.major}.{sys.version_info.minor}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        pytest.skip(f"could not create venv: {r.stderr[:200]}")

    py = venv_dir / ("Scripts" if sys.platform == "win32" else "bin") / (
        "python.exe" if sys.platform == "win32" else "python"
    )
    r = subprocess.run(
        [str(py), "-m", "pip", "install", "--quiet", str(REPO_ROOT)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        # No pip in the bare venv is common with uv's default seeds; try uv pip.
        r = subprocess.run(
            [uv, "pip", "install", "--python", str(py), "--quiet", str(REPO_ROOT)],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            pytest.skip(f"clean install failed (offline?): {r.stderr[-300:]}")

    # Ask the clean interpreter which IMPORT names it can resolve, not just
    # which distributions are installed.
    #
    # Comparing an import name against a distribution name is what this test
    # got wrong first: `import nacl` comes from the distribution `pynacl`,
    # `import yaml` from `PyYAML`, `import dateutil` from `python-dateutil`.
    # Matching raw names reported pynacl as missing on an install that had it.
    # Walking the distributions' top_level.txt plus the module names actually
    # resolvable covers both spellings.
    probe = (
        "import json, importlib.metadata as md, importlib.util as u\n"
        "names = set()\n"
        "for d in md.distributions():\n"
        "    try:\n"
        "        names.add((d.metadata['Name'] or '').lower().replace('_','-'))\n"
        "        top = d.read_text('top_level.txt') or ''\n"
        "        for line in top.splitlines():\n"
        "            line = line.strip().lower()\n"
        "            if line:\n"
        "                names.add(line.split('.')[0])\n"
        "    except Exception:\n"
        "        pass\n"
        "print(json.dumps(sorted(names)))\n"
    )
    r = subprocess.run([str(py), "-c", probe], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"probe failed: {r.stderr[:200]}")
    import json as _json
    provided = set(_json.loads(r.stdout))

    stdlib = _stdlib_names()
    local = {p.name for p in REPO_ROOT.iterdir() if (p / "__init__.py").exists()}
    local |= {"astor_memory"}

    needed: set[str] = set()
    for path in sorted((REPO_ROOT / "astor_memory").rglob("*.py")):
        try:
            tree = pyast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        needed |= _module_uses_third_party(tree, local, stdlib)

    # Modules reached only through a guarded import that degrades when the
    # library is absent. These are optional by construction: each site has a
    # fallback, so absence changes capability, not correctness.
    #
    #   sentence_transformers  reranker falls back to lexical scoring
    #   yaml                   config.py falls back to a minimal parser
    #   dateutil               relative_date falls back to regex parsing
    #   agent / hermes_agent   the Hermes integration and its merge provider
    #                          path; neither is reachable unless astor is
    #                          embedded in a Hermes install
    OPTIONAL_WITH_FALLBACK = {
        "sentence_transformers", "yaml", "dateutil", "agent", "hermes_agent",
    }

    missing = sorted(
        m for m in needed
        if m not in provided and m not in stdlib and m not in local
        and m not in OPTIONAL_WITH_FALLBACK
    )
    assert not missing, (
        "modules imported by astor_memory but absent from a clean install: "
        + ", ".join(missing)
        + "\n  (each needs to be in [project.dependencies], not an extra — "
        "conditional imports hide this from manifest review)"
    )


def test_version_is_consistent_across_every_declaration():
    """`version` appears in three places and `am` reads different ones.

    pyproject [project].version is what pip installs,
    [tool.astor].version is what `am config` reports, and
    astor_memory.__version__ is what the server logs. When these drift the
    symptom is a support report that says one thing while the running code
    says another — e.g. `am --version` claiming 1.16.75 eight releases
    behind a 1.16.83 install, with nothing failing until someone looks.
    """
    import re
    from pathlib import Path

    import astor_memory

    root = Path(__file__).resolve().parents[1]
    pyproject = root / "pyproject.toml"
    # tomllib rejects a BOM outright ("Invalid statement"), so strip it
    # rather than asserting its absence — a BOM here is a hard install
    # failure, but that is a different test's concern.
    data = _toml.loads(pyproject.read_text(encoding="utf-8-sig"))

    project_version = data["project"]["version"]
    astor_version = data["tool"]["astor"]["version"]

    assert project_version == astor_version, (
        f"pyproject [project].version={project_version} but "
        f"[tool.astor].version={astor_version}"
    )
    assert astor_memory.__version__ == project_version, (
        f"astor_memory.__version__={astor_memory.__version__} but "
        f"pyproject version={project_version}"
    )

    # No stale major.minor.patch left behind anywhere in the file.
    stale = re.findall(r"version = \"(\d+\.\d+\.\d+)\"", pyproject.read_text(encoding="utf-8-sig"))
    assert len(set(stale)) == 1, (
        f"pyproject declares multiple versions: {sorted(set(stale))}"
    )
