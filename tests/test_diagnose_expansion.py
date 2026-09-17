"""Tests for the diagnose expansion (Ship C, ADR-0005).

The /v1/health/diagnose endpoint now reports:
1. proxy_hijack_check — env var HTTPS_PROXY/HTTP_PROXY detection
2. db_corruption_check — PRAGMA integrity_check + foreign_key_check
3. embedding_version_check — model loads + dim probe

We test the underlying SQL/logic pieces against a temp DB to avoid
spinning up the live server.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest


def _setup_fake_bus_db() -> tuple[str, callable]:
    """Create a temp bus DB with a schema that allows PRAGMA checks."""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY,
        severity TEXT,
        ts TEXT
    );
    """)
    conn.execute("INSERT INTO audit_log (severity, ts) VALUES ('warning', '2026-09-16')")
    conn.execute("INSERT INTO audit_log (severity, ts) VALUES ('info', '2026-09-16')")
    conn.commit()

    def close():
        conn.close()
    return path, close


class TestProxyHijackCheck(unittest.TestCase):
    """Test proxy_hijack_check logic without spinning up the server."""

    def _proxy_check(self):
        # Mirror server.py logic (with case-insensitive dedupe for Windows)
        proxy_vars = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
        seen_keys = set()
        proxy_findings = []
        for var in proxy_vars:
            val = os.environ.get(var)
            if not val:
                continue
            lowered_var = var.lower()
            if lowered_var in seen_keys:
                continue
            seen_keys.add(lowered_var)
            lowered = val.lower()
            is_loopback = any(
                marker in lowered
                for marker in ("127.0.0.1", "localhost", "::1", "[::1]")
            )
            proxy_findings.append({
                "var": var,
                "value": val,
                "loopback": is_loopback,
                "warn": not is_loopback,
            })
        return {
            "env_vars_set": len(proxy_findings),
            "loopback_only": all(p["loopback"] for p in proxy_findings) if proxy_findings else True,
            "findings": proxy_findings,
            "warn": any(p["warn"] for p in proxy_findings),
        }

    def test_no_proxy_env_set_is_clean(self):
        # Wipe all proxy env vars
        saved = {}
        for v in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            if v in os.environ:
                saved[v] = os.environ.pop(v)
        try:
            result = self._proxy_check()
            self.assertEqual(result["env_vars_set"], 0)
            self.assertTrue(result["loopback_only"])
            self.assertFalse(result["warn"])
        finally:
            for k, v in saved.items():
                os.environ[k] = v

    def test_loopback_proxy_is_clean(self):
        # Wipe all proxy env vars
        saved = {}
        for v in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            if v in os.environ:
                saved[v] = os.environ.pop(v)
        try:
            os.environ["HTTPS_PROXY"] = "http://127.0.0.1:8080"
            result = self._proxy_check()
            self.assertEqual(result["env_vars_set"], 1)
            self.assertTrue(result["loopback_only"])
            self.assertFalse(result["warn"])
        finally:
            for k, v in saved.items():
                os.environ[k] = v
            if "HTTPS_PROXY" not in saved:
                os.environ.pop("HTTPS_PROXY", None)

    def test_non_loopback_proxy_is_warn(self):
        saved = {}
        for v in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            if v in os.environ:
                saved[v] = os.environ.pop(v)
        try:
            os.environ["HTTPS_PROXY"] = "http://198.51.100.42:8080"
            result = self._proxy_check()
            self.assertEqual(result["env_vars_set"], 1)
            self.assertFalse(result["loopback_only"])
            self.assertTrue(result["warn"])
        finally:
            for k, v in saved.items():
                os.environ[k] = v
            if "HTTPS_PROXY" not in saved:
                os.environ.pop("HTTPS_PROXY", None)


class TestDbCorruptionCheck(unittest.TestCase):
    """Test db_corruption_check SQL on a real (temp) SQLite."""

    def setUp(self):
        self.db_path, self.close = _setup_fake_bus_db()

    def tearDown(self):
        self.close()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def test_pristine_db_passes_integrity_check(self):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        integrity = cur.execute("PRAGMA integrity_check").fetchone()
        fk = cur.execute("PRAGMA foreign_key_check").fetchall()
        conn.close()
        self.assertIsNotNone(integrity)
        self.assertEqual(integrity[0], "ok")
        self.assertEqual(len(fk), 0)

    def test_fk_violations_are_detected(self):
        # Add a table with FK constraint and a violating row
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
        CREATE TABLE parent (id INTEGER PRIMARY KEY);
        CREATE TABLE child (
            id INTEGER PRIMARY KEY,
            parent_id INTEGER NOT NULL REFERENCES parent(id)
        );
        INSERT INTO child (id, parent_id) VALUES (1, 999);
        """)
        conn.commit()
        # FK check may need foreign_keys=ON
        conn.execute("PRAGMA foreign_keys = ON")
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        conn.close()
        # child table row 1 has no matching parent → violation
        self.assertGreater(len(fk), 0)


if __name__ == '__main__':
    unittest.main()
