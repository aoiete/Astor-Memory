"""A hard forget must not leave the content behind in the audit log.

The audit row took an old_state snapshot of the whole canonical row and
a content_preview in its metadata. The fact was deleted from
memory_canonical, but its text was still readable in audit_log -- so
"forget" was reversible by anyone with audit access.

A tombstone is different on purpose: the content is still there by
design, so the snapshot and the preview stay.
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


class TestHardForgetLeavesNoContent(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="astor_forget_audit_")
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

    def _write(self, text: str) -> int:
        st, body = _post(self.base + "/v1/write", {
            "text": text, "user": "admin", "tier": "public",
            "mode": "auto", "scope": "long_term"})
        self.assertIn(st, (200, 202), body)
        ids = body.get("fact_ids") or []
        self.assertTrue(ids, body)
        return int(ids[0])

    def _audit_rows(self, fact_id: int | None = None) -> list:
        """Forget audit rows, wherever the fact lived.

        A plain fact is 'personal', so it lands in the caller's private
        bucket and the audit row is written there. Pass fact_id to look
        at one fact only -- the ids restart at 1 for every test, so an
        unfiltered query picks up rows from an earlier test.
        """
        rows = []
        sql = ("SELECT event, actor, old_state, metadata FROM audit_log "
               "WHERE event = 'forget'")
        args = ()
        if fact_id is not None:
            sql += " AND target_id = ?"
            args = (str(fact_id),)
        for db in (Path(self.tmp) / "users" / "admin" / "memory" /
                  "astor_bus_admin.db",
                  Path(self.tmp) / "public" / "memory" /
                  "astor_bus_public.db"):
            if not db.exists():
                continue
            conn = sqlite3.connect(str(db))
            try:
                rows.extend(conn.execute(sql, args).fetchall())
            except sqlite3.Error:
                pass
            finally:
                conn.close()
        return rows

    def test_a_hard_forget_leaves_no_content_in_the_audit_log(self):
        needle = ("the operator asked for this text to be removed for good "
                  "and it must not survive anywhere")
        fid = self._write(needle)

        st, body = _post(self.base + "/v1/forget", {
            "fact_id": fid, "user": "admin", "tier": "private",
            "user_id": "admin", "tombstone_only": False})
        self.assertIn(st, (200, 202), body)

        # the response must not echo the text back either
        self.assertNotIn(needle, json.dumps(body, ensure_ascii=False))

        rows = self._audit_rows(fid)
        self.assertTrue(rows, "the forget was not audited at all")
        for event, actor, old_state, metadata in rows:
            self.assertIsNone(
                old_state,
                "the hard forget kept the old row (content included) in "
                "the audit log")
            self.assertNotIn(
                needle, (metadata or ""),
                "the hard forget kept the content in the audit metadata")

    def test_a_hard_forget_clears_an_earlier_tombstone_preview(self):
        """A tombstone row keeps a content_preview; the hard delete has to
        take that away too, or the text is still readable in the audit log
        even though the snapshot is gone."""
        needle = ("this one is tombstoned with a preview and then hard "
                  "deleted so the preview must not survive")
        fid = self._write(needle)

        st, _ = _post(self.base + "/v1/forget", {
            "fact_id": fid, "user": "admin", "tier": "private",
            "user_id": "admin", "tombstone_only": True})
        self.assertIn(st, (200, 202))

        # the tombstone keeps the preview -- that path is recoverable.
        # (the preview is truncated, so look for the marker, not the
        # whole text)
        previews = [m for _e, _a, _o, m in self._audit_rows(fid)
                    if m and "content_preview" in m]
        self.assertTrue(previews, "the tombstone kept no preview at all")

        st, _ = _post(self.base + "/v1/forget", {
            "fact_id": fid, "user": "admin", "tier": "private",
            "user_id": "admin", "tombstone_only": False})
        self.assertIn(st, (200, 202))

        for _event, _actor, _old, metadata in self._audit_rows(fid):
            self.assertNotIn(
                needle, (metadata or ""),
                "the hard delete left the content preview in the audit log")

    def test_a_hard_forget_clears_the_earlier_snapshot_too(self):
        """A tombstone leaves a snapshot; the hard delete that follows has
        to take it away.

        restore_fact() reads its row from audit_log.old_state -- the most
        recent row that has one. Clearing only the hard delete's own row
        is not enough: the earlier tombstone still holds the full row, so
        restore finds that one and puts the deleted content back. That is
        "forget" being reversible, which is what the hard delete exists
        to prevent.
        """
        needle = ("this one is tombstoned first and hard deleted afterwards "
                  "so the old snapshot must not survive either")
        fid = self._write(needle)

        # tombstone: the snapshot is kept on purpose
        st, _ = _post(self.base + "/v1/forget", {
            "fact_id": fid, "user": "admin", "tier": "private",
            "user_id": "admin", "tombstone_only": True})
        self.assertIn(st, (200, 202))

        # a tombstone CAN be restored -- that path is recoverable
        st, body = _post(self.base + f"/v1/fact/{fid}/restore", {
            "tier": "private", "user_id": "admin", "target_state": "preview"})
        self.assertIn(st, (200, 202), body)
        self.assertTrue(body.get("would_restore"), body)

        # hard delete: now nothing may restore it
        st, _ = _post(self.base + "/v1/forget", {
            "fact_id": fid, "user": "admin", "tier": "private",
            "user_id": "admin", "tombstone_only": False})
        self.assertIn(st, (200, 202))

        st, body = _post(self.base + f"/v1/fact/{fid}/restore", {
            "tier": "private", "user_id": "admin", "target_state": "preview"})
        self.assertIn(st, (200, 202), body)
        self.assertFalse(
            body.get("would_restore"),
            "the hard-deleted fact was still restorable -- the earlier "
            "tombstone snapshot was left behind")
        self.assertNotIn(needle, json.dumps(body, ensure_ascii=False))

    def test_a_tombstone_still_keeps_the_snapshot(self):
        """The tombstone is recoverable on purpose -- the snapshot stays."""
        needle = ("this one is only tombstoned so the snapshot has to stay "
                  "for the restore path to work at all")
        fid = self._write(needle)

        st, body = _post(self.base + "/v1/forget", {
            "fact_id": fid, "user": "admin", "tier": "private",
            "user_id": "admin", "tombstone_only": True})
        self.assertIn(st, (200, 202), body)

        rows = self._audit_rows(fid)
        self.assertTrue(rows)
        # the newest row for this tombstone keeps the old_state
        snapshots = [r[2] for r in rows if r[2]]
        self.assertTrue(
            snapshots,
            "a tombstone must keep the old_state snapshot -- the restore "
            "path needs it")
        self.assertIn(needle, snapshots[-1])


if __name__ == "__main__":
    unittest.main()
