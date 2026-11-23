"""
The release workflow must not hand an attacker two levers.

Two concrete holes were open in `.github/workflows/release.yml`:

  1. `pypa/gh-action-pypi-publish@release/v1` — a mutable tag — ran inside the
     job that holds `id-token: write`, i.e. the job that gets the PyPI
     trusted-publishing token. Anyone who can move that upstream tag owns the
     next release of the package.

  2. `EXPECTED="${{ github.event.inputs.version }}"` put the `workflow_dispatch`
     input straight into a `run:` block. GitHub substitutes the expression
     textually *before* the shell sees it, so a dispatch input containing
     `"` and `$(...)` is shell code. The fix routes the value through `env:`
     and quotes it in the shell.

These assertions read the workflow as text (no YAML dependency, no network,
no CI execution). The rules they encode are the ones a reviewer would
otherwise have to re-derive by eye on every release.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"

# A commit SHA: exactly 40 lowercase hex chars.
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _uses_lines(text: str) -> list[tuple[int, str]]:
    """(line number, the action reference) for every `uses:` in the file."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("uses:"):
            out.append((n, stripped[len("uses:"):].strip()))
    return out


def _run_blocks(text: str) -> list[tuple[int, str]]:
    """(line number, script body) for every `run: |` block in the file.

    A block ends at the first line indented less than the `run:` key.
    """
    lines = text.splitlines()
    blocks: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        m = re.match(r"^(\s*)run:\s*\|", lines[i])
        if not m:
            i += 1
            continue
        indent = len(m.group(1))
        body: list[str] = []
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= indent:
                break
            body.append(nxt)
            j += 1
        blocks.append((i + 1, "\n".join(body)))
        i = j
    return blocks


def test_every_third_party_action_is_pinned_to_a_commit_sha() -> None:
    """No `uses:` may reference a mutable tag or branch.

    A tag like `v4` or `release/v1` can be re-pointed at any commit by
    whoever controls the upstream repo. In a job with release credentials
    that is a supply-chain takeover, not a nuisance.
    """
    text = _workflow_text()
    offenders = []
    for lineno, ref in _uses_lines(text):
        # Local actions (`./path`) have no upstream to pin.
        if ref.startswith("./"):
            continue
        # Strip a trailing comment before parsing the ref.
        ref = ref.split("#", 1)[0].strip()
        if "@" not in ref:
            offenders.append(f"line {lineno}: {ref!r} has no @ref")
            continue
        _action, _sep, rev = ref.rpartition("@")
        if not SHA_RE.match(rev):
            offenders.append(f"line {lineno}: {ref!r} is not pinned to a 40-char SHA")
    assert not offenders, (
        "mutable action refs in .github/workflows/release.yml:\n  "
        + "\n  ".join(offenders)
    )


def test_oidc_publish_job_uses_a_sha_pinned_pypi_action() -> None:
    """The PyPI publish step specifically must be SHA-pinned.

    Checked on its own so that removing the global pin cannot silently
    downgrade only the credentialed step.
    """
    text = _workflow_text()
    pinned = [
        ref for _n, ref in _uses_lines(text)
        if ref.split("#", 1)[0].strip().startswith("pypa/gh-action-pypi-publish@")
    ]
    assert pinned, "the PyPI publish step is gone — check the workflow by hand"
    for ref in pinned:
        rev = ref.split("#", 1)[0].strip().rpartition("@")[2]
        assert SHA_RE.match(rev), (
            f"pypa/gh-action-pypi-publish is referenced as {ref!r}; "
            "the OIDC publish job must use a full commit SHA"
        )


def test_no_workflow_dispatch_input_is_interpolated_into_a_run_block() -> None:
    """`run:` scripts must not contain `${{ ... }}` expressions.

    GitHub expands expressions textually before the shell runs, so an
    expression inside a `run:` is shell injection, not data.
    """
    text = _workflow_text()
    offenders = []
    for lineno, body in _run_blocks(text):
        for expr in re.findall(r"\$\{\{[^}]*\}\}", body):
            offenders.append(f"line {lineno}: {expr}")
    assert not offenders, (
        "expressions interpolated into shell code:\n  "
        + "\n  ".join(offenders)
        + "\n  (pass the value through env: and quote it in the shell)"
    )


