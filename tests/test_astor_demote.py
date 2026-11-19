"""astor_demote: retire a memory the user just rejected.

The feedback hook covers a short verdict ("还是不行"). It cannot cover
a rejection that arrives as a long technical report, and for this
operator that is the normal shape of dissatisfaction. The agent is
already in the conversation and can make the judgement, so the caller
makes the judgement and calls astor_demote.

This test runs the real server against a temporary ASTOR_DIR and
checks the whole path:

  write a fact -> demote it -> recall it -> it comes back marked,
  with the status field set, and it does not get renewed.

Nothing here is mocked: the HTTP endpoint, the store, the read path
and the cache are all real.
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

REPO = Path(r"D:/AI/astor-memory")
PY = r"D:/AI/PY-311/Scripts/python.exe"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _post(url, payload, timeout=30):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class TestAstorDemote(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="astor_demote_")
        os.environ["ASTOR_DIR"] = cls.tmp
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

    def _write_fact(self, content):
        r = _post(self.base + "/v1/write", {
            "text": content, "user": "admin", "mode": "auto",
            "tier": "private", "scope": "long_term"})
        return r

    def _recall(self, query):
        return _post(self.base + "/v1/read",
                     {"query": query, "tier": "private", "user_id": "admin",
                      "top_k": 10})

    def test_demote_marks_the_fact_and_the_recall_says_so(self):
        marker = f"astor demote probe {time.time()}"
        w = self._write_fact(marker)
        self.assertTrue(w.get("ok") or w.get("fact_ids"), w)

        before = self._recall(marker)
        hits = [h for h in (before.get("results") or [])
                if marker in (h.get("content") or "")]
        self.assertTrue(hits, "the fact should be recallable before the demotion")
        fid = hits[0].get("fact_id") or hits[0].get("id")

        r = _post(self.base + f"/v1/fact/{fid}/set_status",
                  {"status": "inactive",
                   "reason": "the user reported this did not work"})
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r.get("status"), "inactive")

        after = self._recall(marker)
        hits2 = [h for h in (after.get("results") or [])
                 if marker in (h.get("content") or "")]
        self.assertTrue(hits2, "the fact should still be recallable")
        h = hits2[0]
        self.assertEqual(h.get("status"), "inactive",
                         "the status has to travel as a field, not as a "
                         "prefix the caller has to grep for")
        self.assertIn("[INACTIVE]", h.get("content") or "",
                      "the content has to say so too")

    def test_a_demoted_fact_does_not_get_renewed(self):
        marker = f"astor demote boundary probe {time.time()}"
        self._write_fact(marker)
        hits = [h for h in (self._recall(marker).get("results") or [])
                if marker in (h.get("content") or "")]
        self.assertTrue(hits)
        fid = hits[0].get("fact_id") or hits[0].get("id")

        _post(self.base + f"/v1/fact/{fid}/set_status",
              {"status": "inactive", "reason": "boundary probe"})

        after = [h for h in (self._recall(marker).get("results") or [])
                 if marker in (h.get("content") or "")]
        self.assertTrue(after)
        # a demotion narrows the fact into the temporary layer with an
        # expiry, so it ages out instead of sitting there forever
        self.assertTrue(after[0].get("valid_until"),
                        "a retired fact needs a boundary, or it is only "
                        "decorated and never retires")
        self.assertEqual(after[0].get("provenance_kind"), "short_term",
                         "a retired fact belongs in the temporary layer")

    def test_the_adapter_exposes_the_tool(self):
        sys.path.insert(0, str(REPO))
        from astor_memory.hermes_adapter import AstorMemoryProvider
        p = AstorMemoryProvider()
        names = [s["name"] for s in p.get_tool_schemas()]
        self.assertIn("astor_demote", names)
        for s in p.get_tool_schemas():
            if s["name"] == "astor_demote":
                props = s["parameters"]["properties"]
                self.assertIn("fact_ids", props)
                self.assertIn("reason", props)
                return
        self.fail("astor_demote schema not found")


if __name__ == "__main__":
    unittest.main()
