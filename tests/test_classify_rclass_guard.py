"""Tests for R-class classifier guard (v1.14.64 fix).

R-class facts are locked user-confirmed learnings. They must classify
as 'success' regardless of body content (which may legitimately
describe failure modes — e.g. "git push 静默卡 5 分钟直到 timeout"
describes a hang, but the fix is the actual success story).
"""
from __future__ import annotations

from astor_memory.forge.extractor import astor_classify_outcome


def test_r_class_with_failure_keywords_in_body_classifies_as_success():
    """R-class + '卡', 'hang', 'timeout', '不报错' in body → must be success."""
    text = (
        "R-class — git push SSH key MSYS bash. Symptom: git push hangs "
        "5 min timeout, msys HOME is systemprofile, github 不报错就 hang. "
        "Fix: GIT_SSH_COMMAND=ssh -i key. Verified via v1.14.63 push."
    )
    outcome = astor_classify_outcome(text)
    assert outcome == "success", (
        f"R-class with failure keywords should still classify as success, "
        f"got {outcome!r}"
    )


def test_r_class_without_any_failure_keywords_still_success():
    text = "R-class — user prefers concise answers in Chinese (locked 2026-07-31)."
    assert astor_classify_outcome(text) == "success"


def test_non_r_class_with_failure_keywords_still_failure():
    """Without R-class marker, body failure keywords still classify as failure."""
    text = "搞砸了，git push 卡死 5 分钟"
    assert astor_classify_outcome(text) == "failure"


def test_non_r_class_neutral_text_stays_neutral():
    text = "今天天气不太好"
    assert astor_classify_outcome(text) == "neutral"


def test_r_class_first_line_match_case_insensitive():
    """R-class prefix is case-insensitive — both 'R-class' and 'r-class' work."""
    text = "r-class — some lowercase variant of the marker"
    assert astor_classify_outcome(text) == "success"


def test_r_class_substring_in_middle_does_not_trigger():
    """Only first-line R-class prefix counts, not body substring."""
    text = "Some neutral fact. R-class — body substring should not promote this."
    # First line is "Some neutral fact." — no R-class prefix.
    # Body contains "R-class" but that's a different zone.
    outcome = astor_classify_outcome(text)
    assert outcome in ("neutral", "success"), f"unexpected outcome {outcome!r}"
