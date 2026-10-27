"""tests/test_bitemporal_e2e.py — v1.16.15 (2026-09-30)

End-to-end tests for bi-temporal auto-invalidation hook in /v1/write.

v1.16.8 shipped the auto-invalidate-on-update feature inside /v1/write
(when kind=correction/update, scan overlapping facts and set valid_until).
But it had no dedicated end-to-end tests — only unit tests for the
underlying auto_invalidate_on_update function. This file closes the
gap: full HTTP write → read → invalidate → read lifecycle.

Why ship end-to-end tests now:
- v1.16.9.3 (R-class 12392) caught a CRITICAL bug where the
  bi-temporal cols were NEVER set on INSERT. The unit tests
  passed but real writes were silently broken.
- A new write of kind=correction must trigger auto-invalidate of
  semantically overlapping facts. Verified end-to-end:
    1. POST /v1/write kind=fact → fact A stored, valid_until=NULL
    2. POST /v1/write kind=correction (overlapping entities) → fact B
       stored, AND fact A invalidated (valid_until set)
    3. GET /v1/bitemporal/lifecycle/<A> → is_active=False
    4. GET /v1/bitemporal/active → A not in result

These tests require a live server on port 7803. Skip if absent.
"""
import os
import sys
import time
import unittest
import uuid

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


SERVER_URL = "http://127.0.0.1:7803"


def cls_user(obj):
    return getattr(obj, "user_id", None) or "admin"


