"""A retired fact must say WHY it was retired.

The retirement reason used to live only in the audit log. A recall
showed "INACTIVE" and nothing else, so the next agent could not tell a
deliberate retirement from a mistake -- and re-proposed the thing the
user had already rejected. SkillHone (EMNLP 2026 Industry Track)
measured what that decision history is worth: dropping it cost 13.4 and
10.9 accuracy points.

The reason now travels in the fact's own row (metadata.status_reason)
and rides inside the label, so every consumer sees it: the plugin
prints the content, the CLI prints the content.

Real server, real store, real read path -- nothing is mocked.
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
PY = sys.executable


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


class TestRetiredReason(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="astor_retired_reason_")
        cls._orig_astor_dir = os.environ.get("ASTOR_DIR")
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
        if cls._orig_astor_dir is None:
            os.environ.pop("ASTOR_DIR", None)
        else:
            os.environ["ASTOR_DIR"] = cls._orig_astor_dir

    def _write(self, content):
        return _post(self.base + "/v1/write", {
            "text": content, "user": "admin", "mode": "auto",
            "tier": "private", "scope": "long_term"})

    def _recall(self, query):
        return _post(self.base + "/v1/read",
                     {"query": query, "tier": "private", "user_id": "admin",
                      "top_k": 10})

    def _fact_id(self, marker):
        hits = [h for h in (self._recall(marker).get("results") or [])
                if marker in (h.get("content") or "")]
        self.assertTrue(hits, "the fact should be recallable")
        return hits[0].get("fact_id") or hits[0].get("id")

    def test_the_reason_shows_up_on_the_recalled_fact(self):
        marker = f"astor retired reason probe {time.time()}"
        self._write(marker)
        fid = self._fact_id(marker)

        reason = "the user said this approach does not work"
        r = _post(self.base + f"/v1/fact/{fid}/set_status",
                  {"status": "inactive", "reason": reason})
        self.assertTrue(r.get("ok"), r)

        hits = [h for h in (self._recall(marker).get("results") or [])
                if marker in (h.get("content") or "")]
        self.assertTrue(hits, "a retired fact is still recalled")
        content = hits[0].get("content") or ""
        self.assertIn("[INACTIVE]", content)
        self.assertIn(reason, content,
                      "the reason has to reach the reader, otherwise the "
                      "next agent re-proposes what was just rejected")

    def test_reactivating_clears_the_reason(self):
        marker = f"astor retired reason reactivate probe {time.time()}"
        self._write(marker)
        fid = self._fact_id(marker)

        reason = "temporary doubt about this fact"
        _post(self.base + f"/v1/fact/{fid}/set_status",
              {"status": "inactive", "reason": reason})
        _post(self.base + f"/v1/fact/{fid}/set_status",
              {"status": "active", "reason": "the user changed their mind"})

        hits = [h for h in (self._recall(marker).get("results") or [])
                if marker in (h.get("content") or "")]
        self.assertTrue(hits)
        content = hits[0].get("content") or ""
        self.assertNotIn(reason, content,
                         "the reason belonged to the retirement; once the "
                         "fact is current again the reason is stale")
        self.assertNotIn("[INACTIVE]", content)

    def test_a_demotion_without_a_reason_still_works(self):
        marker = f"astor retired reason empty probe {time.time()}"
        self._write(marker)
        fid = self._fact_id(marker)

        r = _post(self.base + f"/v1/fact/{fid}/set_status",
                  {"status": "inactive"})
        self.assertTrue(r.get("ok"), r)

        hits = [h for h in (self._recall(marker).get("results") or [])
                if marker in (h.get("content") or "")]
        self.assertTrue(hits)
        content = hits[0].get("content") or ""
        self.assertIn("[INACTIVE]", content)
        self.assertTrue(content.startswith("⚠[INACTIVE] "),
                        "with no reason the label stays the plain one")

    def _multi(self, query):
        return _post(self.base + "/v1/read/multi", {
            "query": query, "tier": "private", "user_id": "admin",
            "scopes": [{"tier": "private", "user_id": "admin", "weight": 1.0}],
            "top_k": 10})

    def test_the_multi_path_carries_the_same_contract(self):
        # The Hermes plugin reads /v1/read/multi, not /v1/read. Before
        # v1.17.51 the multi path did not label retired facts and did
        # not return their status, so in production a demotion was
        # invisible and the plugin's "reported as NOT WORKING" hint
        # could never fire.
        marker = f"astor retired reason multi probe {time.time()}"
        self._write(marker)
        fid = self._fact_id(marker)
        reason = "the multi path has to show this too"
        _post(self.base + f"/v1/fact/{fid}/set_status",
              {"status": "inactive", "reason": reason})

        r = self._multi(marker)
        hits = [h for h in (r.get("results") or [])
                if marker in (h.get("content") or "")]
        self.assertTrue(hits, "the multi path must return the fact")
        self.assertEqual(hits[0].get("status"), "inactive",
                         "the multi path must return the status field")
        self.assertIn(reason, hits[0].get("content") or "",
                      "the multi path must show the reason")

    def test_the_multi_path_sinks_the_retired_fact(self):
        stamp = time.time()
        shared = f"astormultisink {stamp}"
        self._write(f"{shared} alpha")
        self._write(f"{shared} beta")

        rows = self._multi(shared).get("results") or []
        ours = [r for r in rows if shared in (r.get("content") or "")]
        self.assertGreaterEqual(len(ours), 2,
                                "both facts must be in the result")
        # Retire whatever is on top. Which one that is does not matter --
        # what matters is that it WAS on top, so the sink is the only
        # thing that can move it.
        top_id = ours[0].get("fact_id")
        _post(self.base + f"/v1/fact/{top_id}/set_status",
              {"status": "inactive", "reason": "retired for the sink test"})

        rows2 = self._multi(shared).get("results") or []
        ours2 = [r for r in rows2 if shared in (r.get("content") or "")]
        self.assertGreaterEqual(len(ours2), 2)
        self.assertEqual(ours2[0].get("status"), "active",
                         "a retired fact must not stay on top")
        states = [1 if r.get("status") == "inactive" else 0 for r in ours2]
        self.assertEqual(states, sorted(states),
                         "a retired fact has to sit below the active ones")


    def _store_update(self, sql, args):
        """Straight into the store: the lexical index keeps the id, which
        is how a deleted fact came back in production."""
        for db in Path(self.tmp).rglob("*bus*.db"):
            try:
                c = sqlite3.connect(str(db))
                c.execute(sql, args)
                c.commit()
                c.close()
            except Exception:
                continue

    def test_the_multi_path_does_not_resurrect_a_tombstoned_fact(self):
        marker = f"astor multi tombstone probe {time.time()}"
        self._write(marker)
        fid = self._fact_id(marker)
        self._store_update(
            "UPDATE memory_canonical SET tombstoned = 1, tombstoned_at = ? "
            "WHERE id = ?", ("2026-01-01T00:00:00.000Z", fid))

        hits = [h for h in (self._multi(marker).get("results") or [])
                if marker in (h.get("content") or "")]
        self.assertFalse(hits,
                         "a tombstoned fact must not come back -- the "
                         "lexical index still holds its id")

    def test_the_multi_path_drops_an_expired_fact(self):
        marker = f"astor multi expiry probe {time.time()}"
        self._write(marker)
        fid = self._fact_id(marker)
        self._store_update(
            "UPDATE memory_canonical SET expires_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00.000Z", fid))

        hits = [h for h in (self._multi(marker).get("results") or [])
                if marker in (h.get("content") or "")]
        self.assertFalse(hits,
                         "a fact past its boundary must not be served -- "
                         "that is the whole point of the retirement edge")


if __name__ == "__main__":
    unittest.main()
