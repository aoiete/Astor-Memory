"""Three leftovers the peer hardening left behind.

1. peer_public_search had 180 lines of the old implementation sitting
   after the `return` that replaced it. Dead code -- but it referenced
   `req`, which the new path never builds, so removing the return would
   have turned a dead branch into a NameError.
2. The nonce ledger lives in SQLite so it survives across processes.
   That is the point, and it also means a test run leaves spent nonces
   behind: the next test replaying the same signed request got
   rejected as a replay instead of running the path it meant to test.
3. peer_connect.py had a comment block describing a byte cap whose
   constant had moved to peer_search.py.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


class TestPeerPublicSearchHasNoDeadCode(unittest.TestCase):

    def test_the_old_implementation_is_gone(self):
        src = _src("astor_memory/server.py")
        tree = ast.parse(src)
        fn = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name == "peer_public_search"):
                fn = node
                break
        self.assertIsNotNone(fn, "peer_public_search is gone")

        # Every statement after the final `return` in the function body
        # is dead. Find the last return at the top level of the body.
        last_return = None
        for stmt in fn.body:
            if isinstance(stmt, ast.Return):
                last_return = stmt
        self.assertIsNotNone(last_return)
        idx = fn.body.index(last_return)
        dead = fn.body[idx + 1:]
        self.assertEqual(
            dead, [],
            "peer_public_search still has %d statement(s) after its final "
            "return -- dead code that references `req`, which the live "
            "path never builds" % len(dead))

    def test_the_live_path_does_not_build_a_request_object(self):
        """The new path hands the whole request to the service layer."""
        src = _src("astor_memory/server.py")
        tree = ast.parse(src)
        fn = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name == "peer_public_search"):
                fn = node
                break
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        self.assertNotIn(
            "req", names,
            "peer_public_search still names a `req` -- the old local "
            "implementation is still in there")


class TestNonceLedgerIsResetBetweenTests(unittest.TestCase):

    def test_test_state_clears_the_nonce_ledger(self):
        src = _src("astor_memory/_internal/test_state.py")
        self.assertIn(
            "clear_nonce_ledger", src,
            "the test-state reset does not clear the nonce ledger, so a "
            "signed request spent by one test is rejected as a replay by "
            "the next one")

    def test_the_rate_limit_module_exposes_the_reset(self):
        src = _src("astor_memory/_internal/peer_rate_limit.py")
        tree = ast.parse(src)
        fns = {n.name for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)}
        self.assertIn(
            "clear_nonce_ledger", fns,
            "peer_rate_limit has no way to clear the nonce ledger")

    def test_the_reset_actually_empties_the_table(self):
        import os
        import sqlite3
        import tempfile

        tmp = tempfile.mkdtemp(prefix="astor_nonce_clear_")
        os.environ["ASTOR_DIR"] = tmp
        try:
            from astor_memory._internal import peer_rate_limit as prl
            db = prl._db_path()
            conn = sqlite3.connect(str(db))
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS peer_nonce ("
                    "peer_id TEXT NOT NULL, nkey TEXT NOT NULL,"
                    "ts REAL NOT NULL, PRIMARY KEY (peer_id, nkey))")
                conn.execute(
                    "INSERT OR IGNORE INTO peer_nonce VALUES ('p', 'k', 1.0)")
                conn.commit()
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM peer_nonce").fetchone()[0], 1)
            finally:
                conn.close()

            cleared = prl.clear_nonce_ledger()
            self.assertGreaterEqual(cleared, 1)

            conn = sqlite3.connect(str(db))
            try:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM peer_nonce").fetchone()[0], 0,
                    "the nonce ledger was not emptied")
            finally:
                conn.close()
        finally:
            os.environ.pop("ASTOR_DIR", None)


class TestPeerConnectCommentMatchesTheCode(unittest.TestCase):

    def test_the_byte_cap_comes_from_the_shared_constant(self):
        src = _src("astor_memory/_internal/peer_connect.py")
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for a in node.names:
                    imported.add(a.asname or a.name)
        self.assertIn(
            "MAX_RESPONSE_BYTES", imported,
            "peer_connect no longer imports the shared byte cap")

    def test_every_body_read_goes_through_the_capped_reader(self):
        src = _src("astor_memory/_internal/peer_connect.py")
        tree = ast.parse(src)
        fn = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name == "_read_capped"):
                fn = node
                break
        self.assertIsNotNone(fn, "_read_capped is gone")

        # No bare resp.read() / e.read() in the code. Comments are
        # stripped first -- the comment that explains the cap mentions
        # resp.read() on purpose.
        import io
        import tokenize
        code_only = []
        with io.StringIO(src) as fh:
            for tok in tokenize.generate_tokens(fh.readline):
                if tok.type == tokenize.COMMENT:
                    continue
                code_only.append(tok)
        body = tokenize.untokenize(code_only)
        import re
        bare = re.findall(r"\b(?:resp|e|response)\.read\(\s*\)", body)
        self.assertEqual(
            bare, [],
            "peer_connect still has an uncapped body read: %r" % bare)


if __name__ == "__main__":
    unittest.main()
