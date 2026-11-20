"""Three policies that were asked the wrong question.

- the async write path asked the classifier as if every user may
  share, so a user whose `allow_commons_write` is 0 could still
  publish to the commons tier by writing asynchronously;
- a grant's expiry was compared as a STRING, so an offset format
  ("...T12:00:00+08:00") sorted wrong and the grant stayed
  authorized hours past its deadline;
- the MCP server swallowed a trust rejection and went on to observe
  the turn anyway -- it failed open.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _post(url, payload, timeout=30, headers=None):
    body = json.dumps(payload).encode()
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


class TestWritePolicy(unittest.TestCase):

    # -- M9: a grant expires when it says it does ------------------

    def test_an_offset_formatted_grant_expires_on_time(self):
        from astor_memory._internal import grants
        tmp = tempfile.mkdtemp(prefix="astor_grants_")
        # point the module at our own db, not the live one
        grants._grant_path = lambda: Path(tmp) / "grants.db"
        grants._conn = None
        self.addCleanup(setattr, grants, "_conn", None)

        # expired an hour ago, written in a +08:00 offset
        exp = (datetime.now(timezone.utc) - timedelta(hours=1)).astimezone(
            timezone(timedelta(hours=8))).replace(microsecond=0)
        exp_s = exp.isoformat()
        conn = grants._conn_lazy()
        conn.execute(
            "INSERT INTO grants (grantor, grantee, scope, expires_at, revoked, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            ("owner", "user:reader", "read", exp_s,
             datetime.now(timezone.utc).isoformat()))
        conn.commit()

        self.assertFalse(
            grants.check_grant("owner", "user:reader", "read"),
            f"a grant that expired at {exp_s} is still authorized")

    # -- M14: the MCP server fails closed on a trust rejection -----

    def test_a_trust_rejection_stops_the_observation(self):
        from astor_memory import mcp_server_extension as ext

        class _FakeModule:
            """The bits patched_call_tool touches."""

            def __init__(self):
                self._enforce_trust_called = False

            def _enforce_trust(self, arguments):
                self._enforce_trust_called = True
                raise PermissionError("namespace not trusted")

            def list_tools(self):
                return {"tools": []}

            def call_tool(self, name, arguments):
                raise AssertionError("the original call_tool must not run")

        fake = _FakeModule()
        ext._PATCH_DONE = False
        self.addCleanup(setattr, ext, "_PATCH_DONE", False)
        ext.install(fake)

        out = fake.call_tool("astor_auto_observe", {
            "text": "a sentence long enough to be observed by the tool",
            "user_id": "admin"})
        self.assertTrue(out.get("isError"), out)
        self.assertIn("trust rejected", json.dumps(out))
        self.assertTrue(fake._enforce_trust_called)


class TestAsyncWritePolicy(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="astor_async_policy_")
        cls._orig = os.environ.get("ASTOR_DIR")
        os.environ["ASTOR_DIR"] = cls.tmp
        # bot-binding.db: one user who may not share, one who may
        db = Path(cls.tmp) / "bot-binding.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS user_meta ("
            "user_id TEXT PRIMARY KEY, short_alias TEXT UNIQUE NOT NULL,"
            "role TEXT NOT NULL DEFAULT 'user',"
            "active INTEGER NOT NULL DEFAULT 1,"
            "allow_commons_write INTEGER NOT NULL DEFAULT 1)")
        conn.execute(
            "INSERT OR REPLACE INTO user_meta "
            "(user_id, short_alias, role, active, allow_commons_write) "
            "VALUES ('noshare', 'noshare', 'user', 1, 0)")
        conn.execute(
            "INSERT OR REPLACE INTO user_meta "
            "(user_id, short_alias, role, active, allow_commons_write) "
            "VALUES ('sharer', 'sharer', 'user', 1, 1)")
        conn.commit()
        conn.close()

        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.proc = subprocess.Popen(
            [PY, "-u", "-m", "astor_memory.server",
             "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=str(REPO), env={**os.environ, "ASTOR_DIR": cls.tmp},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(cls.base + "/v1/health", timeout=5) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("server did not come up")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.proc.terminate()
            cls.proc.wait(timeout=15)
        except Exception:
            try:
                cls.proc.kill()
            except Exception:
                pass
        if cls._orig is None:
            os.environ.pop("ASTOR_DIR", None)
        else:
            os.environ["ASTOR_DIR"] = cls._orig

    def _public_has(self, text):
        from astor_memory.bus.store import astor_bus
        bus = astor_bus(tier="public", user_id=None)
        n = bus.conn.execute(
            "SELECT count(*) FROM memory_canonical WHERE content LIKE ?",
            (f"%{text}%",)).fetchone()[0]
        return n

    def _private_has(self, text):
        """Look in the user's own private store.

        The bus API would refuse: the admin may not read another
        user's private bucket (strict privacy model). The test may --
        it reads the file directly.
        """
        import glob
        total = 0
        for db in glob.glob(str(Path(os.environ["ASTOR_DIR"]) / "**" / "*.db"),
                            recursive=True):
            conn = sqlite3.connect(db)
            try:
                n = conn.execute(
                    "SELECT count(*) FROM memory_canonical WHERE content LIKE ?",
                    (f"%{text}%",)).fetchone()[0]
            except Exception:
                n = 0
            conn.close()
            if n:
                print(f"  (fact found in {db})")
                total += n
        return total

    def _stub_extractor(self):
        """A fact the classifier WILL call commons.

        Without an LLM the extractor returns kind='fact', and kind
        'fact' is not on the commons list -- so the switch would never
        be consulted and the test would pass for the wrong reason.
        The async path imports the extractor inside its thread, so
        patching the module attribute is enough.
        """
        from astor_memory.forge import extractor as ex

        class _F:
            def __init__(self, text):
                self.content = text
                self.kind = "lesson"
                self.confidence = 0.9

        def _fake(text, **kw):
            return [_F(text)]

        original = ex.astor_extract_facts
        ex.astor_extract_facts = _fake
        self.addCleanup(setattr, ex, "astor_extract_facts", original)

    def test_an_async_write_by_a_user_who_may_not_share_stays_private(self):
        self._stub_extractor()
        text = "always re-read the lockfile before installing a package"
        st, body = _post(self.base + "/v1/write", {
            "text": text, "user": "noshare", "mode": "auto",
            "tier": "public", "scope": "long_term", "async_write": True})
        self.assertIn(st, (200, 202), body)

        # the extract + promote happens in a daemon thread
        deadline = time.time() + 25
        while time.time() < deadline:
            if self._public_has(text):
                break
            time.sleep(0.5)
        self.assertEqual(
            self._public_has(text), 0,
            "a user whose allow_commons_write is 0 published to the public "
            "tier through the async write path")
        self.assertEqual(
            self._private_has(text), 1,
            "the fact should have been kept -- private, not dropped")

    def test_the_same_write_by_a_user_who_may_share_is_public(self):
        # the control: the same text, with the switch on, lands in the
        # public tier. Without this the test above would pass for the
        # wrong reason.
        self._stub_extractor()
        text = "always verify the checksum of a downloaded artifact"
        st, body = _post(self.base + "/v1/write", {
            "text": text, "user": "sharer", "mode": "auto",
            "tier": "public", "scope": "long_term", "async_write": True})
        self.assertIn(st, (200, 202), body)
        deadline = time.time() + 25
        while time.time() < deadline:
            if self._public_has(text):
                break
            time.sleep(0.5)
        self.assertEqual(
            self._public_has(text), 1,
            "the control write should reach the public tier -- otherwise "
            "the previous test proves nothing")


if __name__ == "__main__":
    unittest.main()
