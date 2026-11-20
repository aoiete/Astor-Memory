"""The server's stderr must not become a copy of the memory.

Two log lines carried the text itself: the REUSE warning (120 chars of
the user's write) and the coref line (the whole text, repr'd). Both
were unconditional -- stderr, not a debug flag. Logs outlive the
operation that made them and are readable by a wider audience than the
data's own access path.

The line still says enough to be useful: the length and a hash of the
text.
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
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable
SECRET = "XYZTOKEN123"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _post(url, payload, timeout=30):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


class TestServerLogsNoRawText(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="astor_log_")
        cls._orig = os.environ.get("ASTOR_DIR")
        os.environ["ASTOR_DIR"] = cls.tmp
        cls.errfile = Path(cls.tmp) / "server.err"
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        err = open(cls.errfile, "w", encoding="utf-8")
        cls.proc = subprocess.Popen(
            [PY, "-u", "-m", "astor_memory.server",
             "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=str(REPO), env={**os.environ, "ASTOR_DIR": cls.tmp},
            stdout=err, stderr=err, text=True)
        cls._err = err
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
            cls._err.close()
        except Exception:
            pass
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

    def _stderr(self):
        try:
            return self.errfile.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

    def test_the_reuse_warning_does_not_print_the_text(self):
        # 'moomoo' + 'place_order' trips the REUSE pattern check
        text = f"moomoo place_order for the account holding {SECRET}"
        st, body = _post(self.base + "/v1/write", {
            "text": text, "user": "admin", "mode": "auto",
            "tier": "public", "scope": "long_term"})
        self.assertEqual(st, 200, body)
        time.sleep(1.5)
        err = self._stderr()
        self.assertIn("REUSE pattern detected", err,
                      "the warning should still be there")
        self.assertNotIn(SECRET, err,
                         "the raw text reached the server log")
        self.assertNotIn("place_order for the account", err)

    def test_the_log_line_still_says_something_useful(self):
        from astor_memory.server import _text_fingerprint
        fp = _text_fingerprint("hello world")
        self.assertIn("len=11", fp)
        self.assertNotIn("hello", fp)
        self.assertEqual(fp, _text_fingerprint("hello world"),
                         "the same text must give the same fingerprint")


if __name__ == "__main__":
    unittest.main()
