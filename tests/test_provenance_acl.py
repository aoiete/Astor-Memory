"""The provenance routes must not cross the tenant boundary.

The endpoints that answer "where did this fact come from" --
`/v1/fact/<id>/provenance`, `/lineage`, `/graph.dot`, `/versions` --
take `tier` and `user_id` from the query string. Until v1.17.53 they
did no per-user check at all, and the before_request hook bound every
GET as the server (admin) because `X-Actor` was only parsed for POST.

So a scoped caller could ask for another user's private tier and get
the fact text back:

    X-Actor: user:user_a
    GET /v1/fact/18400/provenance?tier=private&user_id=admin
    -> {"ancestors": [{"fact": {"content": "R-class ..."}}]}

`X-Actor` is the established mechanism -- client.py already sends it.
It just was not honoured on GET.

Real server, real store, real ACL -- nothing is mocked.
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
        # A refusal is the interesting case here, not an exception.
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def _get(url, timeout=30, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


class TestProvenanceScope(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="astor_prov_acl_")
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

    def _admin_fact(self):
        marker = f"astor provenance scope probe {time.time()}"
        st, w = _post(self.base + "/v1/write", {
            "text": marker, "user": "admin", "mode": "auto",
            "tier": "private", "scope": "long_term"})
        self.assertEqual(st, 200, w)
        fids = w.get("fact_ids") or []
        self.assertTrue(fids, w)
        return marker, int(fids[0])

    def test_another_user_cannot_read_the_provenance_of_a_private_fact(self):
        marker, fid = self._admin_fact()
        for route in ("provenance", "lineage", "graph.dot", "versions"):
            with self.subTest(route=route):
                st, body = _get(
                    f"{self.base}/v1/fact/{fid}/{route}"
                    f"?tier=private&user_id=admin",
                    headers={"X-Actor": "user:user_a"})
                self.assertEqual(st, 403,
                                 f"{route} let a scoped caller read another "
                                 f"user's private scope: {str(body)[:200]}")
                self.assertNotIn(marker, body)

    def test_the_owner_still_gets_its_own_provenance(self):
        marker, fid = self._admin_fact()
        st, body = _get(
            f"{self.base}/v1/fact/{fid}/provenance?tier=private&user_id=admin",
            headers={"X-Actor": "admin:admin"})
        self.assertEqual(st, 200, str(body)[:200])
        self.assertIn(str(fid), body)

    def test_a_non_admin_cannot_plant_a_provenance_edge(self):
        marker, fid = self._admin_fact()
        st, body = _post(
            self.base + f"/v1/fact/{fid}/provenance",
            {"parents": [fid], "kind": "extracted", "tier": "private",
             "user_id": "admin"},
            headers={"X-Actor": "user:user_a"})
        self.assertEqual(st, 403,
                         "planting a provenance edge across the tenant "
                         f"boundary must be refused: {str(body)[:200]}")


if __name__ == "__main__":
    unittest.main()
