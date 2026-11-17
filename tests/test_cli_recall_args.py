"""Regression tests for `am recall` CLI argument handling.

v1.17.39: a merge-conflict fragment left a duplicate `print(...)` referencing
the `except ... as e` variable OUTSIDE the except block, so every
`am recall --kinds/--zone` crashed with UnboundLocalError on the normal
(success) path. The zone oversample also ignored `--zone`, so `--zone X`
and the equivalent `--kinds ...` returned different counts.
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault('ASTOR_DIR', os.environ.get('ASTOR_DIR') or str(Path.home() / '.astor'))
sys.path.insert(0, os.environ.get('ASTOR_SOURCE_PATH') or str(Path.cwd()))

CLI_MAIN = Path(__file__).resolve().parents[1] / 'astor_memory' / 'cli' / 'main.py'


def _recall_source() -> str:
    return CLI_MAIN.read_text(encoding='utf-8')


def test_no_duplicate_kinds_warn_print():
    """The `--kinds` lookup warning must exist exactly once, inside the except."""
    hits = [ln for ln in _recall_source().splitlines()
            if '--kinds filter lookup failed' in ln]
    assert len(hits) == 1, f"duplicate warning print found: {hits}"


def _bound_names(node: ast.AST) -> set[str]:
    """Names a scope binds: assignments, imports, function/class defs, params."""
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, (ast.Store, ast.Del)):
            out.add(sub.id)
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(sub.name)
        elif isinstance(sub, (ast.Import, ast.ImportFrom)):
            for alias in sub.names:
                out.add((alias.asname or alias.name).split('.')[0])
        elif isinstance(sub, ast.arg):
            out.add(sub.arg)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            out.add(sub.name)
        elif isinstance(sub, ast.Global) or isinstance(sub, ast.Nonlocal):
            out.update(sub.names)
    return out


def test_except_var_not_referenced_outside_except_block():
    """A name bound ONLY by `except ... as e` must not be loaded from the try
    body -- on the success path that name does not exist (UnboundLocalError)."""
    tree = ast.parse(_recall_source())
    problems = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        handler_names = {h.name for h in node.handlers if h.name}
        if not handler_names:
            continue
        # Names the try body itself binds shadow the handler's name -- those
        # are legitimate (`except Exception as e` + `e = count` in the body).
        body_bound = _bound_names(ast.Module(body=node.body, type_ignores=[]))
        unbound = handler_names - body_bound
        if not unbound:
            continue
        for stmt in node.body:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                    if sub.id in unbound:
                        problems.append(f"L{sub.lineno}: '{sub.id}'")
    assert not problems, (
        "name bound only by `except ... as X` is loaded in the try body "
        "(UnboundLocalError on the success path): " + ", ".join(problems)
    )


def test_zone_oversamples_like_kinds():
    """`--zone` implies a kind filter, so it must oversample the candidate
    pool the same way `--kinds` does."""
    src = _recall_source()
    assert '_filtering = bool(args.kinds)' in src, \
        'zone-aware oversample flag missing (regression of v1.17.39)'
    assert 'if _filtering else args.top_k' in src, \
        'limit must use _filtering, not args.kinds alone'


@pytest.mark.parametrize('args', [
    ['recall', 'x', '--kinds', 'lesson'],
    ['recall', 'x', '--zone', 'lesson'],
])
def test_recall_cli_does_not_crash_on_kind_filter(args, monkeypatch, tmp_path):
    """The real command must not raise UnboundLocalError. Embeddings may be
    unavailable in a test env -- that is a different, expected failure."""
    from astor_memory.cli import main as cli_main

    monkeypatch.setenv('ASTOR_DIR', str(tmp_path / 'astor'))
    try:
        cli_main.main(args)
    except UnboundLocalError as exc:
        pytest.fail(f'UnboundLocalError in `am {" ".join(args)}`: {exc}')
    except Exception:
        # Any other error (missing embeddings, ACL not bound) is out of scope
        # for this regression.
        pass
