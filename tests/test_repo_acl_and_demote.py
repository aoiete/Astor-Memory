"""Three rules that existed but were never called.

- the ACL matrix says ("write", "repo"): {"admin"} -- the repo tier is
  admin-write-only -- and neither /v1/forget nor chat_ingest called the
  check, so a plain user could delete repository memory and write into
  it through chat;
- `am bot demote` only rewrote install-state.json, while the runtime
  reads the role from user_meta.role in bot-binding.db -- so a demoted
  admin kept admin authority.
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


def _user_meta_db(astor_dir):
    """bot-binding.db with one ordinary user and one admin."""
    db = Path(astor_dir) / "bot-binding.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS user_meta ("
        "user_id TEXT PRIMARY KEY, short_alias TEXT UNIQUE NOT NULL,"
        "role TEXT NOT NULL DEFAULT 'user',"
        "active INTEGER NOT NULL DEFAULT 1,"
        "allow_commons_write INTEGER NOT NULL DEFAULT 1)")
    conn.execute(
        "INSERT OR REPLACE INTO user_meta "
        "(user_id, short_alias, role, active) VALUES ('user_a','user_a','user',1)")
    conn.execute(
        "INSERT OR REPLACE INTO user_meta "
        "(user_id, short_alias, role, active) VALUES ('admin','admin','admin',1)")
    conn.commit()
    conn.close()
    return db


class TestRepoTierIsAdminOnly(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="astor_repo_acl_")
        cls._orig = os.environ.get("ASTOR_DIR")
        os.environ["ASTOR_DIR"] = cls.tmp
        _user_meta_db(cls.tmp)
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
        if cls._orig is None:
            os.environ.pop("ASTOR_DIR", None)
        else:
            os.environ["ASTOR_DIR"] = cls._orig

    def test_forget_refuses_a_non_admin_on_the_repo_tier(self):
        st, body = _post(self.base + "/v1/forget",
                          {"fact_id": 999999, "tier": "repo", "repo_id": "r1",
                           "user": "user_a"})
        self.assertEqual(st, 403, f"a non-admin deleted repo memory: {body}")
        self.assertEqual(body.get("error"), "cross_user_forbidden")

    def test_forget_is_not_blocked_for_an_admin(self):
        st, body = _post(self.base + "/v1/forget",
                          {"fact_id": 999999, "tier": "repo", "repo_id": "r1",
                           "user": "admin"})
        self.assertNotEqual(st, 403, f"the admin was refused: {body}")

    def test_chat_ingest_refuses_a_non_admin_on_the_repo_tier(self):
        st, body = _post(self.base + "/v1/chat/ingest", {
            "namespace": "repo:r1", "agent_id": "test", "window_id": "w1",
            "turns": [{"role": "user", "content": "hello there"}],
            "user": "user_a"})
        self.assertEqual(st, 403,
                         f"a non-admin wrote into shared repo memory: {body}")

    def test_chat_ingest_is_not_blocked_for_an_admin(self):
        st, body = _post(self.base + "/v1/chat/ingest", {
            "namespace": "repo:r1", "agent_id": "test", "window_id": "w2",
            "turns": [{"role": "user", "content": "hello there"}],
            "user": "admin"})
        self.assertEqual(st, 200, f"the admin was refused: {body}")


class TestBotDemoteTakesEffect(unittest.TestCase):

    def test_demote_also_demotes_the_runtime_role(self):
        tmp = tempfile.mkdtemp(prefix="astor_demote_")
        old = os.environ.get("ASTOR_DIR")
        os.environ["ASTOR_DIR"] = tmp
        try:
            from astor_memory._internal import bot_binding
            bot_binding.upsert_user(
                user_id='demote_target', short_alias='demote_target',
                role='admin', subscription_plan='admin')
            db = bot_binding._db_path()

            from astor_memory.cli import main as cli
            import importlib
            cli = importlib.import_module("astor_memory.cli.main")

            class _Args:
                user_id = "demote_target"

            rc = cli.cmd_bot_demote(_Args())
            self.assertEqual(rc, 0)

            conn = sqlite3.connect(str(db))
            role = conn.execute(
                "SELECT role FROM user_meta WHERE user_id = 'demote_target'"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(role, "user",
                             "the runtime role is what the server reads -- a "
                             "demoted admin that stays admin is not demoted")
        finally:
            if old is None:
                os.environ.pop("ASTOR_DIR", None)
            else:
                os.environ["ASTOR_DIR"] = old


if __name__ == "__main__":
    unittest.main()
