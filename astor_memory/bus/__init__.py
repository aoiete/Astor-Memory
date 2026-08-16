"""
astor_bus: SQLite append-only event log + canonical fact store.

Three tables:
- events: append-only event log (immutable history)
- memory_candidates: extracted facts before promotion
- memory_canonical: promoted facts (the actual memory)
- audit_log: per-action audit trail (7-year retention per Plan § Audit log)
"""
from .store import AstorBus, AstorEvent, astor_bus, astor_reset_bus
from .schema import astor_init_schema, astor_verify_schema, SCHEMA_VERSION

__all__ = [
    "AstorBus", "AstorEvent", "astor_bus", "astor_reset_bus",
    "astor_init_schema", "astor_verify_schema", "SCHEMA_VERSION",
]
