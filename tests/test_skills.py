"""tests/test_skills.py — v1.16.13 (2026-09-30)

Tests for MemSkill-inspired Skill Bank abstraction.

Critical tests:
  - Skill.invoke wraps run() in try/except (no raise on bug)
  - SkillBank.register / get / list / unregister
  - Controller-style selection by tag (any / all)
  - Bank size invariant
  - Built-in skills auto-loaded on first get_bank()
  - Built-in skills wrap real astor operations (smoke)
  - reset_bank() lets tests start clean
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astor_memory.nest.skills import (
    Skill,
    SkillBank,
    get_bank,
    reset_bank,
    controller_select,
)


def _echo_skill(name: str, tags: list[str], payload_key: str = "msg"):
    """Helper: build a Skill that echoes a key from context."""
    def run(ctx):
        return {"echoed": ctx.get(payload_key), "ctx_keys": sorted(ctx.keys())}
    return Skill(name=name, description=f"echo {payload_key}", tags=tags, run=run)


def _raising_skill(name: str, tags: list[str]):
    """Helper: build a Skill that always raises."""
    def run(ctx):
        raise ValueError("intentional test failure")
    return Skill(name=name, description="raises", tags=tags, run=run)


class TestSkillInvoke(unittest.TestCase):
    def test_invoke_returns_result_with_metadata(self):
        s = _echo_skill("echo_test", ["t1"])
        r = s.invoke({"msg": "hello"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["skill"], "echo_test")
        self.assertEqual(r["echoed"], "hello")
        self.assertIn("elapsed_ms", r)

    def test_invoke_wraps_exception(self):
        s = _raising_skill("fail_test", ["t1"])
        r = s.invoke({})
        self.assertFalse(r["ok"])
        self.assertIn("ValueError", r["error"])
        self.assertEqual(r["skill"], "fail_test")

    def test_invoke_empty_context(self):
        s = _echo_skill("empty_test", ["t1"])
        r = s.invoke({})
        self.assertTrue(r["ok"])
        self.assertIsNone(r["echoed"])

    def test_non_dict_result_gets_wrapped(self):
        def return_list(ctx):
            return [1, 2, 3]
        s = Skill(name="list_test", description="returns list", tags=["t1"], run=return_list)
        r = s.invoke({})
        self.assertTrue(r["ok"])
        self.assertEqual(r["value"], [1, 2, 3])


class TestSkillBank(unittest.TestCase):
    def setUp(self):
        self.bank = SkillBank()

    def test_register_and_get(self):
        s = _echo_skill("s1", ["t1"])
        self.bank.register(s)
        self.assertIs(self.bank.get("s1"), s)
        self.assertIsNone(self.bank.get("missing"))

    def test_register_replaces(self):
        s1 = _echo_skill("dup", ["t1"])
        s2 = _echo_skill("dup", ["t2"])
        self.bank.register(s1)
        self.bank.register(s2)
        self.assertEqual(self.bank.size(), 1)
        self.assertEqual(self.bank.get("dup").tags, ["t2"])

    def test_register_requires_name(self):
        with self.assertRaises(ValueError):
            self.bank.register(Skill(name="", description="", tags=[], run=lambda c: {}))

    def test_unregister(self):
        self.bank.register(_echo_skill("u", ["t1"]))
        self.assertTrue(self.bank.unregister("u"))
        self.assertFalse(self.bank.unregister("u"))  # second call returns False
        self.assertEqual(self.bank.size(), 0)

    def test_list_with_tag_filter(self):
        self.bank.register(_echo_skill("a", ["alpha"]))
        self.bank.register(_echo_skill("b", ["beta"]))
        self.bank.register(_echo_skill("c", ["alpha", "beta"]))
        all_skills = self.bank.list()
        self.assertEqual(len(all_skills), 3)
        alpha = self.bank.list(tag="alpha")
        self.assertEqual({d["name"] for d in alpha}, {"a", "c"})

    def test_select_any(self):
        self.bank.register(_echo_skill("a", ["x"]))
        self.bank.register(_echo_skill("b", ["y"]))
        self.bank.register(_echo_skill("c", ["z"]))
        out = self.bank.select(["x", "y"], require_all=False)
        names = {s.name for s in out}
        self.assertEqual(names, {"a", "b"})

    def test_select_all(self):
        self.bank.register(_echo_skill("a", ["x", "y"]))
        self.bank.register(_echo_skill("b", ["x"]))
        out = self.bank.select(["x", "y"], require_all=True)
        self.assertEqual([s.name for s in out], ["a"])

    def test_invoke_by_name(self):
        self.bank.register(_echo_skill("invoke_me", ["t1"]))
        r = self.bank.invoke("invoke_me", {"msg": "from bank"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["echoed"], "from bank")

    def test_invoke_missing_skill(self):
        r = self.bank.invoke("nope", {})
        self.assertFalse(r["ok"])
        self.assertIn("not found", r["error"])


class TestSingletonBank(unittest.TestCase):
    def setUp(self):
        reset_bank()

    def tearDown(self):
        reset_bank()

    def test_singleton_returns_same_instance(self):
        a = get_bank()
        b = get_bank()
        self.assertIs(a, b)

    def test_builtin_skills_loaded(self):
        bank = get_bank()
        names = {d["name"] for d in bank.list()}
        # 4 built-ins per skills.py
        self.assertIn("coref_resolve", names)
        self.assertIn("path_score", names)
        self.assertIn("bitemporal_invalidate", names)
        self.assertIn("episode_link", names)

    def test_builtin_skill_has_tags(self):
        bank = get_bank()
        s = bank.get("coref_resolve")
        self.assertIsNotNone(s)
        self.assertIn("write_hook", s.tags)
        self.assertIn("preprocess", s.tags)


class TestControllerSelect(unittest.TestCase):
    def setUp(self):
        self.bank = SkillBank()
        self.bank.register(_echo_skill("alpha_skill", ["alpha"]))
        self.bank.register(_echo_skill("beta_skill", ["beta"]))
        self.bank.register(_echo_skill("gamma_skill", ["gamma"]))

    def test_select_by_name_passthrough(self):
        out = controller_select(["alpha_skill"], bank=self.bank)
        self.assertEqual(out, ["alpha_skill"])

    def test_select_missing_name_dropped(self):
        out = controller_select(["alpha_skill", "nonexistent"], bank=self.bank)
        self.assertEqual(out, ["alpha_skill"])

    def test_select_by_tag(self):
        out = controller_select(["alpha"], bank=self.bank)
        self.assertEqual(out, ["alpha_skill"])

    def test_select_by_multiple_tags_returns_all_matches(self):
        out = controller_select(["alpha", "beta"], bank=self.bank)
        self.assertEqual(set(out), {"alpha_skill", "beta_skill"})

    def test_select_empty_request(self):
        out = controller_select([], bank=self.bank)
        self.assertEqual(out, [])


class TestBuiltinSkillSmoke(unittest.TestCase):
    """Smoke tests that built-in skills actually wrap real astor ops."""

    def setUp(self):
        reset_bank()

    def tearDown(self):
        reset_bank()

    def test_path_score_skill_smoke(self):
        """path_score_skill runs without bus (uses stub context)."""
        bank = get_bank()
        s = bank.get("path_score")
        self.assertIsNotNone(s)
        r = s.invoke({"query": "Maria", "anchor": {"id": 1, "content": "Maria test"}})
        self.assertTrue(r["ok"])
        self.assertIn("direct", r)
        self.assertIn("path", r)

    def test_coref_skill_empty_text_returns_no_change(self):
        bank = get_bank()
        s = bank.get("coref_resolve")
        r = s.invoke({"text": ""})
        self.assertFalse(r["changed"])
        self.assertEqual(r["resolutions"], [])

    def test_episode_link_requires_fact_id(self):
        bank = get_bank()
        s = bank.get("episode_link")
        r = s.invoke({"episode_id": 1})  # missing fact_id
        self.assertFalse(r["ok"])
        self.assertIn("fact_id required", r["error"])

    def test_bitemporal_invalidate_requires_fact_id(self):
        bank = get_bank()
        s = bank.get("bitemporal_invalidate")
        r = s.invoke({})
        self.assertFalse(r["ok"])
        self.assertIn("fact_id required", r["error"])


if __name__ == "__main__":
    unittest.main()
