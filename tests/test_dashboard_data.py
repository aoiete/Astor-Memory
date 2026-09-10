"""test_dashboard_data.py — verify dashboard aggregation.

Tests:
- Returns expected top-level keys
- Hero numbers are non-negative integers
- eval_trend handles empty/missing history gracefully
- top_keywords is a list of {keyword, count} dicts
- importance_histogram has 4 buckets
- per_user sorted by active desc
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory.dashboard_data import build_dashboard_payload  # noqa: E402

ASTOR_DIR = "D:/AI/Astor-Memory-Runtime"


def test_required_keys():
    p = build_dashboard_payload(ASTOR_DIR)
    expected = {"generated_at", "hero", "eval_trend", "per_user", "growth_30d",
                "top_keywords", "recent_facts", "importance_histogram", "health"}
    assert set(p.keys()) >= expected, f"missing keys: {expected - set(p.keys())}"


def test_hero_invariants():
    p = build_dashboard_payload(ASTOR_DIR)
    h = p["hero"]
    assert h["total_users"] >= 0
    assert h["total_facts"] >= 0
    assert h["active_facts"] >= 0
    assert h["active_facts"] <= h["total_facts"]
    assert h["tombstoned"] == h["total_facts"] - h["active_facts"]
    assert h["trend_status"] in {"improving", "stable", "regressing", "no_data", "unknown"}


def test_eval_trend_safe():
    p = build_dashboard_payload(ASTOR_DIR)
    et = p["eval_trend"]
    assert "window_30d_baselines" in et
    assert "all_variants_last" in et
    assert "trend_status" in et


def test_top_keywords_shape():
    p = build_dashboard_payload(ASTOR_DIR)
    tk = p["top_keywords"]
    assert isinstance(tk, list)
    for item in tk:
        assert "keyword" in item
        assert "count" in item
        assert isinstance(item["count"], int)


def test_importance_histogram_buckets():
    p = build_dashboard_payload(ASTOR_DIR)
    h = p["importance_histogram"]
    assert set(h.keys()) >= {"critical (>=0.9)", "high (0.7-0.9)", "mid (0.5-0.7)", "low (<0.5)"}
    assert sum(h.values()) >= 0


def test_per_user_sorted():
    p = build_dashboard_payload(ASTOR_DIR)
    pu = p["per_user"]
    if len(pu) >= 2:
        for i in range(len(pu) - 1):
            assert pu[i]["active"] >= pu[i + 1]["active"], "per_user not sorted by active desc"


def test_recent_facts_shape():
    p = build_dashboard_payload(ASTOR_DIR)
    rf = p["recent_facts"]
    assert isinstance(rf, list)
    assert len(rf) <= 5
    for f in rf:
        assert {"id", "content", "importance", "ts"} <= set(f.keys())


def test_growth_30d_dict():
    p = build_dashboard_payload(ASTOR_DIR)
    g = p["growth_30d"]
    assert isinstance(g, dict)
    for d, c in g.items():
        assert len(d) == 10  # YYYY-MM-DD
        assert isinstance(c, int) and c >= 0


def test_health_keys():
    p = build_dashboard_payload(ASTOR_DIR)
    h = p["health"]
    assert {"embedding_failed", "audit_warnings", "audit_total"} <= set(h.keys())


def test_idempotent():
    p1 = build_dashboard_payload(ASTOR_DIR)
    p2 = build_dashboard_payload(ASTOR_DIR)
    # hero numbers should be identical across immediate calls (no race)
    assert p1["hero"]["total_facts"] == p2["hero"]["total_facts"]
    assert p1["hero"]["total_users"] == p2["hero"]["total_users"]


if __name__ == "__main__":
    # pytest-compatible: each test_* runs as standalone assertion
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = []
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed.append(t.__name__)
    if failed:
        print(f"\n{len(failed)}/{len(tests)} tests failed: {failed}")
        sys.exit(1)
    print(f"\n{len(tests)}/{len(tests)} tests passed")
