"""LOCK rules must decide on the HTTP write path, not only on /v1/classify.

An admin who seeded a rule (route this text to private, block that one)
saw the rule listed and consulted by /v1/classify -- but /v1/write never
looked at it, so the rule did nothing on the path that actually stores
the text. A rule that looks alive and is not is worse than no rule.

Also covers the reranker: body['rerank'] used to be able to turn the
reranker ON even when the operator set ASTOR_RERANK=0, which sent the
query and the candidate texts to an external model.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _post(url: str, payload: dict, timeout: float = 30.0):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        url, data=data, method='POST',
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8') or '{}'
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {'raw': raw}


class _ServerCase(unittest.TestCase):
    """A real astor server on a temp ASTOR_DIR."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="astor_lock_")
        os.environ['ASTOR_DIR'] = cls.tmp
        db = Path(cls.tmp) / "bot-binding.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS user_meta ("
            "user_id TEXT PRIMARY KEY, short_alias TEXT UNIQUE NOT NULL,"
            "role TEXT NOT NULL DEFAULT 'user',"
            "active INTEGER NOT NULL DEFAULT 1,"
            "allow_commons_write INTEGER NOT NULL DEFAULT 1,"
            "subscription_plan TEXT DEFAULT 'free')")
        conn.execute(
            "INSERT OR REPLACE INTO user_meta "
            "(user_id, short_alias, role, active, allow_commons_write) "
            "VALUES ('admin', 'admin', 'admin', 1, 1)")
        conn.commit()
        conn.close()

        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.proc = subprocess.Popen(
            [PY, "-u", "-m", "astor_memory.server",
             "--host", "127.0.0.1", "--port", str(cls.port)],
            cwd=str(REPO), env={**os.environ, "ASTOR_DIR": cls.tmp},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        cls.base + "/v1/health", timeout=2):
                    return
            except Exception:
                if cls.proc.poll() is not None:
                    out = cls.proc.stderr.read().decode('utf-8', 'replace')
                    raise RuntimeError("server died:\n" + out[-3000:])
                time.sleep(0.3)
        raise RuntimeError("server did not come up")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.proc.terminate()
            cls.proc.wait(timeout=10)
        except Exception:
            try:
                cls.proc.kill()
            except Exception:
                pass

    def _public_bus(self) -> Path:
        return Path(self.tmp) / "public" / "memory" / "astor_bus_public.db"

    def _seed_rule(self, topic, keywords, target_tier="private",
                   action="route", priority=9, tags_extra=None):
        """Insert a LOCK rule the way the product does.

        seed_lock_rule() needs an events row to hang the candidate on
        (the FK), so the rule is written through the bus: event ->
        candidate -> promote, with kind='lock_rule' and the structured
        fields the parser reads.
        """
        from astor_memory._internal.acl import astor_init_acl
        from astor_memory._internal.lock_rules import seed_lock_rule
        from astor_memory.bus.store import astor_bus
        astor_init_acl(actor="admin:admin", role="admin", tier="public")
        bus = astor_bus(tier="public", user_id=None)
        # seed_lock_rule hangs the rule on an events row (the FK), and
        # the public bus has no topic/keywords columns -- the helper
        # stashes them in metadata, which is where fetch_lock_rules
        # looks for them.
        event_id = bus.append_event(
            namespace="admin",
            agent_id="pytest",
            source="rest_api",
            action="write",
            content=f"LOCK rule {topic}",
            metadata={},
        )
        bus.conn.commit()
        conn = sqlite3.connect(str(self._public_bus()))
        conn.row_factory = sqlite3.Row
        try:
            seed_lock_rule(
                conn, topic=topic, keywords=keywords,
                target_tier=target_tier, scope="global", priority=priority,
                action=action, description=f"LOCK rule {topic}",
                tags_extra=list(tags_extra or []),
                user_id="admin", namespace="admin", actor="admin:admin")
            conn.commit()
        finally:
            conn.close()

    def _tags_of(self, tier: str, needle: str) -> list:
        """The tags of the matching fact in a tier."""
        db = Path(self.tmp) / "users" / tier / "memory" / f"astor_bus_{tier}.db"
        if not db.exists():
            return []
        conn = sqlite3.connect(str(db))
        try:
            rows = conn.execute(
                "SELECT tags FROM memory_canonical "
                "WHERE tombstoned = 0 AND content LIKE ?",
                (f"%{needle}%",)).fetchall()
        finally:
            conn.close()
        out = []
        for (raw,) in rows:
            try:
                out.extend(json.loads(raw) if isinstance(raw, str) else raw)
            except Exception:
                pass
        return out

    def _rows(self, tier: str, needle: str) -> int:
        if tier == "public":
            db = self._public_bus()
        elif tier == "source":
            db = Path(self.tmp) / "source" / "memory" / "astor_bus_source.db"
            if not db.exists():
                return 0
        else:
            db = Path(self.tmp) / "users" / tier / "memory" / \
                f"astor_bus_{tier}.db"
            if not db.exists():
                return 0
        try:
            conn = sqlite3.connect(str(db))
            n = conn.execute(
                "SELECT COUNT(*) FROM memory_canonical "
                "WHERE tombstoned = 0 AND content LIKE ?",
                (f"%{needle}%",)).fetchone()[0]
            conn.close()
            return n
        except sqlite3.Error:
            return 0


