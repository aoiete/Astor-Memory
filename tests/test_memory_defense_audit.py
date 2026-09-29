"""test_memory_defense_audit.py — v1.15.45 (Ship P0.1b)

PII scan results now persist to bus.audit_log. Two paths tested:
  1. block policy → severity='critical'
  2. redact policy → severity='warning'

Verify the audit entry exists in bus.audit_log with right event name
('memory_defense_scan') and severity.
"""
import sqlite3
import sys
import os
import tempfile
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from astor_memory.bus.store import astor_bus as _astor_bus_factory
from astor_memory._internal.acl import astor_check_write as _acl_func
from astor_memory.bus.schema import astor_init_schema
from astor_memory.nest.memory_defense import audit_log_record, PIIMatch


def _make_bus() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")
    astor_init_schema(conn)
    return conn


def _audit_count(conn, event="memory_defense_scan"):
    return conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE event = ?", (event,)
    ).fetchone()[0]


def test_audit_log_record_writes_to_bus():
    """audit_log_record persists to bus.audit_log with correct severity."""
    bus = _make_bus()
    try:
        # Patch bus via bus_factory indirection — memory_defense imports astor_bus lazily.
        # For test simplicity, monkey-patch.
        from unittest.mock import patch as _patch

        # Build a fake bus that records calls
        class _FakeAstorBus:
            def __init__(self):
                self.audit_calls = []

            def write_audit(self, **kwargs):
                self.audit_calls.append(kwargs)
                # Persist into the bus.audit_log table
                bus.execute(
                    "INSERT INTO audit_log (event, actor, target_type, target_id, "
                    "new_state, severity, metadata) VALUES (?, ?, ?, ?, ?, ?, '{}')",
                    (kwargs['event'], kwargs['actor'], kwargs['target_type'],
                     kwargs.get('target_id'), kwargs.get('new_state'),
                     kwargs.get('severity', 'info')),
                )
                bus.commit()

        fake_bus = _FakeAstorBus()

        # Mock the astor_bus + astor_check_write imports inside memory_defense
        from astor_memory.bus.store import astor_bus as _astor_bus_factory
        with _patch.object(sys.modules['astor_memory.bus.store'], 'astor_bus', return_value=fake_bus):
            with _patch.object(sys.modules['astor_memory._internal.acl'], 'astor_check_write') as _acl:
                matches = [
                    PIIMatch(name='openai_api_key', severity='block', start=0, end=10,
                             snippet='sk-abc123'),
                ]
                entry = audit_log_record(
                    fact_id=42, user='admin:admin', tier='public',
                    matches=matches, policy_applied='block',
                )

        # Verify DB has the audit row
        n = _audit_count(bus)
        assert n == 1, f"expected 1 audit row, got {n}"
        row = bus.execute(
            "SELECT event, actor, target_type, target_id, severity "
            "FROM audit_log WHERE event='memory_defense_scan'"
        ).fetchone()
        assert row['event'] == 'memory_defense_scan'
        assert row['severity'] == 'critical', f"expected critical, got {row['severity']}"
        assert row['actor'] == 'admin:admin'
        assert row['target_id'] == '42'
        # new_state should be JSON with the entry dict
        parsed = json.loads(row['new_state'] if row['new_state'] else '{}') \
            if 'new_state' in row.keys() else {}
        # new_state is in row keys but Row object - read separately
        new_state_row = bus.execute(
            "SELECT new_state FROM audit_log WHERE event='memory_defense_scan'"
        ).fetchone()
        parsed = json.loads(new_state_row['new_state'])
        assert parsed['match_count'] == 1
        assert parsed['matches'][0]['name'] == 'openai_api_key'
        assert parsed['matches'][0]['severity'] == 'block'
        assert 'fingerprint' in parsed['matches'][0]
        print("  ✓ audit_log_record writes to bus.audit_log (block → critical)")
    finally:
        bus.close()


