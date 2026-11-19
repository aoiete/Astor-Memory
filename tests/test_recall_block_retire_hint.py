"""The recall block tells the agent how to retire a fact it rejected.

The feedback hook only fires on a short verdict ("还是不行"). A
rejection that arrives as a long technical report is the normal shape
of one for this operator, and the agent can tell where a keyword rule
cannot. The block already carries the fact ids, so the affordance
costs one line.

The plugin lives outside this repository -- it is the Hermes memory
provider -- so the test locates it through the Hermes home and skips
when it is not installed. Nothing here hardcodes a host path.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path


def _hermes_home():
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        pass
    env = os.environ.get("HERMES_HOME")
    return Path(env) if env else None


def _plugin_dir():
    home = _hermes_home()
    if home is None:
        return None
    for candidate in (home / "hermes-agent" / "plugins" / "memory" / "astor_memory",
                      home / "plugins" / "memory" / "astor_memory"):
        if (candidate / "__init__.py").exists():
            return candidate
    return None


PLUGIN = _plugin_dir()
_HOME = _hermes_home()
AGENT_DIR = (_HOME / "hermes-agent") if _HOME is not None else None


def _load_block_formatter():
    spec = importlib.util.spec_from_file_location(
        "astor_plugin_probe", PLUGIN / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._format_recall_block


@unittest.skipIf(PLUGIN is None or AGENT_DIR is None,
                 "the Hermes astor_memory plugin is not installed here")
class TestRecallBlockTellsHowToRetire(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if str(AGENT_DIR) not in sys.path:
            sys.path.insert(0, str(AGENT_DIR))
        # staticmethod: a plain function stored on the class would be
        # bound as a method and get self passed as the first argument
        cls.fmt = staticmethod(_load_block_formatter())

    def _block(self, results):
        return self.fmt({"results": results}, 0.12)

    def test_the_line_is_present(self):
        block = self._block([{
            "fact_id": 9299, "content": "some fact", "kind": "fact",
            "importance": 0.5, "similarity": 0.8, "level_text": "some fact",
            "status": "active",
        }])
        self.assertIn("astor_demote", block,
                      "the agent has to be told the tool exists")
        self.assertIn("id=NNN", block,
                      "and told where the ids are")

    def test_it_does_not_appear_without_results(self):
        self.assertEqual(self.fmt({"results": []}, 0.12), "")

    def test_the_ids_are_in_the_block_too(self):
        """The instruction points at ids the block must actually carry."""
        block = self._block([{
            "fact_id": 4242, "content": "another fact", "kind": "fact",
            "importance": 0.5, "similarity": 0.8, "level_text": "another fact",
            "status": "active",
        }])
        self.assertIn("4242", block)


if __name__ == "__main__":
    unittest.main()
