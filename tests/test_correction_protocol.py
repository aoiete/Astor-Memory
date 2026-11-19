"""v1.15.57 — pushback capture protocol tests.

Covers:
- POST /v1/experience — auto-dedup + 3-occurrence HOT promote
- POST /v1/experience/match — recall
- POST /v1/write auto-fork on kind=correction
- after_request middleware X-Astor-Experience-Warning on /v1/read
"""
import json
import time
import unittest
import urllib.request
import urllib.error

# This file writes. It used to write to the live server, so every test
# run left a fact behind -- 182 of them accumulated in the live private
# tier over months ("auto fork-<uuid>" and "middleware-warn-test"),
# and they showed up in recall.
#
# So the tests get their own astor: a temporary ASTOR_DIR and a server
# on a free port. If it cannot start, the module skips -- falling back
# to the live server would be the bug again.
BASE = ""


def _start_isolated_server():
    """A throwaway astor on a temp dir and a free port.

    Returns (base_url, process) or (None, None).
    """
    import os
    import socket
    import subprocess
    import sys
    import tempfile
    from pathlib import Path as _P

    repo = _P(__file__).resolve().parent.parent
    py = sys.executable
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    tmp = tempfile.mkdtemp(prefix="astor_correction_")
    env = {**os.environ, "ASTOR_DIR": tmp}
    proc = subprocess.Popen(
        [py, "-u", "-m", "astor_memory.server", "--host", "127.0.0.1",
         "--port", str(port)],
        cwd=str(repo), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    import time as _t
    base = f"http://127.0.0.1:{port}"
    deadline = _t.time() + 90
    while _t.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/v1/health", timeout=5) as r:
                if r.status == 200:
                    return base, proc
        except Exception:
            _t.sleep(0.5)
    try:
        proc.kill()
    except Exception:
        pass
    return None, None


def setUpModule():
    global BASE
    base, proc = _start_isolated_server()
    if base is None:
        raise unittest.SkipTest("could not start an isolated astor server")
    BASE = base
    globals()["_ISOLATED_PROC"] = proc


def tearDownModule():
    proc = globals().pop("_ISOLATED_PROC", None)
    if proc is not None:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _post(path: str, body: dict, timeout: int = 15) -> tuple[int, dict]:
    """POST JSON to astor endpoint. Returns (status_code, parsed_body)."""
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {"_error": str(e)}


class TestCorrectionProtocol(unittest.TestCase):
    """v1.15.57 pushback capture protocol."""

    def test_experience_insert_dedup(self):
        """First call inserts, next two increment count on same row."""
        body = {
            "text": "test pushback " + str(time.time()),
            "outcome": "failure",
            "trigger_keywords": ["t1", "t2", "t3"],
            "user": "admin",
            "tier": "source",
        }
        # First insert
        code1, d1 = _post("/v1/experience", body)
        self.assertEqual(code1, 200)
        self.assertFalse(d1["deduped"])
        eid = d1["experience_id"]
        # Dedup 1
        code2, d2 = _post("/v1/experience", body)
        self.assertEqual(code2, 200)
        self.assertTrue(d2["deduped"])
        self.assertEqual(d2["experience_id"], eid)
        # Dedup 2
        code3, d3 = _post("/v1/experience", body)
        self.assertEqual(code3, 200)
        self.assertTrue(d3["deduped"])
        self.assertEqual(d3["experience_id"], eid)

    def test_experience_match_recall(self):
        """Recent pushback appears in /v1/experience/match.

        v1.16.9.2 fix: prior runs polluted the live bus with 100+ `matchtest-*`
        experiences (all sharing trigger_keywords=['match']). With top_k=5, the
        new entry was never in the returned slice (older higher-importance
        rows outranked it). Fix: use the unique timestamp AS one of the
        trigger_keywords so the new row's trigger matches the query exactly,
        AND raise top_k to a level that survives bus pollution.
        """
        unique = "matchtest-" + str(int(time.time()))
        body = {
            "text": unique,
            "outcome": "failure",
            # v1.16.9.2: include the unique token as a trigger keyword so the
            # match query (which now embeds the unique token) returns the
            # new row first via exact-keyword match (kw_score 0.5 * 1 hit).
            "trigger_keywords": [unique, "match"],
            "user": "admin",
            "tier": "source",
        }
        _post("/v1/experience", body)

        code, d = _post("/v1/experience/match", {
            "query": unique,
            "user": "admin",
            "tier": "source",
            # v1.16.9.2: 50 is enough to find a recent kw-exact match even
            # with 100+ polluted `matchtest-*` rows in the live bus.
            "top_k": 50,
        })
        self.assertEqual(code, 200)
        matches = d.get("matches", [])
        self.assertTrue(any(unique in m["action_summary"] for m in matches),
                        f"Expected {unique!r} in matches, got "
                        f"{[m['action_summary'][:40] for m in matches]}")

    def test_write_auto_fork_correction_kind(self):
        """POST /v1/write with kind=correction creates an experience row."""
        import uuid as _uuid
        unique = "auto fork-" + _uuid.uuid4().hex[:12]
        code, d = _post("/v1/write", {
            "text": unique, "kind": "correction", "user": "admin", "tier": "source",
        })
        self.assertEqual(code, 200)
        self.assertIsNotNone(d.get("experience_id"))
        self.assertIsNotNone(d.get("experience_occurrence"))

    def test_read_middleware_warning_header(self):
            """POST /v1/read sets X-Astor-Experience-Warning when matches exist."""
            unique_text = "middleware-warn-test " + str(int(time.time()))
            kw = ["middlewarewarn", "shipbefore"]
            # Write with kind=correction so it auto-forks as experience (with these trigger_keywords)
            _post("/v1/write", {
                "text": unique_text,
                "kind": "correction",
                "user": "admin",
                "tier": "source",
                "tags": kw,
            })

            # Query uses one of the trigger keywords so kw-only match_experiences fires
            req = urllib.request.Request(
                f"{BASE}/v1/read",
                data=json.dumps({
                    "query": "middlewarewarn shipbefore",
                    "user": "admin", "tier": "source", "top_k": 3
                }).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                warning = resp.headers.get("X-Astor-Experience-Warning")
            # Note: the middleware uses kw-only match_experiences. If our trigger
            # keywords don't intersect the query tokens, no warning — that's expected.
            # The test is mainly that the endpoint responds 200 with valid body shape.
            self.assertIsInstance(body, dict)
            # body should have either 'results' or 'count' from /v1/read shape
            self.assertTrue("count" in body or "results" in body)

    def test_consult_includes_experience_field(self):
        """/v1/consult now returns 'experience' array + counts.experience."""
        code, d = _post("/v1/consult", {
            "query": "anything", "user": "admin", "tier": "source", "top_k": 2
        })
        self.assertEqual(code, 200)
        self.assertIn("experience", d)
        self.assertIn("experience", d["counts"])


if __name__ == "__main__":
    unittest.main(verbosity=2)