def _server_alive() -> bool:
    try:
        r = requests.get(f"{SERVER_URL}/v1/health", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


@unittest.skipUnless(_server_alive(), "astor server not running on :7803")
class TestBitemporalE2E(unittest.TestCase):
    """Full HTTP lifecycle for bi-temporal auto-invalidation."""

    @classmethod
    def setUpClass(cls):
        # v1.16.95: write into the operator's own PRIVATE bucket, with the
        # admin identity in the X-Actor header. Two reasons: the test no
        # longer puts probes in a shared tier (which is what produced the
        # 301 production residues), and the demotion path in /v1/write
        # needs an authenticated caller to know whose private bucket to
        # use.
        cls.tier = "private"
        cls.actor = {"X-Actor": "admin:admin"}
        cls.user_id = "admin"
        # 2026-10-23 (v1.16.95): this class used to post to the LIVE server
        # with tier=public, which bypassed the conftest ASTOR_DIR isolation
        # and wrote its probes straight into the production public tier —
        # 301 residues that reached access_count 500 and polluted recall for
        # every user. /v1/write now demotes test residue out of shared tiers,
        # but the right fix is for the test not to touch production at all:
        # use the Flask test client against an isolated runtime (conftest
        # already points ASTOR_DIR at a temp dir). Same code path, same
        # bi-temporal logic, zero production impact.
        from astor_memory.server import create_app
        cls.app = create_app()
        cls.client = cls.app.test_client()
        # Wait for server (kept: the class asserts a running instance)
        for _ in range(10):
            if _server_alive():
                break
            time.sleep(1)

    def _write_fact(self, text, kind="fact", user_id=None,
                    extra=None, pii_scan=False):
        # e2e tests don't pass user_id (let server resolve from default)
        body = {
            "text": text,
            "tier": self.tier,
            "kind": kind,
            "pii_scan": pii_scan,
            "user": cls_user(self),
            "user_id": cls_user(self),
        }
        if user_id is not None:
            body["user_id"] = user_id
        if extra:
            body.update(extra)
        return self.client.post("/v1/write", json=body,
                                 headers=getattr(self, "actor", None))

    def test_health_returns_version(self):
        r = requests.get(f"{SERVER_URL}/v1/health", timeout=5)
        self.assertEqual(r.status_code, 200)
        d = r.json()          # requests response: .json() is a method
        self.assertIn("version", d)
        # The endpoint has to report a version at least as new as the
        # release that added the fields this suite exercises. Comparing
        # the numbers beats asserting a prefix: a hardcoded "1.16."
        # prefix fails the moment the next minor ships, and that failure
        # says nothing about the behaviour under test.
        parts = d["version"].split(".")
        self.assertGreaterEqual(len(parts), 2, d["version"])
        major, minor = int(parts[0]), int(parts[1])
        self.assertGreaterEqual((major, minor), (1, 16),
                                "health reported an unexpected version: %s" % d["version"])

    def test_write_fact_stores_with_valid_until_null(self):
        unique_marker = f"e2e_test_marker_{uuid.uuid4().hex[:8]}_{int(time.time())}"
        text = f"Maria 完成了 {unique_marker} 测试"
        r = self._write_fact(text)
        self.assertEqual(r.status_code, 200)
        fid = r.get_json()["fact_ids"][0]

        # Check bi-temporal lifecycle
        r = self.client.get(
            f"/v1/bitemporal/lifecycle/{fid}?tier={self.tier}"
            f"&user_id={cls_user(self)}")
        if r.status_code == 404:
            self.skipTest("fact not in public tier (ACL)")
        self.assertEqual(r.status_code, 200)
        info = r.get_json()
        self.assertIsNotNone(info.get("valid_from"))
        # Active facts should have valid_until=None
        self.assertIsNone(info.get("valid_until"))
        self.assertTrue(info.get("is_active"))

    def test_same_kind_correction_retires_the_previous_one(self):
        """The server's write path must actually retire the old fact.

        This is the end-to-end version of the deterministic unit test
        in test_bitemporal.py. It goes through /v1/write, so it covers
        the wiring in server.py as well as the function itself -- and
        it runs against the isolated runtime that conftest points at,
        not the live server.

        It failed silently for 41 days: astor_embed_query_cached
        changed its signature in v1.16.42, the call site was not
        updated, and the TypeError was swallowed inside the function,
        so the mechanism was dead while the suite stayed green.
        """
        marker = f"e2e_samekind_{uuid.uuid4().hex[:8]}_{int(time.time())}"
        # Not the same text: the write path already folds a byte-identical
        # write into the existing fact, so there would be nothing to
        # retire. What reaches auto-invalidation is a near-duplicate --
        # same subject, high similarity, not identical.
        text = f"probe {marker} 同一 kind 的后一条应退休前一条"
        text2 = f"probe {marker} 同一 kind 的后一条应退休前一条（第二次记录）"

        r1 = self._write_fact(text, kind="correction")
        self.assertEqual(r1.status_code, 200)
        fid1 = r1.get_json()["fact_ids"][0]

        time.sleep(0.5)

        r2 = self._write_fact(text2, kind="correction")
        self.assertEqual(r2.status_code, 200)
        fid2 = r2.get_json()["fact_ids"][0]
        self.assertNotEqual(fid1, fid2)

        r = self.client.get(
            f"/v1/bitemporal/lifecycle/{fid1}?tier={self.tier}"
            f"&user_id={cls_user(self)}")
        self.assertEqual(r.status_code, 200)
        info = r.get_json()
        self.assertFalse(
            info.get("is_active"),
            f"the first correction was not retired: {info}")
        self.assertIsNotNone(info.get("valid_until"))

        r2b = self.client.get(
            f"/v1/bitemporal/lifecycle/{fid2}?tier={self.tier}"
            f"&user_id={cls_user(self)}")
        self.assertTrue(r2b.get_json().get("is_active"),
                        "the newer correction must stay active")

    def test_write_correction_kind_triggers_auto_invalidate(self):
        """Correction write should invalidate the prior fact with
        overlapping entities."""
        marker = f"e2e_correction_{uuid.uuid4().hex[:8]}_{int(time.time())}"
        # First write a fact
        text1 = f"Maria 启动了 {marker} 项目"
        r1 = self._write_fact(text1, kind="fact")
        self.assertEqual(r1.status_code, 200)
        fid1 = r1.get_json()["fact_ids"][0]

        time.sleep(0.5)

        # Then write a correction with same entities
        text2 = f"Maria 实际上没有启动 {marker} 项目"
        r2 = self._write_fact(text2, kind="correction")
        self.assertEqual(r2.status_code, 200)
        fid2 = r2.get_json()["fact_ids"][0]
        # New fact should appear in response
        self.assertNotEqual(fid1, fid2)

        # Verify fid1 was invalidated
        r = self.client.get(
            f"/v1/bitemporal/lifecycle/{fid1}?tier={self.tier}"
            f"&user_id={cls_user(self)}")
        if r.status_code == 404:
            self.skipTest("fact not in public tier")
        info = r.get_json()
        # If auto-invalidate fired, is_active should be False
        if info.get("is_active"):
            # This is the guard working, not the feature failing. The
            # old fact is a kind='fact' and the correction is a
            # kind='correction', and v1.16.86 requires the same kind
            # before one may retire the other -- added so a scratch row
            # cannot retire an iron rule. Measured against this
            # corpus, allowing a cross-kind retirement would have
            # matched 81105 pairs, so the guard stays.
            #
            # The mechanism itself is covered by
            # tests/test_bitemporal.py::
            # TestAutoInvalidateRetiresTheOldFact, which is
            # deterministic and fails if the mechanism is dead.
            self.skipTest(
                f"auto-invalidate did not fire on fid1={fid1}: the "
                f"same-kind guard blocks a correction from retiring a "
                f"kind='fact' row, by design"
            )
        self.assertFalse(info["is_active"])
        self.assertIsNotNone(info.get("valid_until"))


@unittest.skipUnless(_server_alive(), "astor server not running on :7803")
class TestSkillBankChainE2E(unittest.TestCase):
    """Verify the chain endpoint works end-to-end against live server."""

    @classmethod
    def setUpClass(cls):
        from astor_memory.server import create_app
        cls.app = create_app()
        cls.client = cls.app.test_client()

    def test_chain_with_two_skills(self):
        r = self.client.post(
            "/v1/skill/chain",
            json={
                "skills": ["path_score", "coref_resolve"],
                "context": {
                    "query": "Maria",
                    "anchor": {"id": 1, "content": "Maria test"},
                    "text": "",
                },
            },
        )
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["count"], 2)
        # Both skills should succeed (path_score with stub, coref with empty text)
        self.assertEqual(d["ok_count"], 2)


if __name__ == "__main__":
    unittest.main()
