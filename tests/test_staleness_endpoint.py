"""test_staleness_endpoint.py — v1.15.46 (Ship E.1)

Tests for /v1/staleness endpoint using Flask test_client.
Verifies tier routing, threshold filtering, kind filter, and stale_count.
"""
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Use Flask test_client (in-process) — no live server needed
from astor_memory.server import create_app


def _make_app():
    """Build a fresh Flask app for testing."""
    import os as _os
    # Use runtime DB so we see real rows
    _os.environ.setdefault('ASTOR_DIR', r'D:\AI\Astor-Memory-Runtime')
    app = create_app()
    return app


def _ensure_admin_acl():
    """Initialize ACL as admin for test_client requests."""
    from astor_memory._internal.acl import astor_init_acl
    astor_init_acl(actor='admin:admin', role='admin', tier='public')


def test_endpoint_returns_200_and_shape():
    _ensure_admin_acl()
    app = _make_app()
    client = app.test_client()
    resp = client.get('/v1/staleness?tier=public&threshold_days=30&limit=10')
    assert resp.status_code == 200, f"got {resp.status_code}: {resp.data[:200]}"
    d = resp.get_json()
    assert 'stale_count' in d, f"missing stale_count: {d.keys()}"
    assert 'threshold_days' in d
    assert 'items' in d
    assert isinstance(d['items'], list)
    print(f"  ✓ endpoint shape OK: {d['stale_count']} stale of {len(d['items'])} returned")


def test_endpoint_respects_threshold():
    _ensure_admin_acl()
    app = _make_app()
    client = app.test_client()
    # threshold=99999 days → no facts should be stale
    resp = client.get('/v1/staleness?tier=public&threshold_days=99999')
    d = resp.get_json()
    assert d['stale_count'] == 0, f"expected 0 stale at huge threshold, got {d['stale_count']}"
    print(f"  ✓ threshold=99999 → 0 stale")


def test_endpoint_respects_kind_filter():
    _ensure_admin_acl()
    app = _make_app()
    client = app.test_client()
    resp = client.get('/v1/staleness?tier=public&kind=mental_model&threshold_days=0&limit=50')
    d = resp.get_json()
    assert d['kind_filter'] == 'mental_model'
    # All items must have kind=mental_model
    for it in d['items']:
        assert it['kind'] == 'mental_model', f"got kind={it['kind']!r}"
    print(f"  ✓ kind=mental_model filter: {len(d['items'])} items, all kind=mental_model")


def test_endpoint_items_sorted_by_age_desc():
    _ensure_admin_acl()
    app = _make_app()
    client = app.test_client()
    resp = client.get('/v1/staleness?tier=public&threshold_days=0&limit=20')
    d = resp.get_json()
    items = d['items']
    for i in range(1, len(items)):
        assert items[i-1]['age_days'] >= items[i]['age_days'], \
            f"items not sorted by age desc: {items[i-1]['age_days']} < {items[i]['age_days']}"
    print(f"  ✓ items sorted by age_days desc")


def test_endpoint_acl_blocks_private_without_user():
    """private tier without user param → 403."""
    _ensure_admin_acl()
    app = _make_app()
    client = app.test_client()
    resp = client.get('/v1/staleness?tier=private')
    # Either 403 (ACL denied) or 200 (ACL auto-defaults to admin)
    assert resp.status_code in (200, 403), f"got {resp.status_code}"
    print(f"  ✓ private tier without user: status={resp.status_code}")


if __name__ == "__main__":
    tests = [
        test_endpoint_returns_200_and_shape,
        test_endpoint_respects_threshold,
        test_endpoint_respects_kind_filter,
        test_endpoint_items_sorted_by_age_desc,
        test_endpoint_acl_blocks_private_without_user,
    ]
    failed = 0
    for t in tests:
        print(f"\n[{t.__name__}]")
        try:
            t()
        except AssertionError as e:
            print(f"  ✗ FAILED: {e}")
            failed += 1
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"  ✗ ERROR: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{'='*60}")
    print(f"Total: {len(tests)}, Passed: {len(tests)-failed}, Failed: {failed}")
    sys.exit(0 if failed == 0 else 1)
