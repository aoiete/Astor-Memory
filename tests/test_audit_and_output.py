"""Three things the audit found that are cheap to get wrong.

- a second administrator's reflection run was audited as 'admin:admin',
  whoever called: the role check passed, the attribution did not;
- relay.json holds a bearer token in the clear and was written with
  the default file permissions;
- `am bundle inspect` printed a bundle's own path, version and file
  names straight to the terminal, and a bundle is somebody else's
  file -- it can carry ANSI escapes and newlines.
"""
from __future__ import annotations

import io
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
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable
ESC = "\x1b"


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


class TestRelayTokenPermissions(unittest.TestCase):

    def test_the_relay_token_file_is_locked_down(self):
        from astor_memory._internal import relay_client
        tmp = tempfile.mkdtemp(prefix="astor_relay_perm_")
        seen = []
        orig = os.chmod

        def _spy(path, mode, *a, **kw):
            seen.append((str(path), mode))
            return orig(path, mode, *a, **kw)

        os.chmod = _spy
        try:
            relay_client.write_relay_entry(
                base_url="http://relay.example", key_id="k1", token="secret-token",
                astor_dir=tmp)
            relay_client.write_config("http://relay2.example", astor_dir=tmp)
        finally:
            os.chmod = orig

        self.assertTrue(seen, "the relay config holds a bearer token; it "
                               "must be written with restricted permissions")
        for path, mode in seen:
            self.assertEqual(mode, 0o600, f"{path} got mode {oct(mode)}")
        cfg_file = Path(tmp) / "relay.json"
        self.assertTrue(cfg_file.exists())
        self.assertIn("secret-token", cfg_file.read_text(encoding="utf-8"))


class TestBundleOutputIsSafe(unittest.TestCase):

    def test_control_sequences_in_a_bundle_never_reach_the_terminal(self):
        import importlib
        from astor_memory import bundle as bundle_mod
        # the package exports a `main` function, so take the module
        cli = importlib.import_module("astor_memory.cli.main")

        evil = f"{ESC}[2J{ESC}[31mred{ESC}[0m\nforged line"
        man = {
            "bundle_path": evil,
            "bundle_bytes": 1024,
            "created_at": evil,
            "astor_version": evil,
            "tiers": [evil],
            "files": [{"arcname": evil, "bytes": 10, "sha256": evil * 4,
                       "rows": {evil: 1}}],
            "total_bytes": 10,
        }
        orig = bundle_mod.inspect_bundle
        bundle_mod.inspect_bundle = lambda path: man
        self.addCleanup(setattr, bundle_mod, "inspect_bundle", orig)

        class _Args:
            path = "bundle.json"

        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_bundle_inspect(_Args())
        out = buf.getvalue()
        self.assertNotIn(ESC, out, "the bundle's control sequences reached "
                                   "the terminal")
        self.assertNotIn("\nforged line", out)

    def test_safe_term_strips_what_it_should(self):
        from astor_memory.cli.main import _safe_term
        self.assertEqual(_safe_term("a\x1b[31mb\x0ac"), "a?[31mb?c")
        self.assertEqual(_safe_term("plain"), "plain")


class TestReflectionAuditActor(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="astor_reflect_actor_")
        cls._orig = os.environ.get("ASTOR_DIR")
        os.environ["ASTOR_DIR"] = cls.tmp
        # two administrators, so the audit can tell them apart
        db = Path(cls.tmp) / "bot-binding.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS user_meta ("
            "user_id TEXT PRIMARY KEY, short_alias TEXT UNIQUE NOT NULL,"
            "role TEXT NOT NULL DEFAULT 'user',"
            "active INTEGER NOT NULL DEFAULT 1,"
            "allow_commons_write INTEGER NOT NULL DEFAULT 1)")
        for uid in ("admin", "second"):
            conn.execute(
                "INSERT OR REPLACE INTO user_meta "
                "(user_id, short_alias, role, active) VALUES (?, ?, 'admin', 1)",
                (uid, uid))
        conn.commit()
        conn.close()

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

    def test_a_second_admin_is_audited_as_the_actor(self):
        # something for reflection to look at
        for i in range(3):
            _post(self.base + "/v1/write", {
                "text": f"the deployment notes for release {i} are the same text",
                "user": "admin", "mode": "auto", "tier": "public",
                "scope": "long_term"})

        st, body = _post(self.base + "/v1/reflection/run",
                          {"tier": "public", "min_size": 2, "max_clusters": 5},
                          headers={"X-Actor": "admin:second"})
        self.assertEqual(st, 200, body)

        from astor_memory.bus.store import astor_bus
        bus = astor_bus(tier="public", user_id=None)
        rows = bus.conn.execute(
            "SELECT actor FROM audit_log WHERE event LIKE 'reflection%'").fetchall()
        self.assertTrue(rows, "the reflection run should leave audit rows")
        actors = {r[0] for r in rows}
        self.assertNotIn("admin:admin", actors,
                         "the run was attributed to the owner account, "
                         "not to the administrator who asked for it")
        self.assertIn("admin:second", actors, actors)


if __name__ == "__main__":
    unittest.main()
