"""test_dashboard_endpoint.py — verify /v1/dashboard endpoint via Flask test client.

Tests:
- GET /v1/dashboard returns 200 with payload
- Response contains all expected top-level keys
- _cache field is 'miss' on first call, 'hit' on second call (5min cache)
- Payload values are sane (non-negative ints, valid trend_status)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory.server import create_app, _DASHBOARD_CACHE  # noqa: E402

# Tests run against the live Astor-Memory-Runtime at D:/AI/Astor-Memory-Runtime.
# Pass ?astor_dir= override because Flask test client uses system HOME by default.
TEST_ASTOR_DIR = "D:/AI/Astor-Memory-Runtime"


def _reset_cache():
    """Module-level cache is shared across tests — reset before each."""
    _DASHBOARD_CACHE["payload"] = None
    _DASHBOARD_CACHE["ts"] = 0.0
    _DASHBOARD_CACHE["astor_dir"] = None


def _get_dashboard(client):
    """Helper: GET /v1/dashboard with explicit astor_dir, return (status, json)."""
    resp = client.get(f"/v1/dashboard?astor_dir={TEST_ASTOR_DIR}")
    try:
        return resp.status_code, resp.get_json()
    except Exception:
        return resp.status_code, None


def test_endpoint_returns_200():
    app = create_app()
    client = app.test_client()
    status, body = _get_dashboard(client)
    assert status == 200, f"expected 200 got {status}: {body}"
    assert body is not None


def test_payload_has_required_keys():
    app = create_app()
    client = app.test_client()
    _, body = _get_dashboard(client)
    expected = {"generated_at", "hero", "eval_trend", "per_user", "growth_30d",
                "top_keywords", "recent_facts", "importance_histogram", "health"}
    assert set(body.keys()) >= expected, f"missing: {expected - set(body.keys())}"


def test_cache_miss_then_hit():
    """First call: _cache=miss. Second call within TTL: _cache=hit."""
    _reset_cache()
    app = create_app()
    client = app.test_client()
    _, b1 = _get_dashboard(client)
    _, b2 = _get_dashboard(client)
    assert b1["_cache"] == "miss", f"first call expected miss got {b1['_cache']}"
    assert b2["_cache"] == "hit", f"second call expected hit got {b2['_cache']}"


def test_hero_numbers_sane():
    app = create_app()
    client = app.test_client()
    _, body = _get_dashboard(client)
    h = body["hero"]
    assert h["total_users"] >= 1, f"expected >=1 user, got {h['total_users']}"
    assert h["total_facts"] > 0
    assert h["active_facts"] <= h["total_facts"]
    assert h["trend_status"] in {"improving", "stable", "regressing", "no_data", "unknown"}


def test_eval_trend_shape():
    app = create_app()
    client = app.test_client()
    _, body = _get_dashboard(client)
    et = body["eval_trend"]
    assert "window_30d_baselines" in et
    assert "all_variants_last" in et
    assert "trend_status" in et


def test_per_user_list():
    app = create_app()
    client = app.test_client()
    _, body = _get_dashboard(client)
    pu = body["per_user"]
    assert isinstance(pu, list)
    assert len(pu) >= 1
    for u in pu:
        assert {"user", "facts", "active", "tombstoned", "high_imp"} <= set(u.keys())


def test_health_keys():
    app = create_app()
    client = app.test_client()
    _, body = _get_dashboard(client)
    h = body["health"]
    assert {"embedding_failed", "audit_warnings", "audit_total"} <= set(h.keys())


def test_recent_facts_limit():
    app = create_app()
    client = app.test_client()
    _, body = _get_dashboard(client)
    rf = body["recent_facts"]
    assert isinstance(rf, list)
    assert len(rf) <= 5


if __name__ == "__main__":
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