def test_redact_policy_severity_warning():
    """redact policy produces severity='warning'."""
    bus = _make_bus()
    try:
        from unittest.mock import patch as _patch
        from astor_memory.bus.store import astor_bus as _astor_bus_factory

        class _FakeAstorBus:
            def __init__(self):
                self.audit_calls = []

            def write_audit(self, **kwargs):
                bus.execute(
                    "INSERT INTO audit_log (event, actor, target_type, target_id, "
                    "new_state, severity, metadata) VALUES (?, ?, ?, ?, ?, ?, '{}')",
                    (kwargs['event'], kwargs['actor'], kwargs['target_type'],
                     kwargs.get('target_id'), kwargs.get('new_state'),
                     kwargs.get('severity', 'info')),
                )
                bus.commit()

        fake_bus = _FakeAstorBus()

        with _patch.object(sys.modules['astor_memory.bus.store'], 'astor_bus', return_value=fake_bus):
            with _patch.object(sys.modules['astor_memory._internal.acl'], 'astor_check_write') as _acl:
                matches = [
                    PIIMatch(name='telegram_chat_id', severity='redact', start=0, end=9,
                             snippet='12345678'),
                ]
                entry = audit_log_record(
                    fact_id=None, user='admin:admin', tier='public',
                    matches=matches, policy_applied='redact',
                )

        row = bus.execute(
            "SELECT severity FROM audit_log WHERE event='memory_defense_scan'"
        ).fetchone()
        assert row['severity'] == 'warning', f"expected warning, got {row['severity']}"
        print("  ✓ redact policy → severity='warning'")
    finally:
        bus.close()


def test_no_audit_when_no_matches():
    """No matches → no audit row (don't pollute audit log on every write)."""
    bus = _make_bus()
    try:
        from unittest.mock import patch as _patch
        from astor_memory.bus.store import astor_bus as _astor_bus_factory

        class _FakeAstorBus:
            def write_audit(self, **kwargs):
                bus.execute(
                    "INSERT INTO audit_log (event, actor, severity) VALUES (?, ?, ?)",
                    (kwargs['event'], kwargs['actor'], kwargs.get('severity', 'info')),
                )

        with _patch.object(sys.modules['astor_memory.bus.store'], 'astor_bus', return_value=_FakeAstorBus()):
            with _patch.object(sys.modules['astor_memory._internal.acl'], 'astor_check_write'):
                # Empty matches list
                entry = audit_log_record(
                    fact_id=1, user='admin:admin', tier='public',
                    matches=[], policy_applied='redact',
                )

        n = _audit_count(bus)
        assert n == 0, f"expected 0 audit rows (no matches), got {n}"
        print("  ✓ no audit row when no matches (zero noise)")
    finally:
        bus.close()


def test_fingerprint_in_audit_entry():
    """Verify fingerprint is persisted (audit-safe, no raw secrets)."""
    bus = _make_bus()
    try:
        from unittest.mock import patch as _patch
        from astor_memory.bus.store import astor_bus as _astor_bus_factory

        class _FakeAstorBus:
            def write_audit(self, **kwargs):
                bus.execute(
                    "INSERT INTO audit_log (event, actor, new_state, severity) "
                    "VALUES (?, ?, ?, ?)",
                    (kwargs['event'], kwargs['actor'], kwargs.get('new_state', ''),
                     kwargs.get('severity', 'info')),
                )

        with _patch.object(sys.modules['astor_memory.bus.store'], 'astor_bus', return_value=_FakeAstorBus()):
            with _patch.object(sys.modules['astor_memory._internal.acl'], 'astor_check_write'):
                matches = [
                    PIIMatch(name='email', severity='redact', start=0, end=10,
                             snippet='user@example.com'),
                ]
                entry = audit_log_record(
                    fact_id=5, user='admin:admin', tier='public',
                    matches=matches, policy_applied='redact',
                )

        row = bus.execute(
            "SELECT new_state FROM audit_log WHERE event='memory_defense_scan'"
        ).fetchone()
        parsed = json.loads(row['new_state'])
        # fingerprint is sha256(snippet)[:12], not literal 'fp_abc123'
        assert parsed['matches'][0]['fingerprint'] == 'b4c9a289323b', f"got {parsed['matches'][0]['fingerprint']}"
        # Verify NO raw secret leaked
        assert 'user@example.com' not in row['new_state'], "raw secret leaked into audit!"
        print("  ✓ fingerprint persisted, raw secret NOT in audit log")
    finally:
        bus.close()


if __name__ == "__main__":
    tests = [
        test_audit_log_record_writes_to_bus,
        test_redact_policy_severity_warning,
        test_no_audit_when_no_matches,
        test_fingerprint_in_audit_entry,
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
