"""Regression tests for `am mcp serve` (added 2026-10-16).

Three defects shipped in v1.16.76 that made the MCP server
non-functional on Windows and unusable anywhere else:

1. `_pipe_ready()` polled stdin via `PeekNamedPipe`, which returns
   ok=0 for a pipe Python created. The fallback compared `tell()`
   against `fstat().st_size`, and a pipe has no size, so it was
   always 0. `_pipe_ready` therefore returned False forever and the
   server sat in its deadline loop, then exited silently.

2. `_send()` wrapped every response in LSP-style `Content-Length`
   headers. The MCP stdio spec is newline-delimited JSON; a spec-
   following host parses one line and chokes on the header.

3. All three tool handlers imported `astor_read` / `astor_write`
   from `astor_memory`, which never existed (the package exports
   bus/nest/forge only). Every tools/call returned "cannot import
   name ...". `astor_status` additionally constructed a whole Flask
   app per call, which printed to stdout and corrupted the stream.

These tests drive the server as a real subprocess over stdin/stdout,
because that is the only way to catch framing and stdout-purity
bugs -- unit-testing the helpers would miss all three.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _child_env(tmpdir):
    env = {
        "PATH": r"C:\Windows\system32;C:\Windows",
        "PYTHONPATH": str(REPO),
        "ASTOR_DIR": str(tmpdir),
        "ASTOR_HOME": str(Path(tmpdir) / "home"),
        "SYSTEMROOT": r"C:\Windows",
        "TEMP": os.environ.get("TEMP", r"C:\Windows\Temp"),
        "PYTHONIOENCODING": "utf-8",
    }
    return env


class TestMcpStdioServer(unittest.TestCase):
    """Drive `am mcp serve` the way a real MCP host would."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls._tmp = tempfile.mkdtemp(prefix="astor_mcp_test_")
        cls.proc = subprocess.Popen(
            [sys.executable, "-W", "ignore", "-m",
             "astor_memory.cli.main", "mcp", "serve"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
            cwd=str(REPO), env=_child_env(cls._tmp),
        )
        # give the interpreter time to import the package
        time.sleep(3)
        if cls.proc.poll() is not None:
            err = cls.proc.stderr.read()
            raise AssertionError(
                f"MCP server exited at startup rc={cls.proc.returncode}\n"
                f"stderr: {err[:600]}")

    @classmethod
    def tearDownClass(cls):
        import shutil
        if cls.proc.poll() is None:
            cls.proc.kill()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def _rpc(self, method, params=None, rid=1, timeout=20):
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if line:
                return json.loads(line)
            if self.proc.poll() is not None:
                self.fail(f"server exited while handling {method}")
        self.fail(f"no response to {method} within {timeout}s")

    def test_initialize_responds(self):
        """Regression: server sat in _pipe_ready forever, never answered."""
        r = self._rpc("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        })
        res = r.get("result", {})
        self.assertIn("serverInfo", res,
                      f"initialize returned no serverInfo: {r}")
        self.assertIn("tools", res.get("capabilities", {}))

    def test_response_is_single_line_json(self):
        """Regression: responses used LSP Content-Length framing."""
        r = self._rpc("tools/list", {}, rid=2)
        self.assertIn("result", r, f"tools/list returned an error: {r}")
        tools = r["result"].get("tools", [])
        self.assertTrue(tools, "tools/list returned no tools")
        names = {t.get("name") for t in tools}
        self.assertIn("astor_recall", names)
        self.assertIn("astor_write", names)
        self.assertIn("astor_status", names)

    def test_write_tool_does_not_raise_import_error(self):
        """Regression: handlers imported astor_read/astor_write, which
        do not exist in astor_memory."""
        r = self._rpc("tools/call", {
            "name": "astor_write",
            "arguments": {"text": "MCP regression test fact.",
                          "user": "admin", "tier": "public"},
        }, rid=3)
        res = r.get("result", {})
        text = (res.get("content") or [{}])[0].get("text", "")
        self.assertNotIn("cannot import name", text,
                         f"handler hit the missing-API bug: {text}")
        self.assertFalse(res.get("isError"),
                         f"astor_write failed: {text}")

    def test_recall_tool_does_not_raise_import_error(self):
        """Regression: astor_recall imported the same missing astor_read."""
        r = self._rpc("tools/call", {
            "name": "astor_recall",
            "arguments": {"query": "regression test fact",
                          "user": "admin", "top_k": 3},
        }, rid=4)
        res = r.get("result", {})
        text = (res.get("content") or [{}])[0].get("text", "")
        self.assertNotIn("cannot import name", text,
                         f"handler hit the missing-API bug: {text}")

    def test_status_tool_returns_json(self):
        """Regression: astor_status built a Flask app per call, printing
        to stdout and corrupting the stream."""
        r = self._rpc("tools/call", {"name": "astor_status",
                                     "arguments": {}}, rid=5)
        res = r.get("result", {})
        text = (res.get("content") or [{}])[0].get("text", "")
        self.assertNotIn("cannot import name", text)
        # a fresh ASTOR_DIR has no bot-binding.db, so doctor may report a
        # missing table -- that is a clean error, not a protocol break
        self.assertTrue(text.strip(), "astor_status returned empty content")


if __name__ == "__main__":
    unittest.main()