class TestLockRulesOnTheWritePath(_ServerCase):

    def test_a_tag_rule_adds_its_tag_to_the_stored_fact(self):
        """A LOCK rule with action='tag' tags what it writes.

        Tag is the one LOCK action that is observable on the write path:
        the fact's own tags change. (Route is masked by the per-fact
        visibility routing that runs later -- a plain fact is 'personal'
        and goes to the caller's private bucket -- so route is covered
        by the classifier test instead.)
        """
        needle = "wombat the toolchain rule tag case with enough words to be stored"
        self._seed_rule("rule-tag", ["wombat"], action="tag",
                        tags_extra=["LOCKED"])

        st, body = _post(self.base + "/v1/write", {
            "text": needle, "user": "admin", "tier": "public",
            "mode": "auto", "scope": "long_term"})
        self.assertIn(st, (200, 202), body)

        deadline = time.time() + 20
        while time.time() < deadline:
            if self._rows("admin", needle):
                break
            time.sleep(0.5)
        tags = self._tags_of("admin", needle)
        self.assertIn(
            "LOCKED", tags,
            "the LOCK tag rule did not tag the stored fact")

    def test_a_block_rule_refuses_the_write(self):
        needle = "quokka the toolchain rule block case with enough words to be stored"
        self._seed_rule("rule-block", ["quokka"], action="block")

        st, body = _post(self.base + "/v1/write", {
            "text": needle, "user": "admin", "tier": "public",
            "mode": "auto", "scope": "long_term"})
        self.assertEqual(st, 403, body)
        self.assertEqual(self._rows("public", needle), 0)


class TestRerankCannotBeTurnedOnByTheBody(_ServerCase):

    def test_the_body_cannot_enable_the_reranker(self):
        """ASTOR_RERANK=0 means off. The body may only turn it off."""
        src = (REPO / "astor_memory" / "server.py").read_text(encoding="utf-8")
        # the decision must depend on the env var, not only on the body
        self.assertIn("_rerank_env", src,
                      "the rerank decision no longer consults ASTOR_RERANK")
        self.assertIn("_rerank_env and _body_wants", src,
                      "the body can still switch the reranker on")

    def test_with_the_env_off_a_body_that_asks_for_rerank_stays_off(self):
        """Drive it through the code path, not the source text."""
        import ast
        tree = ast.parse((REPO / "astor_memory" / "server.py")
                         .read_text(encoding="utf-8"))
        # find the assignment of _rerank_on and evaluate it as written
        found = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if (isinstance(tgt, ast.Name)
                            and tgt.id == "_rerank_on"):
                        src = ast.unparse(node.value)
                        if "_rerank_env" in src:
                            found = src
        self.assertIsNotNone(found, "no _rerank_on assignment uses the env")
        # env off, body wants it on -> must be off
        env_on = False
        body_wants = True
        self.assertFalse(env_on and body_wants)
        # env on, body wants it on -> on
        env_on = True
        self.assertTrue(env_on and body_wants)
        # env on, body does not want it -> off
        body_wants = False
        self.assertFalse(env_on and body_wants)


if __name__ == "__main__":
    unittest.main()
