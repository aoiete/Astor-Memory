"""Check that the hermes plugin template and the installed plugin expose the
same CONTRACT.

The template in docs/agent_plugin_template/frameworks/hermes/ is the
canonical reference implementation. The copy that actually runs lives in
the hermes-agent checkout at plugins/memory/astor_memory/ and is
UNTRACKED by git -- so a hermes upgrade can silently replace or empty it
without git noticing. That happened on 2026-11-02: the installed copy
had drifted to a different set of tools and parameter names than the
template, and the template was still on the pre-v1.13.2 "input_schema"
key that current hermes no longer reads.

Comparing the two files byte-for-byte is the wrong check. They are
SUPPOSED to differ: the template resolves its paths from the
environment, while the installed copy may carry one machine's paths.
What must not differ is the contract a caller sees:

  - the set of tool names
  - the parameter names each tool accepts
  - the schema key the framework reads ("parameters", not "input_schema")

So this compares the contract, not the implementation.

Usage:
    python tools/check_plugin_contract.py            # compare template vs installed
    python tools/check_plugin_contract.py --template # template against itself
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "docs" / "agent_plugin_template" / "frameworks" / "hermes" / "__init__.py"
def default_installed() -> Path:
    """Locate the installed hermes plugin without hard-coding one machine.

    HERMES_HOME wins when it is set; otherwise fall back to the platform's
    local app data, which is where hermes-agent keeps its tree on Windows.
    """
    import os
    home = os.environ.get("HERMES_HOME")
    if home:
        base = Path(home)
    else:
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            raise SystemExit("[ERR] set HERMES_HOME (or LOCALAPPDATA) to locate the install")
        base = Path(local) / "hermes"
    return base / "hermes-agent" / "plugins" / "memory" / "astor_memory" / "__init__.py"

# the schema key current hermes reads (renamed from input_schema in v1.13.2)
SCHEMA_KEY = "parameters"
LEGACY_SCHEMA_KEY = "input_schema"


def tool_contract(path: Path) -> dict[str, dict]:
    """Return {tool_name: {param_name: True}} from a plugin source file."""
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)

    tools: dict[str, dict] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "get_tool_schemas":
            continue
        for stmt in ast.walk(node):
            if not isinstance(stmt, ast.Dict):
                continue
            d = stmt
            name = None
            params: dict[str, bool] = {}
            for k, v in zip(d.keys, d.values):
                if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                    continue
                if k.value == "name" and isinstance(v, ast.Constant):
                    name = v.value
                if k.value in ("parameters", "input_schema") and isinstance(v, ast.Dict):
                    # The tool's argument names live under the JSON Schema
                    # "properties" key, not at the top of the schema dict:
                    #   {"type": "object", "properties": {"query": {...}}, "required": [...]}
                    # Taking the schema's own keys would report
                    # properties/required/type for every tool and compare
                    # nothing -- a check that always passes.
                    props = None
                    for sk, sv in zip(v.keys, v.values):
                        if isinstance(sk, ast.Constant) and sk.value == "properties" \
                                and isinstance(sv, ast.Dict):
                            props = sv
                    if props is not None:
                        for pk in props.keys:
                            if isinstance(pk, ast.Constant) and isinstance(pk.value, str):
                                params[pk.value] = True
            if name:
                tools[name] = params
    return tools


def legacy_keys(path: Path) -> list[str]:
    src = path.read_text(encoding="utf-8")
    return sorted(set(re.findall(rf'"{LEGACY_SCHEMA_KEY}"\s*:', src)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--installed", default=None)
    ap.add_argument("--template", action="store_true",
                    help="check the template against itself (self-test)")
    args = ap.parse_args()

    installed = Path(args.installed) if args.installed else default_installed()
    if args.template:
        installed = TEMPLATE

    for p in (TEMPLATE, installed):
        if not p.exists():
            print(f"[ERR] missing: {p}")
            return 2

    t_tools = tool_contract(TEMPLATE)
    i_tools = tool_contract(installed)

    print(f"  template   : {TEMPLATE}")
    print(f"  installed  : {installed}")
    print(f"  tools      : template={sorted(t_tools)}")
    print(f"                installed={sorted(i_tools)}")

    problems: list[str] = []

    missing = sorted(set(t_tools) - set(i_tools))
    extra = sorted(set(i_tools) - set(t_tools))
    if missing:
        problems.append(f"installed is MISSING tools the template has: {missing}")
    if extra:
        problems.append(f"installed has tools the template does not: {extra}")

    for name in sorted(set(t_tools) & set(i_tools)):
        t_p = set(t_tools[name])
        i_p = set(i_tools[name])
        if t_p != i_p:
            problems.append(
                f"tool '{name}' parameter mismatch: "
                f"only-in-template={sorted(t_p - i_p)} only-in-installed={sorted(i_p - t_p)}")

    for label, p in (("template", TEMPLATE), ("installed", installed)):
        legacy = legacy_keys(p)
        if legacy:
            problems.append(
                f"{label} still uses the legacy '{LEGACY_SCHEMA_KEY}' key "
                f"({len(legacy)}x) -- current hermes reads '{SCHEMA_KEY}'")

    if not t_tools:
        problems.append("template exposed no tools -- the parser is wrong, not the file")

    if problems:
        print("\n  DRIFT:")
        for p in problems:
            print(f"    - {p}")
        return 1

    print("\n  PASS: template and installed plugin expose the same contract")
    return 0


if __name__ == "__main__":
    sys.exit(main())
