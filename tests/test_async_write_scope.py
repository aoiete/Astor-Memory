"""The async write path graded its scope like the sync path does.

Since v1.16.92 the sync path decides the scope by evidence: a fact with
no evidence of being worth keeping gets the temporary tier and an
expiry. The async path still hard-coded scope_type='long_term', so a
fact written asynchronously never got the temporary tier and never got
an expiry -- the one path where a memory could not expire.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
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


class TestAsyncWriteScope(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="astor_async_scope_")
        os.environ['ASTOR_DIR'] = cls.tmp
        db = Path(cls.tmp) / "bot-binding.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS user_meta ("
            "user_id TEXT PRIMARY KEY, short_alias TEXT UNIQUE NOT NULL,"
            "role TEXT NOT NULL DEFAULT 'user',"
            "active INTEGER NOT NULL DEFAULT 1,"
            "allow_commons_write INTEGER NOT NULL DEFAULT 1,"
            "subscription_plan TEXT DEFAULT 'free')")
        conn.execute(
            "INSERT OR REPLACE INTO user_meta "
            "(user_id, short_alias, role, active, allow_commons_write) "
            "VALUES ('admin', 'admin', 'admin', 1, 1)")
        conn.commit()
        conn.close()

        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.proc = subprocess.Popen(
            [PY, "-u", "-m", "astor_memory.server",
             "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=str(REPO), env={**os.environ, "ASTOR_DIR": cls.tmp},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        cls.base + "/v1/health", timeout=2):
                    return
            except Exception:
                if cls.proc.poll() is not None:
                    out = cls.proc.stderr.read().decode('utf-8', 'replace')
                    raise RuntimeError("server died:\n" + out[-3000:])
                time.sleep(0.3)
        raise RuntimeError("server did not come up")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.proc.terminate()
            cls.proc.wait(timeout=10)
        except Exception:
            try:
                cls.proc.kill()
            except Exception:
                pass

    def _scope_of(self, needle: str):
        """The scope_type of the stored fact, wherever it landed."""
        for db in (Path(self.tmp) / "public" / "memory" /
                   "astor_bus_public.db",
                   Path(self.tmp) / "users" / "admin" / "memory" /
                   "astor_bus_admin.db"):
            if not db.exists():
                continue
            conn = sqlite3.connect(str(db))
            try:
                rows = conn.execute(
                    "SELECT scope_type, expires_at FROM memory_canonical "
                    "WHERE tombstoned = 0 AND content LIKE ?",
                    (f"%{needle}%",)).fetchall()
            except sqlite3.Error:
                continue
            finally:
                conn.close()
            for row in rows:
                return row
        return None

    def _stub_extractor(self):
        from astor_memory.forge import extractor as ex

        class _F:
            def __init__(self, text):
                self.content = text
                self.kind = "fact"
                self.confidence = 0.9
                self.importance = 0.3
                self.provenance_kind = "user_write"

        def _fake(text, **kw):
            return [_F(text)]

        original = ex.astor_extract_facts
        ex.astor_extract_facts = _fake
        self.addCleanup(setattr, ex, "astor_extract_facts", original)

    def test_an_async_write_honours_the_caller_scope(self):
        """scope='short_term' has to be honoured on the async path."""
        self._stub_extractor()
        needle = ("an async note about the temporary scratch policy that "
                  "should land in the temporary tier")
        st, body = _post(self.base + "/v1/write", {
            "text": needle, "user": "admin", "tier": "public",
            "mode": "auto", "scope": "short_term", "async_write": True})
        self.assertIn(st, (200, 202), body)

        deadline = time.time() + 25
        while time.time() < deadline:
            if self._scope_of(needle):
                break
            time.sleep(0.5)
        found = self._scope_of(needle)
        self.assertIsNotNone(found, "the async fact was never stored")
        self.assertEqual(
            found[0], "short_term",
            "the async write ignored the caller's scope -- it hard-coded "
            "long_term, so an async memory could never expire")


if __name__ == "__main__":
    unittest.main()
