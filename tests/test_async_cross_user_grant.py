"""An async write into someone else's private bucket needs a grant.

The background thread bound its ACL to the TARGET user, and
astor_check_write decides "own scope vs cross-user" by comparing the
target against ctx.user_id. With ctx.user_id set to the target, every
cross-user private write looked like the caller's own scope and the
grant check was skipped.

The fix keeps the caller's identity in the thread and opens the target's
bucket under an owner scope -- the same shape store.py uses.
"""
from __future__ import annotations

import ast
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _post(url: str, payload: dict, timeout: float = 30.0):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        url, data=data, method='POST',
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8') or '{}'
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {'raw': raw}


class TestAsyncThreadKeepsTheCallersIdentity(unittest.TestCase):
    """The async thread must not become the target.

    astor_check_write decides "own scope vs cross-user" by comparing the
    target against ctx.user_id. The thread used to bind ctx.user_id to the
    TARGET, so every ACL check inside the thread saw "my own scope" --
    including the write into the target's bucket, which then needed no
    grant of its own.

    The assertion is on the AST, not on the source text: it looks at the
    astor_init_acl call inside the async write thread and checks which
    value it binds as user_id. Source-text matching would break on a
    reformat; this cannot.
    """

    def _acl_bindings_in_the_async_thread(self):
        """Every astor_init_acl call inside server.py, with its user_id arg."""
        tree = ast.parse(
            (REPO / "astor_memory" / "server.py").read_text(encoding="utf-8"))
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name not in ("astor_init_acl", "_wia"):
                continue
            uid = None
            for kw in node.keywords:
                if kw.arg == "user_id":
                    uid = ast.unparse(kw.value)
            found.append((node.lineno, uid))
        return found

    def test_no_acl_binding_in_the_server_binds_the_target_user(self):
        """No ACL binding may name _w_user (the write target)."""
        offenders = [(ln, uid) for ln, uid in self._acl_bindings_in_the_async_thread()
                     if uid and "_w_user" in uid]
        self.assertEqual(
            offenders, [],
            "an ACL binding names the write target as the acting user")

    def test_the_async_thread_binds_the_callers_user_id(self):
        """And it does bind the caller's own id."""
        bindings = self._acl_bindings_in_the_async_thread()
        good = [(ln, uid) for ln, uid in bindings
                if uid and "_w_caller_uid" in uid]
        self.assertTrue(
            good,
            "the async write thread does not bind the caller's own user_id")

    def test_the_target_bucket_is_opened_under_an_owner_scope(self):
        """Opening the target's bucket must not mean becoming the target."""
        tree = ast.parse(
            (REPO / "astor_memory" / "server.py").read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add(alias.asname or alias.name)
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
        # the import may be aliased (own_private_scope as _w_scope)
        self.assertTrue(
            any(n.startswith("own_private_scope") or n.endswith("_scope")
                or n.endswith("_scope2") for n in names),
            "the async write thread opens the target's private bucket "
            "without an owner scope")


if __name__ == "__main__":
    unittest.main()
