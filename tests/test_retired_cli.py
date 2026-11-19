"""The retired-fact queue: a retirement is visible, and reversible.

A fact the operator reported as not working stays in the store -- it is
still recallable, it is marked [INACTIVE], and it ages out on its own.
What it loses is being served as current truth.

Two things have to hold for that to be trustworthy:

  * the reason is recorded. Without it there is no way to tell a
    deliberate retirement from a mistake, and nothing to show the
    operator. It goes in the tier database that holds the fact, so the
    fact and its reason are read together.
  * it can be undone. A retirement that cannot be reversed is a
    deletion with extra steps.

This runs a real server on a temporary ASTOR_DIR and drives the real
CLI functions against it. Nothing is mocked.
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
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# the interpreter running the tests is the one the server should use
PY = sys.executable


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class TestRetiredQueue(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # the directory conftest already isolated this process into. The
        # server subprocess and the in-process CLI have to share it, and
        # making a second one would mean mutating ASTOR_DIR out from
        # under every other test in the run.
        cls.tmp = os.environ["ASTOR_DIR"]
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.env = {**os.environ, "ASTOR_DIR": cls.tmp}
        cls.proc = subprocess.Popen(
            [PY, "-u", "-m", "astor_memory.server",
             "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=str(REPO), env=cls.env,
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

    def _post(self, path, payload):
        body = json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    def _write(self, text):
        return self._post("/v1/write", {
            "text": text, "user": "admin", "mode": "auto",
            "tier": "private", "scope": "long_term"})

    def _admin_db(self):
        p = Path(self.tmp) / "users" / "admin" / "memory" / "astor_bus_admin.db"
        return p

    def _audit_rows(self, fact_id):
        p = self._admin_db()
        if not p.exists():
            return []
        c = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=30)
        try:
            return c.execute(
                "SELECT reason FROM audit_log WHERE event='set_status' "
                "AND target_id=? ORDER BY id DESC",
                (str(fact_id),)).fetchall()
        finally:
            c.close()

    def test_a_retirement_records_the_reason_next_to_the_fact(self):
        marker = f"retired queue probe {time.time()}"
        w = self._write(marker)
        fid = w["fact_ids"][0]

        r = self._post(f"/v1/fact/{fid}/set_status",
                       {"status": "inactive",
                        "reason": "the user reported this did not work"})
        self.assertTrue(r.get("ok"), r)

        rows = self._audit_rows(fid)
        self.assertTrue(rows,
                        "the reason has to be recorded, or a deliberate "
                        "retirement is indistinguishable from a mistake")
        self.assertIn("did not work", rows[0][0])

    def test_am_retired_lists_it_and_am_reactivate_restores_it(self):
        marker = f"retired cli probe {time.time()}"
        w = self._write(marker)
        fid = w["fact_ids"][0]
        self._post(f"/v1/fact/{fid}/set_status",
                   {"status": "inactive",
                    "reason": "cli round trip probe"})

        # the CLI reads the tier database in process
        import importlib
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        cli_main = importlib.import_module("astor_memory.cli.main")

        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli_main.cmd_retired(type("A", (), {
                "limit": 20, "tier": "all", "user": "admin"})())
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn(f"#{fid}", out, out)
        self.assertIn("cli round trip probe", out, out)

        # and reactivate puts it back
        buf2 = io.StringIO()
        os.environ["ASTOR_ENDPOINT"] = self.base
        with contextlib.redirect_stdout(buf2):
            rc2 = cli_main.cmd_reactivate(type("A", (), {
                "fact_id": fid, "reason": "restored by the test"})())
        self.assertEqual(rc2, 0, buf2.getvalue())

        after = self._post("/v1/read", {
            "query": marker, "tier": "private", "user_id": "admin",
            "top_k": 5})
        hits = [h for h in (after.get("results") or [])
                if marker in (h.get("content") or "")]
        self.assertTrue(hits)
        self.assertNotEqual(hits[0].get("status"), "inactive",
                            "the fact should be current truth again")


if __name__ == "__main__":
    unittest.main()
