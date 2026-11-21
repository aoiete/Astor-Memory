"""The docs must not tell an agent to run a command that does not exist.

Three of them did: `am read` (the real one is `am recall`), `am serve`
(the server takes --host/--port on the command line, there is no serve
subcommand) and `am restore` (a tombstoned fact comes back through
POST /v1/fact/<id>/restore). A doc that names a command nobody can
run is worse than no doc: the agent follows it, the command fails, and
the agent reports the failure as a memory problem.

So the command set is taken from the CLI itself -- `am --help` prints
it -- and every `am <something>` in the docs has to be in it.

KNOWN_DEBT lists the exceptions, one file and one command each. They
are commands that a planning document or a historical record names as
a planned or former feature -- `am migrate from-mem0`, `am skill scan`,
`am compact` -- not commands the docs tell a reader to run today. The
list is per file and per command on purpose: a new document that
invents a command fails this test immediately, and a debt that is
paid off gets its entry deleted rather than the test loosened.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[1] / "docs"

# `am` on its own, or `am` followed by a flag -- both fine.
_CMD = re.compile(r"\bam\s+([a-z][a-z0-9-]*)")

KNOWN_DEBT: dict[str, set[str]] = {
    "acl-v1.2-hardening.md": {"compact", "audit"},
    "acl-v1.2-hardening.zh-CN.md": {"compact", "audit"},
    "architecture.md": {"compact", "serve", "skill", "verify"},
    "architecture.zh-CN.md": {"compact", "skill", "verify"},
    "auto-memory.md": {"server"},
    "contributing.md": {"migrate"},
    "contributing.zh-CN.md": {"migrate"},
    "fact-lifecycle.md": {"broadcast", "ingest"},
    "fact-lifecycle.zh-CN.md": {"broadcast", "ingest"},
    "faq.md": {"skill"},
    "faq.zh-CN.md": {"skill"},
    "migration.md": {"migrate", "serve"},
    "migration.zh-CN.md": {"migrate", "serve"},
    "troubleshooting.md": {"benchmark", "compact", "inspect", "logs",
                           "processes", "serve", "verify"},
    "troubleshooting.zh-CN.md": {"benchmark", "compact", "inspect", "logs",
                                 "serve", "verify"},
}


@pytest.fixture(scope="module")
def real_commands() -> set[str]:
    """The subcommands `am` actually has, read from `am --help`."""
    out = subprocess.run(
        [sys.executable, "-m", "astor_memory.cli.main", "--help"],
        capture_output=True, text=True, timeout=120,
    )
    text = out.stdout + out.stderr
    m = re.search(r"\{([a-z0-9,-]+)\}", text)
    assert m, f"could not read the subcommand list from --help: {text[:400]}"
    return {c for c in m.group(1).split(",") if c}


def _doc_files() -> list[Path]:
    # The Chinese documents quote the English ones; they are checked too.
    return sorted(DOCS.rglob("*.md"))


def _spans(text: str) -> list[str]:
    """Every code span and every line inside a fenced block."""
    spans: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            spans.append(line)
        spans.extend(re.findall(r"`([^`\n]+)`", line))
    return spans


@pytest.mark.parametrize("doc", _doc_files(), ids=lambda p: p.name)
def test_every_am_command_in_the_docs_exists(doc: Path, real_commands: set[str]) -> None:
    allowed = KNOWN_DEBT.get(doc.name, set())
    found: dict[str, str] = {}
    for span in _spans(doc.read_text(encoding="utf-8")):
        for name in _CMD.findall(span):
            if name in real_commands or name in allowed:
                continue
            # `--version` is a flag, not a subcommand.
            if name.startswith("-"):
                continue
            found.setdefault(name, span.strip()[:80])
    assert not found, (
        f"{doc.name} names am commands that do not exist: {found}\n"
        f"the real ones are: {sorted(real_commands)}\n"
        f"if the command is planned rather than shipped, add it to "
        f"KNOWN_DEBT in this file -- otherwise fix the document."
    )


def test_the_cli_does_not_print_a_restore_command() -> None:
    """`am restore` was printed by the CLI itself as the way back."""
    src = (Path(__file__).resolve().parents[1] / "astor_memory" / "cli" / "main.py")
    assert "am restore" not in src.read_text(encoding="utf-8")
