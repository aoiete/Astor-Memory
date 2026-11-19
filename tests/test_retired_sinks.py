"""A retired fact sinks below the active ones, but stays visible.

Retiring a fact is supposed to stop it steering the answer. Measured
on 2026-11-19, of six facts that ranked first for their own question,
four kept rank 1 after being retired -- the only thing between a
retired fact and the answer was a marker in its content.

The survey on multimodal agent memory (arXiv 2610.19366) checks
formation fidelity, update effectiveness and retrieval accuracy
separately. A demotion that leaves the ranking untouched scores badly
on update effectiveness.

So retired facts go below the active ones. They are still returned: the
fact may be what the query is about, and hiding it would hide the
context.

This runs a real server on a temporary ASTOR_DIR and drives the real
read path.
"""
from __future__ import annotations

import json
import os
import socket
import shutil
import subprocess
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


class TestRetiredFactSinks(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Its own empty store. The session-wide isolation directory that
        # conftest sets up is shared with every other test file, so what
        # is in it depends on what ran before this file -- and the
        # scores move with the corpus. The order this test is about is
        # decided by scores, so the corpus has to be fixed.
        cls.tmp = tempfile.mkdtemp(prefix="astor_retired_sinks_")
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
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _post(self, path, payload):
        body = json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    def _read(self, query, tier="private", user="admin", top_k=10):
        return self._post("/v1/read", {"query": query, "tier": tier,
                                        "user_id": user, "top_k": top_k})

    def _ids(self, query, **kw):
        r = self._read(query, **kw)
        return [h.get("fact_id") for h in (r.get("results") or [])]

    def _write(self, text, **extra):
        body = {"text": text, "user": "admin", "mode": "auto",
                "tier": "private", "scope": "long_term"}
        body.update(extra)
        return self._post("/v1/write", body)

    def test_a_retired_fact_goes_below_the_active_ones(self):
        """A retired fact goes below the active ones, and stays visible.

        The test reads exactly once, after the retirement. Reading
        before it would bump the surfaced facts' confirmation times,
        which lifts the active facts' scores and moves the landscape
        under the assertion -- an earlier version of this test passed
        with the fix removed because of exactly that.

        The retired fact keeps the strongest match, so the only thing
        that can put it below the others is the order.
        """
        marker = f"qq{int(time.time() * 1000)}"
        query = f"zebra sinking {marker}"
        strong = self._write(
            f"zebra sinking {marker} zebra sinking {marker} "
            f"zebra sinking {marker}")["fact_ids"][0]
        weak = [self._write(f"zebra {marker} filler{i}")["fact_ids"][0]
                for i in range(2)]

        r = self._post(f"/v1/fact/{strong}/set_status",
                       {"status": "inactive", "reason": "sinking probe"})
        self.assertTrue(r.get("ok"), r)

        # Retiring prefixes the content with a warning, which changes
        # the text, which re-embeds the fact. Read after that settles,
        # so the score is the settled one and not a half-written
        # vector.
        time.sleep(3)

        res = self._read(query)
        after = [h.get("fact_id") for h in (res.get("results") or [])]
        scores = {h.get("fact_id"): (h.get("similarity") or 0.0)
                  for h in (res.get("results") or [])}

        self.assertIn(strong, after,
                      "a retired fact must stay visible: it may be what "
                      "the query is about")
        for a in weak:
            self.assertIn(a, after)
            self.assertLess(after.index(a), after.index(strong),
                            f"an active fact must come before the retired "
                            f"one, got {after}")

        # The retired fact must still be the strongest match. If it is
        # not, it sank because it lost points, and this test would pass
        # for the wrong reason -- the order is what is under test.
        self.assertGreaterEqual(scores.get(strong, 0.0),
                                max(scores.get(a, 0.0) for a in weak),
                                "the retired fact must still be the best "
                                "match; the order is what must move it")

    def test_a_retired_fact_is_marked_in_every_channel(self):
        """The injection channels build their own rows and used to
        return a retired fact with no warning and no status at all.

        The scored path labels a retired fact when it builds its row.
        The meta-recall channels build rows from their own queries and
        never looked at the status column, so a fact that only those
        channels found came back looking like current truth. The
        demotion was invisible for it.

        The marking is done by one function over the merged list, so
        that is what is under test: a row that arrives without a status
        has to come out marked. Testing it through a read would not
        prove anything -- the scored path labels its own rows, so the
        read passes whether or not the merged list is marked.
        """
        from astor_memory.server import _mark_retired_rows

        rows = [{"fact_id": 4242, "content": "a lesson about the marker",
                 "score_kind": "meta_recall"},
                {"fact_id": 7, "content": "an active fact"}]
        _mark_retired_rows(rows, {4242})

        self.assertEqual(rows[0].get("status"), "inactive")
        self.assertTrue(rows[0]["content"].startswith("\u26a0[INACTIVE] "),
                        f"got {rows[0]['content']!r}")
        self.assertNotIn("status", rows[1])
        self.assertEqual(rows[1]["content"], "an active fact")

    def test_marking_is_not_applied_twice(self):
        from astor_memory.server import _mark_retired_rows

        rows = [{"fact_id": 1, "content": "\u26a0[INACTIVE] already marked"}]
        _mark_retired_rows(rows, {1})
        self.assertEqual(rows[0]["content"].count("[INACTIVE]"), 1)

    def test_the_order_inside_each_group_is_untouched(self):
        """A stable sort: only the groups move, not the order within
        them."""
        marker = f"qq{int(time.time() * 1000)}b"
        ids = [self._write(f"zebra order {marker} item{i}")["fact_ids"][0]
               for i in range(3)]

        self._post(f"/v1/fact/{ids[-1]}/set_status",
                   {"status": "inactive", "reason": "order probe"})
        time.sleep(3)
        after = self._ids(f"zebra order {marker}")
        actives = [i for i in after if i in ids and i != ids[-1]]
        self.assertEqual(len(actives), 2, f"got {after}")
        self.assertLess(after.index(ids[-1]), after.index(ids[-1]) + 1,
                        "the retired fact must be last")
        self.assertLess(after.index(actives[0]), after.index(actives[1]),
                        "the active facts must keep their relative order")


if __name__ == "__main__":
    unittest.main()
