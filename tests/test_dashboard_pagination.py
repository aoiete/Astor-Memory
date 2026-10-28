# -*- coding: utf-8 -*-
"""The connections table keeps its page when the list is re-read.

Toggling a switch re-reads the list. The re-read used to set the page
back to the first one, so clicking a switch on the second page threw
the operator back to the top -- which reads as "the button did
nothing"."""
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "astor_memory" / "dashboard" / "app.js"


def _body(src, fn_name):
    i = src.find("function " + fn_name)
    assert i >= 0, fn_name
    depth = 0
    started = False
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
            started = True
        elif src[j] == "}":
            depth -= 1
            if started and depth == 0:
                return src[i:j + 1]
    raise AssertionError("unterminated " + fn_name)


def test_reading_the_list_does_not_reset_the_page():
    src = APP.read_text(encoding="utf-8")
    body = _body(src, "renderConnections")
    resets = re.findall(r"^\s*_connPage\s*=\s*0\s*;", body, re.M)
    assert not resets, (
        "renderConnections resets the page to the first one, so a "
        "switch pressed on page 2 jumps back to page 1")


def test_the_page_is_clamped_to_what_is_there():
    src = APP.read_text(encoding="utf-8")
    body = _body(src, "_drawConnPage")
    assert "_connPage >= pageCount" in body, body
    assert "_connPage < 0" in body, body
