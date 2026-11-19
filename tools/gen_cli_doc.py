#!/usr/bin/env python
"""Generate docs/cli.md from the CLI's own argparse definitions.

The CLI is the reference implementation of what astor can do, so the
document is generated from it rather than maintained by hand: a command
that exists is documented the next time this runs, and one that was
removed disappears from the document instead of lingering.

Usage:
    python tools/gen_cli_doc.py            # write docs/cli.md
    python tools/gen_cli_doc.py --check    # fail if docs/cli.md is stale
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CLI_MAIN = REPO / "astor_memory" / "cli" / "main.py"
OUT = REPO / "docs" / "cli.md"


def _python() -> str:
    for cand in (os.environ.get("ASTOR_PYTHON"), sys.executable):
        if cand and Path(cand).exists():
            return cand
    return sys.executable


def _top_level_commands() -> list[tuple[str, str]]:
    """Every command registered on the main subparser, with its help text.

    Only the main ``subparsers`` counts; nested groups (dashboard, bundle)
    register on their own parser objects and are documented under their
    parent.
    """
    src = CLI_MAIN.read_text(encoding="utf-8", errors="replace")
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for pat in (
        re.compile(r"subparsers\.add_parser\(\s*'([^']+)'\s*,\s*help='([^']*)'", re.S),
        re.compile(r'subparsers\.add_parser\(\s*"([^"]+)"\s*,\s*help="([^"]*)"', re.S),
    ):
        for name, helptext in pat.findall(src):
            if name not in seen:
                seen.add(name)
                found.append((name, helptext))
    return found


def _help_for(command: str) -> str:
    env = dict(os.environ)
    env.setdefault("ASTOR_DIR", str(REPO.parent / "Astor-Memory-Runtime"))
    env["PYTHONPATH"] = str(REPO)
    try:
        proc = subprocess.run(
            [_python(), "-m", "astor_memory.cli.main", command, "--help"],
            capture_output=True, text=True, timeout=90, env=env, cwd=str(REPO),
        )
    except Exception as exc:  # a command that cannot print help is skipped
        return ""
    out = (proc.stdout or "").strip()
    return out if "usage:" in out else ""


def build() -> str:
    cmds = _top_level_commands()
    lines = [
        "# CLI reference",
        "",
        "Every `am` subcommand, generated from the CLI's own argument parser.",
        "Regenerate with `python tools/gen_cli_doc.py`; the document is never",
        "edited by hand, so a command that exists is always documented and a",
        "command that was removed disappears from here.",
        "",
        "## Index",
        "",
        "| command | what it does |",
        "| --- | --- |",
    ]
    bodies = []
    for name, helptext in cmds:
        one_line = " ".join(helptext.split()) or "(see below)"
        lines.append("| `am %s` | %s |" % (name, one_line))
        body = _help_for(name)
        if body:
            bodies.append((name, body))
    lines.append("")
    lines.append("## Commands")
    lines.append("")
    for name, body in bodies:
        lines.append("### `am %s`" % name)
        lines.append("")
        lines.append("```")
        lines.append(body)
        lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if docs/cli.md is out of date")
    args = ap.parse_args()

    text = build()
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print("docs/cli.md is stale -- run: python tools/gen_cli_doc.py")
            return 1
        print("docs/cli.md is up to date")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print("wrote %s (%d bytes, %d commands)" % (OUT, len(text), text.count("### `am ")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
