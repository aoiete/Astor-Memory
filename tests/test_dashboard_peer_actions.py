"""The dashboard's delete button has to actually delete.

It did not. `onPeerAction` was an if / else-if chain:

    if (action === 'delete') {
      if (!confirm(...)) return;        <- the first branch matched delete
    } else if (action === 'blacklist') {
      ...
    } else if (action === 'delete') {   <- unreachable: the chain had
      const r = await fetch(... DELETE)    already matched 'delete'
    } else if ...

So the button asked for confirmation, the operator said yes, and nothing
happened. The peer stayed, and the only evidence was the button that
did not work.

The test asserts on the source, because the bug is a control-flow
shape, not a runtime value: the DELETE fetch has to be in the same
branch that raises the confirm, and there must not be a second
'delete' branch further down the chain.
"""
from __future__ import annotations

import re
from pathlib import Path

APP_JS = (Path(__file__).resolve().parent.parent
          / "astor_memory" / "dashboard" / "app.js")


def _on_peer_action_body() -> str:
    src = APP_JS.read_text(encoding="utf-8")
    i = src.find("async function onPeerAction")
    assert i >= 0, "onPeerAction is gone from app.js"
    # The function ends at the next top-level `async function` or `function`.
    j = src.find("\n}", i)
    assert j > i
    return src[i:j]


def test_delete_branch_sends_the_delete_request():
    body = _on_peer_action_body()
    code = "\n".join(l.split("//")[0] for l in body.splitlines())
    # Find the branch that handles the delete action.
    m = re.search(r"if \(action === 'delete'\)\s*\{", code)
    assert m, "no delete branch in onPeerAction"
    branch = code[m.end():]
    # The branch ends at the next `} else if` at the same nesting.
    end = branch.find("} else if")
    branch = branch[:end] if end > 0 else branch
    assert "method: 'DELETE'" in branch, (
        "the delete branch does not send a DELETE request:\n%s" % branch)
    assert "confirm(" in branch, (
        "the delete branch does not ask for confirmation:\n%s" % branch)


def test_there_is_only_one_delete_branch():
    """A second 'delete' branch in the chain is unreachable by construction."""
    body = _on_peer_action_body()
    # Strip line comments first -- the fix's own comment mentions the
    # dead branch by name, and a comment is not a branch.
    code = "\n".join(l.split("//")[0] for l in body.splitlines())
    branches = re.findall(r"(?:if|else if) \(action === 'delete'\)", code)
    assert len(branches) == 1, (
        "onPeerAction has %d delete branches; only the first one can ever "
        "run, so the others are dead code" % len(branches))


def _all_handler_bodies() -> str:
    """Every click handler in app.js, not just the peer one.

    The check is "a button whose action has no branch does nothing when
    clicked". A button is not necessarily in the peer table -- the
    connections table has its own switch -- so looking at one handler
    reported a working button as a dead one. The set of handlers is
    what a click can reach.
    """
    # The whole file, minus the line comments. A handler can be
    # anywhere in it, and slicing a function body out by brace
    # counting stops at the first nested `}` -- which is a try/finally
    # in most handlers.
    src = APP_JS.read_text(encoding="utf-8")
    return "\n".join(l.split("//")[0] for l in src.splitlines())


def test_every_action_button_has_a_branch():
    """A button whose action has no branch does nothing when clicked."""
    code = _all_handler_bodies()
    src = APP_JS.read_text(encoding="utf-8")
    actions = set(re.findall(r'data-action="([a-z-]+)"', src))
    handled = set(re.findall(r"action === '([a-z-]+)'", code))
    missing = actions - handled
    assert not missing, (
        "these dashboard buttons have no handler: %s" % sorted(missing))
