"""Two ways to delete a fact, and one way to bring it back.

`/v1/bitemporal/cascade_forget` had no authorization at all: the tier
came from the request body and the bus was opened with it, so any
caller could tombstone any fact in any tier -- including another
user's private one.

And a hard forget did not cancel the fact's queued cascade rows, so
the daily replay pass re-indexed the deleted content and the
"forgotten" fact came back searchable.

Real server, real bus, real cascade replay -- nothing is mocked. The
classifier may route a fact out of the requested tier, so the tests
find where it actually landed instead of assuming.
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


class TestForgetAndCascade(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="astor_forget_acl_")
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

    # -- helpers -----------------------------------------------------

    @staticmethod
    def _bus(tier, user):
        from astor_memory.bus.store import astor_bus
        return astor_bus(tier=tier, user_id=user)

    def _write(self, text, tier="public", user="admin"):
        st, w = _post(self.base + "/v1/write", {
            "text": text, "user": user, "mode": "auto",
            "tier": tier, "scope": "long_term"})
        self.assertEqual(st, 200, w)
        fids = w.get("fact_ids") or []
        self.assertTrue(fids, w)
        fid = int(fids[0])
        # the classifier may route a fact out of the requested tier, so
        # find where it actually landed
        for t, u in (("public", None), ("private", "admin")):
            bus = self._bus(t, u)
            if bus.conn.execute(
                    "SELECT 1 FROM memory_canonical WHERE id = ?", (fid,)).fetchone():
                return fid, t, u
        return fid, tier, user

    def _public_fact(self, text):
        """A fact in the PUBLIC tier, written directly.

        The write endpoint may route a fact out of the requested tier
        when the classifier calls it personal. That would hide the hole
        this test is about: the ACL refuses to open another user's
        private bus anyway, so a probe that lands in private proves
        nothing. The public tier has no per-user gate -- that is the
        whole point of this endpoint's tier coming from the body.
        """
        bus = self._bus("public", None)
        ev = bus.append_event(namespace="acl_probe", agent_id="test",
                              source="test", action="write", content=text)
        cid = bus.insert_candidate(event_id=ev, namespace="acl_probe",
                                   content=text)
        out = bus.promote_candidate(cid, promoted_by="test",
                                    visibility="commons")
        fid = out.get("fact_id") if isinstance(out, dict) else out
        fid = int(fid)
        row = bus.conn.execute(
            "SELECT id FROM memory_canonical WHERE id = ?", (fid,)).fetchone()
        self.assertIsNotNone(row, f"probe fact {fid} is not in the public bus")
        return fid

    def _enqueue_cascade(self, tier, user, fact_id, content="x"):
        from astor_memory.bus.cascade import enqueue
        bus = self._bus(tier, user)
        return enqueue(bus, fact_id=fact_id, operation="embed_insert",
                       tier=tier, user_id=user,
                       payload={"content": content}, error="")

    @staticmethod
    def _row(bus, fid):
        return bus.conn.execute(
            "SELECT tombstoned FROM memory_canonical WHERE id = ?", (fid,)).fetchone()

    # -- M6: cascade_forget is not an open door -----------------------

    def test_cascade_forget_refuses_a_non_admin(self):
        fid = self._public_fact("cascade forget acl probe")
        st, body = _post(self.base + "/v1/bitemporal/cascade_forget",
                          {"fact_id": fid, "tier": "public"},
                          headers={"X-Actor": "user:sunday"})
        self.assertEqual(st, 403,
                         f"a non-admin may not cascade-forget: {body}")
        row = self._row(self._bus("public", None), fid)
        self.assertIsNotNone(row, "the fact should still be there")
        self.assertEqual(row[0], 0, "the fact must not be tombstoned")

    def test_cascade_forget_still_works_for_an_admin(self):
        fid = self._public_fact("cascade forget admin probe")
        st, body = _post(self.base + "/v1/bitemporal/cascade_forget",
                          {"fact_id": fid, "tier": "public"},
                          headers={"X-Actor": "admin:admin"})
        self.assertEqual(st, 200, body)
        row = self._row(self._bus("public", None), fid)
        self.assertEqual(row[0], 1, "the admin's request should tombstone it")

    # -- M25: a forgotten fact stays forgotten ------------------------

    def test_forget_cancels_the_facts_queued_cascade_rows(self):
        fid = self._public_fact("cascade row cancel probe")
        tier, user = "public", None
        self._enqueue_cascade(tier, user, fid,
                              "the payload that must not come back")
        bus = self._bus(tier, user)
        n = bus.conn.execute(
            "SELECT count(*) FROM cascade_state WHERE fact_id = ?", (fid,)).fetchone()[0]
        self.assertEqual(n, 1, "the cascade row should be queued")

        st, body = _post(self.base + "/v1/forget",
                          {"fact_id": fid, "tier": tier, "user_id": user},
                          headers={"X-Actor": "admin:admin"})
        self.assertEqual(st, 200, body)

        n = bus.conn.execute(
            "SELECT count(*) FROM cascade_state WHERE fact_id = ?", (fid,)).fetchone()[0]
        self.assertEqual(n, 0,
                         "the queued cascade row must be cancelled with the "
                         "fact, or the replay re-indexes deleted content")

    def test_replay_skips_a_fact_that_was_forgotten(self):
        from astor_memory.bus.cascade import replay_one
        fid = self._public_fact("cascade replay skip probe")
        tier, user = "public", None
        row_id = self._enqueue_cascade(tier, user, fid,
                                       "content that must not be re-indexed")
        bus = self._bus(tier, user)
        # the fact dies, but the row survives (an older build, or a row
        # enqueued after the forget)
        bus.conn.execute(
            "UPDATE memory_canonical SET tombstoned = 1 WHERE id = ?", (fid,))
        bus.conn.commit()

        out = replay_one(bus, int(row_id))
        self.assertEqual(out.get("status"), "skipped", out)


if __name__ == "__main__":
    unittest.main()
