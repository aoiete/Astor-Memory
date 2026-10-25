"""Tests for `am install --ide <agent>` (added 2026-10-16).

The installer writes files that agents read at startup, so a wrong
command name in them is silent breakage: the install reports success,
the agent loads the rules, and then calls a tool that does not exist.

Three defects these cover, all found by running the installer and
reading what it produced:

1. Every generated rules file told agents to call `am_read` (an MCP
   tool that never existed) or run `am read` (a CLI subcommand that
   does not exist). The real names are `astor_recall` and
   `am recall`. Installed across claude-code, cline, opencode,
   cursor, continue, windsurf, aider and aider's conf.

2. opencode.json came out with two headings -- the handler prefixed
   "# Memory source priority" onto PRIORITY_MARKER, which already
   began with "## Memory source priority".

3. `am install --ide hermes` printed "patch
   agent/system_prompt.py lines 483-499 (5-line change)" and wrote
   nothing. docs/agent-adapters.md had recorded on 2026-08-15 that
   this recipe does not match hermes-agent 0.20.0 and removed it. The
   real mechanism is `memory.provider: astor_memory` in config.yaml.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from astor_memory.installer import astor_install
from astor_memory.installer.registry import astor_verify_agent

# Agents that are expected to produce at least one file
FILE_AGENTS = ["claude-code", "cline", "opencode", "cursor",
               "continue", "windsurf", "aider"]

ALL_AGENTS = FILE_AGENTS + ["hermes", "openclaw"]


class TestInstallerOutputIsReal(unittest.TestCase):
    """Anything the installer writes must reference real commands."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="astor_install_test_"))
        cls.written: dict[str, list[Path]] = {}
        for agent in ALL_AGENTS:
            # Use each agent's own recommended mode. Tier C agents
            # (cursor, continue, windsurf, aider) have no priority hook
            # and only accept `coexist`; asking them for `priority`
            # returns no changes at all, which is correct behaviour.
            mode = astor_verify_agent(agent).get("recommended_mode")
            res = astor_install(agent, cls.tmp, mode)
            if "error" in res:
                continue
            paths = []
            for ch in res.get("changes", []):
                p = Path(ch["path"])
                if ch.get("action") == "patch" and p.exists():
                    p.write_text(ch["content"], encoding="utf-8")
                p.parent.mkdir(parents=True, exist_ok=True)
                if not p.exists():
                    p.write_text(ch.get("content", ""), encoding="utf-8")
                paths.append(p)
            cls.written[agent] = paths

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_advertised_agent_is_supported(self):
        for agent in ALL_AGENTS:
            with self.subTest(agent=agent):
                report = astor_verify_agent(agent)
                self.assertTrue(report["supported"],
                                f"{agent} claims no support: {report}")

    def test_file_agents_actually_write_something(self):
        for agent in FILE_AGENTS:
            with self.subTest(agent=agent):
                self.assertTrue(
                    self.written.get(agent),
                    f"{agent} wrote no files -- install reported nothing")

    def test_no_output_references_a_nonexistent_tool(self):
        """Regression: generated rules called `am_read` / `am read`."""
        for agent, paths in self.written.items():
            for p in paths:
                body = p.read_text(encoding="utf-8", errors="replace")
                with self.subTest(agent=agent, file=p.name):
                    self.assertNotIn(
                        "am_read", body,
                        f"{p.name} references a nonexistent MCP tool")
                    self.assertNotIn(
                        "`am read", body,
                        f"{p.name} references a nonexistent CLI subcommand")

    def test_output_references_the_real_tool(self):
        found = 0
        for agent, paths in self.written.items():
            for p in paths:
                body = p.read_text(encoding="utf-8", errors="replace")
                if "Memory source priority" in body:
                    found += 1
                    self.assertIn(
                        "astor_recall", body,
                        f"{agent}/{p.name} lacks the real tool name")
        self.assertGreaterEqual(
            found, len(FILE_AGENTS) - 1,
            "expected the priority marker in most agents' output")

    def test_opencode_json_has_single_heading(self):
        """Regression: opencode.json got two headings."""
        p = self.tmp / "opencode.json"
        if not p.exists():
            self.skipTest("opencode handler wrote nothing")
        data = json.loads(p.read_text(encoding="utf-8"))
        blob = json.dumps(data)
        self.assertEqual(
            blob.count("## Memory source priority"), 1,
            "opencode.json has a duplicated heading")

    def test_generated_json_parses(self):
        for agent in ("opencode", "cline"):
            for p in self.written.get(agent, []):
                if p.suffix != ".json":
                    continue
                with self.subTest(file=str(p)):
                    json.loads(p.read_text(encoding="utf-8"))

    def test_hermes_does_not_recommend_the_removed_patch(self):
        """Regression: installer told users to patch system_prompt.py,
        a recipe docs/agent-adapters.md had already removed."""
        res = astor_install("hermes", self.tmp,
                           astor_verify_agent("hermes").get("recommended_mode"))
        notes = " ".join(res.get("notes", []))
        self.assertNotIn(
            "483-499", notes,
            "installer still cites the removed 5-line patch recipe")
        self.assertNotIn(
            "5-line", notes,
            "installer still describes the removed patch as the fix")

    def test_hermes_sets_memory_provider_in_config(self):
        """The real Hermes mechanism is memory.provider in config.yaml."""
        cfg = self.tmp / "config.yaml"
        cfg.write_text(
            "model: x\n"
            "memory:\n"
            "  memory_enabled: true\n"
            "  user_char_limit: 80000\n",
            encoding="utf-8")
        res = astor_install("hermes", self.tmp,
                           astor_verify_agent("hermes").get("recommended_mode"))
        self.assertTrue(res.get("changes"),
                        "hermes install planned no changes")
        patch = res["changes"][0]
        cfg.write_text(patch["content"], encoding="utf-8")
        self.assertIn("provider: astor_memory",
                      cfg.read_text(encoding="utf-8"))

    def test_hermes_install_is_idempotent(self):
        """A config already in the target shape is left alone.

        Priority mode hands memory to astor, so a config that still has
        the built-in store on is NOT finished -- the first run completes
        it and the second run has nothing left to do.
        """
        from astor_memory.installer.handlers import astor_install_hermes

        cfg = self.tmp / "config.yaml"
        cfg.write_text(
            "memory:\n  provider: astor_memory\n"
            "  memory_enabled: false\n  user_profile_enabled: false\n",
            encoding="utf-8")
        res = astor_install_hermes(self.tmp, "priority")
        self.assertFalse(res.get("changes"),
                         "re-running hermes install should be a no-op")
        notes = " ".join(res.get("notes", []))
        self.assertIn("already selects", notes)

    def test_hermes_install_completes_a_partial_config(self):
        """A config that names the provider but still has the built-in
        store on gets completed on the first run."""
        from astor_memory.installer.handlers import astor_install_hermes

        cfg = self.tmp / "config.yaml"
        cfg.write_text(
            "memory:\n  provider: astor_memory\n  memory_enabled: true\n",
            encoding="utf-8")
        res = astor_install_hermes(self.tmp, "priority")
        patches = [c for c in res.get("changes", [])
                    if c["path"].endswith("config.yaml")]
        self.assertTrue(patches, "a partial config was left alone")
        cfg.write_text(patches[0]["content"], encoding="utf-8")
        body = cfg.read_text(encoding="utf-8")
        self.assertIn("memory_enabled: false", body)
        self.assertIn("user_profile_enabled: false", body)
        # Exactly one provider key -- a second one would make yaml pick
        # one at random.
        self.assertEqual(body.count("provider: astor_memory"), 1)

    def test_hermes_install_handles_a_config_without_a_trailing_newline(self):
        """The last line of the memory block may have no newline; it
        still belongs to the block."""
        from astor_memory.installer.handlers import astor_install_hermes

        cfg = self.tmp / "config.yaml"
        cfg.write_text("memory:\n  provider: auto", encoding="utf-8")
        res = astor_install_hermes(self.tmp, "priority")
        patches = [c for c in res.get("changes", [])
                    if c["path"].endswith("config.yaml")]
        self.assertTrue(patches)
        cfg.write_text(patches[0]["content"], encoding="utf-8")
        body = cfg.read_text(encoding="utf-8")
        self.assertEqual(body.count("provider:"), 1)
        self.assertIn("provider: astor_memory", body)
        self.assertIn("memory_enabled: false", body)


if __name__ == "__main__":
    unittest.main()