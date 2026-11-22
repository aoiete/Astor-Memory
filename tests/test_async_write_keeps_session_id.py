"""A fact written asynchronously must remember which session wrote it.

The write path has two halves. The sync half promotes the fact with
``origin_session_id`` set, so the fact records the conversation it came
from. The async half -- the one every write goes through when the
embedding is slow, which is the normal case -- captured the session id
into a local and then never used it.

The result was that a fact written by the Telegram agent, through
astor_write, had NO origin_session_id. Not a wrong one: none. And
origin_session_id is what the dashboard buckets by when it answers
"which platform did this come from" and "which session".

The session id was captured and dropped, so the fix is to thread it
through. This test pins the two write paths to the same rule, because
the two drifting apart is exactly what happened.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVER = REPO / "astor_memory" / "server.py"


def _promote_calls(tree: ast.AST) -> list[ast.Call]:
    """Every call to promote_candidate in the file."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = None
            if isinstance(f, ast.Attribute):
                name = f.attr
            elif isinstance(f, ast.Name):
                name = f.id
            if name == "promote_candidate":
                out.append(node)
    return out


def _kwarg(call: ast.Call, key: str):
    for kw in call.keywords:
        if kw.arg == key:
            return kw.value
    return None


class TestAsyncWriteKeepsTheSessionId(unittest.TestCase):

    def setUp(self):
        self.src = SERVER.read_text(encoding="utf-8")
        self.tree = ast.parse(self.src)
        self.calls = _promote_calls(self.tree)

    def test_the_async_write_threads_the_session_id(self):
        """The async promote must carry origin_session_id.

        It is the write path that runs for a real Telegram message: the
        fact is extracted in a background thread because embedding is
        the slow part. The sync path was the only one that recorded the
        session, and it is the path that is NOT taken in normal
        operation.
        """
        async_calls = [c for c in self.calls
                       if _kwarg(c, "promoted_by") is not None
                       and "async" in ast.unparse(_kwarg(c, "promoted_by"))]
        self.assertTrue(async_calls,
                        "the async write path was not found -- the test "
                        "cannot tell whether it keeps the session id")
        for call in async_calls:
            self.assertIsNotNone(
                _kwarg(call, "origin_session_id"),
                "the async write promotes the fact without "
                "origin_session_id -- the fact cannot be traced back to "
                "the conversation that wrote it")

    def test_the_session_id_is_not_captured_and_dropped(self):
        """A local that holds the session id and is never read is the bug.

        This is the shape of the defect: the value was threaded into the
        thread's scope, so it LOOKED handled, and then nothing used it.
        """
        assigned = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                assigned.add(node.id)
        used = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                used.add(node.id)
        # _w_session was the captured-but-unused one. If it is assigned
        # and never loaded, the session id is being dropped again.
        for name in ("_w_session",):
            if name in assigned:
                self.assertIn(name, used,
                              f"{name} is assigned but never read -- the "
                              "session id is captured and dropped again")

    def test_the_session_and_mirror_paths_agree(self):
        """The primary write and its source-tier mirror are one write."""
        session_calls = [c for c in self.calls
                         if _kwarg(c, "origin_session_id") is not None]
        self.assertTrue(session_calls)
        for call in session_calls:
            # Every write that knows its session must name the same
            # source, so the two paths cannot drift apart.
            value = ast.unparse(_kwarg(call, "origin_session_id"))
            self.assertTrue(
                "_write_session_id" in value or "_w_session" in value,
                f"origin_session_id is set from {value!r}, which is not the "
                "session id the write was given")


if __name__ == "__main__":
    unittest.main()
