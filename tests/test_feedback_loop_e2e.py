"""The feedback loop, end to end.

The astor-feedback hook has never fired on a real fact. It fired once,
on a synthetic test fact, and the recall path did not even look at the
status it set. So the loop was unproven in both directions: the hook
half and the store half.

This runs the real hook process against a real astor instance and
walks the whole path:

    a fact is recalled          -> the hook records which facts fed the
                                   answer
    the user says "还是不行"     -> the hook demotes them
    the next recall             -> the fact is labelled, carries a
                                   boundary, and the agent is told

Nothing here is mocked: the hook is a subprocess reading JSON on
stdin, and astor is a server on a real port.
"""
from __future__ import annotations

import json
import os
import socket
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
MARKER = "wombat"


def _find_hook() -> Path | None:
    """The installed astor-feedback hook.

    It lives under the Hermes home, not in this repository. The Hermes
    home is derived rather than hardcoded, so nothing here names a
    machine.
    """
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    except Exception:
        env = os.environ.get("HERMES_HOME")
        if not env:
            return None
        home = Path(env)
    for candidate in (home / "hooks" / "astor-feedback" / "astor_feedback.py",):
        if candidate.exists():
            return candidate
    return None


HOOK = _find_hook()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@unittest.skipIf(HOOK is None,
                 "the astor-feedback hook is not installed on this host")
class TestFeedbackLoopEndToEnd(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.astor_dir = Path(cls._tmp.name)
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"

        cls._server = subprocess.Popen(
            [PY, "-m", "astor_memory.server", "--host", "127.0.0.1",
             "--port", str(cls.port)],
            cwd=str(REPO),
            env={**os.environ, "PYTHONPATH": str(REPO),
                 "ASTOR_DIR": str(cls.astor_dir)},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(90):
            try:
                with urllib.request.urlopen(f"{cls.base}/v1/health", timeout=3) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(1)
        else:
            raise RuntimeError("astor did not come up")

        # the hook keeps its state under $ASTOR_DIR/feedback, so a
        # separate dir keeps the test out of the live runtime's state
        cls._hook_state = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._hook_env = {
            **os.environ,
            "ASTOR_DIR": str(cls.astor_dir),
            "ASTOR_ENDPOINT": cls.base,
        }

    @classmethod
    def tearDownClass(cls):
        try:
            cls._server.terminate()
            cls._server.wait(timeout=10)
        except Exception:
            try:
                cls._server.kill()
            except Exception:
                pass
        try:
            cls._hook_state.cleanup()
        except OSError:
            pass
        try:
            cls._tmp.cleanup()
        except OSError:
            pass

    def _post(self, path, body):
        req = urllib.request.Request(
            f"{self.base}{path}", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

    def _run_hook(self, event, event_name):
        payload = dict(event)
        payload["hook_event_name"] = event_name
        r = subprocess.run(
            [PY, str(HOOK)],
            input=json.dumps(payload).encode(),
            capture_output=True, env=self._hook_env, timeout=60,
        )
        out = (r.stdout or b"").decode("utf-8", "replace").strip()
        try:
            return json.loads(out.splitlines()[-1])
        except Exception:
            return {"raw": out, "stderr": (r.stderr or b"").decode("utf-8", "replace")}

    def test_the_whole_loop(self):
        # 1. a fact exists and is recalled
        w = self._post("/v1/write", {
            "text": f"a fact about {MARKER} that the user will reject",
            "tier": "private", "user": "admin", "mode": "regex", "kind": "fact",
        })
        fids = w.get("fact_ids") or []
        self.assertTrue(fids, w)
        fid = fids[0]

        r = self._post("/v1/read", {
            "query": MARKER, "tier": "private", "user": "admin", "top_k": 5,
        })
        hits = [h for h in (r.get("results") or []) if h.get("fact_id") == fid]
        self.assertTrue(hits, r)
        self.assertEqual(hits[0].get("status"), "active", hits[0])

        # 2. the hook sees the recall and records the fact
        session = "e2e-feedback-loop"
        res = self._run_hook({
            "session_id": session,
            "tool_name": "astor_recall",
            "result": json.dumps({"results": [
                {"fact_id": fid, "content": hits[0].get("content")}]}),
        }, "post_tool_call")
        self.assertEqual(res.get("action"), "recorded", res)
        self.assertIn(fid, res.get("facts") or [], res)

        # 3. the turn ends. The hook rotates: what was recalled becomes
        #    "used", and "used" is what the NEXT turn's verdict judges.
        #    This is the "one turn apart" the hook's docstring describes
        #    -- the user says "still not working" in turn N+1 about the
        #    answer turn N produced. Without this rotation there is
        #    nothing to demote yet, which is why the hook has to be
        #    driven through a whole turn before a verdict lands.
        res = self._run_hook({
            "session_id": session,
            "extra": {"user_message": ""},
        }, "post_llm_call")
        self.assertEqual(res.get("action"), "skip", res)

        # 4. the next turn recalls, and the user says it did not work
        res = self._run_hook({
            "session_id": session,
            "tool_name": "astor_recall",
            "result": json.dumps({"results": [
                {"fact_id": fid, "content": hits[0].get("content")}]}),
        }, "post_tool_call")
        self.assertEqual(res.get("action"), "recorded", res)

        res = self._run_hook({
            "session_id": session,
            "extra": {"user_message": "还是不行"},
        }, "post_llm_call")
        self.assertEqual(res.get("action"), "demoted", res)
        self.assertIn(fid, res.get("facts") or [], res)

        # 5. the next recall says so, and the fact carries a boundary
        r = self._post("/v1/read", {
            "query": MARKER, "tier": "private", "user": "admin", "top_k": 5,
        })
        hits = [h for h in (r.get("results") or []) if h.get("fact_id") == fid]
        self.assertTrue(hits, "the fact must stay recallable")
        self.assertIn("[INACTIVE]", hits[0].get("content") or "", hits[0])
        self.assertEqual(hits[0].get("status"), "inactive", hits[0])

        import sqlite3
        dbs = sorted(self.astor_dir.rglob("*bus*.db"))
        self.assertTrue(dbs, f"no bus db under {self.astor_dir}")
        row = None
        for db in dbs:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True,
                                  timeout=30)
            try:
                row = con.execute(
                    "SELECT scope_type, expires_at FROM memory_canonical "
                    "WHERE id=?", (fid,)).fetchone()
            except sqlite3.Error:
                row = None
            con.close()
            if row:
                break
        self.assertIsNotNone(row, "the fact row was not found in any tier")
        self.assertEqual(row[0], "short_term", row)
        self.assertIsNotNone(row[1], row)


if __name__ == "__main__":
    unittest.main()
