# -*- coding: utf-8 -*-
"""distill_opt_in is a three-state flag, and the code says so.

The schema and the migration both default it to -1 ("never asked"),
which is different from 0 ("explicit no"). A comment that called the
default 0 sent a reader looking for a missing value that was not
missing, so the comment now has to agree with the DDL -- and this test
is what keeps them agreeing.
"""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "astor_memory" / "_internal"
       / "bot_binding.py").read_text(encoding="utf-8")


def test_the_schema_and_the_migration_agree_on_the_default():
    ddl = re.search(r"distill_opt_in\s+INTEGER\s+NOT NULL\s+DEFAULT\s+(-?\d+)", SRC)
    alter = re.search(r"ADD COLUMN distill_opt_in[^)]*DEFAULT\s+(-?\d+)", SRC)
    assert ddl and alter, "both the CREATE and the ALTER must declare a default"
    assert ddl.group(1) == alter.group(1) == "-1", (
        "the schema and the migration must default to -1 (never asked)")


def test_the_comment_does_not_claim_a_different_default():
    # The comment sits right above the column; it must not say the
    # default is 0, which is what made a reader think -1 was dirt.
    block = SRC[:SRC.find("distill_opt_in INTEGER")].rsplit("--", 30)[-1]
    assert "defaults to 0" not in block, (
        "the comment says the default is 0; the DDL says -1")
    assert "-1" in block, "the comment must document the three states"


def test_the_three_states_are_what_the_code_decides():
    import ast
    tree = ast.parse(SRC)
    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    auto = ast.unparse(fns["distill_auto_allowed"])
    mining = ast.unparse(fns["distill_mining_allowed"])
    # automatic: own tier -> anything but 0 runs; other tier -> 1 only
    assert "if own:" in auto and "value != 0" in auto, auto
    assert "value == 1" in auto, auto
    # manual: the operator's own tier always runs, the switch only
    # governs the automatic path
    assert "if own:" in mining and "return True" in mining, mining
    assert "value == 1" in mining, mining