def test_version_input_travels_through_env_and_is_quoted() -> None:
    """The version dispatch input must arrive as an env var, quoted on use.

    Asserted positively (not just "no expressions in run") so the fix
    cannot be replaced by something that merely drops the check.
    """
    text = _workflow_text()
    assert re.search(
        r"^\s*VERSION_INPUT:\s*\$\{\{\s*github\.event\.inputs\.version\s*\}\}\s*$",
        text,
        re.MULTILINE,
    ), "the version input is not mapped to the VERSION_INPUT env var"

    # The regex only matches when the value is inside double quotes, so the
    # captured group is the bare variable reference.
    quoted = re.findall(r'^\s*EXPECTED="(\$[A-Za-z_][A-Za-z0-9_]*)"\s*$', text, re.MULTILINE)
    assert "$VERSION_INPUT" in quoted, (
        "EXPECTED must be assigned from the quoted env var \"$VERSION_INPUT\", "
        f"found assignments: {quoted!r}"
    )


# --- v1.17.85: the release was unrunnable, and silently so ----------------
#
# The version dispatch input defaulted to 'v1.17.26' -- frozen since
# Week 4 of the plan, long before the project reached 1.17.85. Two
# things followed, and neither was visible until someone tried to
# release:
#
#   1. The default was wrong, so the workflow failed its own version
#      check against a version that had nothing to do with the code.
#      The error named the OLD version, which reads like "the release
#      is broken" rather than "the default was never updated".
#
#   2. The checkout had no `ref:`, so it took the DEFAULT BRANCH, not
#      the tag. The workflow names a version to release and then
#      verifies it against pyproject.toml -- but pyproject came from
#      main. So the check compared main's version against the tag's
#      name, and it could never succeed for any tag that was not also
#      main's HEAD. It was not a release of a tag; it was a release of
#      whatever happened to be on main, wearing the tag's name.
#
# The tags had also stopped at v1.17.40 -- nothing creates them.


def test_the_version_default_matches_the_declared_version() -> None:
    """The dispatch default must be the version the code actually is.

    A stale default is worse than no default: the workflow runs, and
    fails, with an error naming a version from months ago.
    """
    text = _workflow_text()
    m = re.search(r"^\s*default:\s*'(v\d+\.\d+\.\d+)'\s*$", text, re.MULTILINE)
    assert m, "the version input has no default"
    default = m.group(1)

    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8-sig")
    pm = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    assert pm, "pyproject.toml has no [project].version"
    declared = pm.group(1)

    assert default == f"v{declared}", (
        f"the release workflow's version default is {default} but "
        f"pyproject.toml declares {declared} -- the release would fail "
        f"its own version check against a version from the past"
    )


def test_the_checkout_uses_the_release_tag() -> None:
    """The tag being released must be what gets built.

    Without a `ref:`, checkout takes the default branch, so the
    workflow verifies the tag's version against main's pyproject.toml
    and packages main's code under the tag's name.
    """
    text = _workflow_text()
    # The checkout step runs from its `- name: Checkout` line to the
    # next step. Comments inside the `with:` block are part of it, so
    # the block is taken by step boundaries rather than by a pattern
    # that would stop at the first comment.
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines)
                  if re.match(r"^\s*- name: Checkout\s*$", l)), None)
    assert start is not None, "the checkout step was not found"
    end = next((i for i in range(start + 1, len(lines))
                 if re.match(r"^\s*- name: ", lines[i])), len(lines))
    block = "\n".join(lines[start:end])

    assert re.search(r"^\s*ref:\s*\$\{\{\s*github\.event\.inputs\.version\s*\}\}\s*$",
                     block, re.MULTILINE), (
        "the checkout does not pin the release tag -- it would build "
        "the default branch and publish it under the tag's name"
    )
    assert re.search(r"^\s*fetch-depth:\s*0\s*$", block, re.MULTILINE), (
        "the checkout does not fetch tags, so the tag cannot be verified"
    )


def test_the_release_verifies_that_the_tag_exists() -> None:
    """A version that names a tag nobody pushed is not a release.

    The check has to fail loudly and say what to do about it, because
    the whole gap was invisible: the workflow had never run, PyPI had
    never received a package, and the tags had silently stopped.
    """
    text = _workflow_text()
    assert re.search(r'refs/tags/\$EXPECTED', text), (
        "the release does not check that the tag exists"
    )
    assert "git tag -a" in text, (
        "the failure message does not say how to create the missing tag"
    )
