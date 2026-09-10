"""test_health_diagnose.py — verify /v1/health/diagnose endpoint + diagnose script.

Tests:
- /v1/health/diagnose returns 200 with embedding_failed / warnings / audit_total_by_severity
- embedding_failed.total == 62 (admin user)
- warnings.total == 12
- audit_total_by_severity includes 'info' + 'warning'
- Top errors has 'NoneType' as dominant
- All embedding_failed records are queued_for_replay=true
- astor_health_diagnose.py CLI runs end-to-end
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory.server import create_app  # noqa: E402

TEST_USER = "admin"
TEST_ASTOR_DIR = "D:/AI/Astor-Memory-Runtime"


def _get(client, path):
    return client.get(path)


def test_endpoint_200():
    app = create_app()
    client = app.test_client()
    r = _get(client, f"/v1/health/diagnose?user={TEST_USER}&astor_dir={TEST_ASTOR_DIR}")
    assert r.status_code == 200, f"expected 200 got {r.status_code}"
    body = r.get_json()
    assert body is not None
    return body


def test_endpoint_has_required_keys():
    body = test_endpoint_200()
    for k in ["embedding_failed", "warnings", "audit_total_by_severity"]:
        assert k in body, f"missing key: {k}"


def test_embedding_total_62():
    body = test_endpoint_200()
    assert body["embedding_failed"]["total"] == 62, \
        f"expected 62, got {body['embedding_failed']['total']}"


def test_embedding_top_error_is_nonetype():
    body = test_endpoint_200()
    errors = body["embedding_failed"]["errors"]
    assert len(errors) >= 1
    top_err = errors[0][0]
    assert "NoneType" in top_err or "acl" in top_err or "atexit" in top_err, \
        f"unexpected top error: {top_err}"


def test_embedding_all_queued_for_replay():
    body = test_endpoint_200()
    assert body["embedding_failed"]["no_replay_queued"] == 0, \
        f"some embedding failed records not queued: {body['embedding_failed']['no_replay_queued']}"


def test_warnings_total_12():
    body = test_endpoint_200()
    assert body["warnings"]["total"] == 12, \
        f"expected 12, got {body['warnings']['total']}"


def test_warnings_all_forget():
    body = test_endpoint_200()
    events = [e[0] for e in body["warnings"]["by_event"]]
    assert events == ["forget"], f"unexpected warning events: {events}"


def test_audit_severity_has_info_and_warning():
    body = test_endpoint_200()
    sev = body["audit_total_by_severity"]
    assert "info" in sev
    assert "warning" in sev
    assert sev["warning"] == 12


def test_user_not_found_returns_404():
    app = create_app()
    client = app.test_client()
    r = _get(client, "/v1/health/diagnose?user=nonexistent_xyz&astor_dir=" + TEST_ASTOR_DIR)
    assert r.status_code == 404
    body = r.get_json()
    assert body.get("error") == "user_db_not_found"


def test_diagnose_script_runs():
    """End-to-end smoke: scripts/astor_health_diagnose.py exits 0."""
    r = subprocess.run(
        ['D:/AI/PY-311/Scripts/python.exe',
         'scripts/astor_health_diagnose.py',
         '--user', TEST_USER,
         '--astor-dir', TEST_ASTOR_DIR],
        capture_output=True, text=True, timeout=30, cwd=str(ROOT),
    )
    assert r.returncode == 0, f"script failed: {r.stderr}"
    assert "Embedding Failed" in r.stdout
    assert "Audit Warnings" in r.stdout
    assert "62" in r.stdout  # the total
    assert "12" in r.stdout  # the warning count


def test_diagnose_script_embedding_list():
    """--embedding mode lists records."""
    r = subprocess.run(
        ['D:/AI/PY-311/Scripts/python.exe',
         'scripts/astor_health_diagnose.py',
         '--embedding', '--limit', '5',
         '--user', TEST_USER, '--astor-dir', TEST_ASTOR_DIR],
        capture_output=True, text=True, timeout=30, cwd=str(ROOT),
    )
    assert r.returncode == 0, f"script failed: {r.stderr}"
    assert "Embedding Failed records" in r.stdout
    assert "NoneType" in r.stdout


def test_diagnose_script_warnings_list():
    """--warnings mode lists records."""
    r = subprocess.run(
        ['D:/AI/PY-311/Scripts/python.exe',
         'scripts/astor_health_diagnose.py',
         '--warnings', '--limit', '5',
         '--user', TEST_USER, '--astor-dir', TEST_ASTOR_DIR],
        capture_output=True, text=True, timeout=30, cwd=str(ROOT),
    )
    assert r.returncode == 0, f"script failed: {r.stderr}"
    assert "Audit Warnings" in r.stdout
    assert "forget" in r.stdout


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
        print(f"\n{len(failed)}/{len(tests)} failed: {failed}")
        sys.exit(1)
    print(f"\n{len(tests)}/{len(tests)} tests passed")
