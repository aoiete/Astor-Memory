"""test_pii_public_gate.py — v1.16+ PII hard gate for public tier.

Verifies the v1.16+ behavior: any write to tier='public' is forced through
Memory Defense PII scan (default policy 'block'). Caller may opt out with
body.pii_scan=False (admin escape hatch). Env ASTOR_PII_PUBLIC_FORCE=0
disables the entire gate (emergency escape — not for normal ops).

4 end-to-end assertions via Flask test_client + tmp_path ASTOR_DIR:
1. PII content + tier=public + pii_scan=True → 400 pii_blocked
2. PII content + tier=source → 200 success (source not gated)
3. PII content + tier=public + pii_scan=False → 200 success (escape hatch)
4. Clean content + tier=public → 200 success
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _seed_admin(tmp_path, monkeypatch):
    """Set ASTOR_DIR + seed bot-binding.db with admin actor so the test client
    can write to the public tier without 403s."""
    target = tmp_path / "astor"
    monkeypatch.setenv("ASTOR_DIR", str(target))
    from astor_memory._internal import bot_binding as bb
    monkeypatch.setattr(bb, "_con", None)
    bb.upsert_user(user_id="admin", short_alias="admin", role="admin", subscription_plan="power")
    return target


def _client(target):
    from astor_memory.server import create_app
    return create_app(str(target)).test_client()


def _reset_singletons():
    """Close + drop the bus and nest singletons so the next test case can
    create fresh DB handles against a new tmp_path. Without this, Windows
    holds file handles from previous tmpdir and rmtree fails on cleanup."""
    from astor_memory.bus import store as _bs
    if _bs._astor_bus_singleton is not None:
        try:
            _bs._astor_bus_singleton.close()
        except Exception:
            pass
    _bs._astor_bus_singleton = None
    from astor_memory.nest import vector_store as _vs
    singleton = _vs._nest_singleton
    if isinstance(singleton, dict):
        for inst in list(singleton.values()):
            try:
                inst.close()
            except Exception:
                pass
        singleton.clear()
    _vs._nest_singleton = None
    import gc
    gc.collect()


def _truthy(label: str, got) -> None:
    if not got:
        print(f"  [FAIL] {label} (got falsy: {got!r})")
        sys.exit(1)
    print(f"  [PASS] {label}")


def _eq(label: str, got, expected) -> None:
    if got != expected:
        print(f"  [FAIL] {label}\n    got     : {got!r}\n    expected: {expected!r}")
        sys.exit(1)
    print(f"  [PASS] {label}")


def main() -> int:
    print("=== PII public gate (v1.16+) ===")

    import tempfile
    from _pytest.monkeypatch import MonkeyPatch

    def _run_case(body: dict):
        _reset_singletons()
        tmp = tempfile.mkdtemp(prefix="astor_pii_gate_")
        try:
            target = _seed_admin(Path(tmp), MonkeyPatch())
            client = _client(target)
            return client.post("/v1/write", json=body)
        finally:
            _reset_singletons()
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)

    # ===== Case 1: PII + public → 400 pii_blocked =====
    r = _run_case({
        "text": "我的 moomoo 账号 13800138000 密码 abc123",
        "user": "admin",
        "tier": "public",
    })
    _eq("(1) PII + public → 400", r.status_code, 400)
    body = r.get_json()
    _truthy("(1) error key 'pii_blocked'", body.get("error") == "pii_blocked")

    # ===== Case 2: PII + source → 200 (source not gated) =====
    r = _run_case({
        "text": "我的 moomoo 账号 13800138000 密码 abc123",
        "user": "admin",
        "tier": "source",
    })
    _eq("(2) PII + source → 200", r.status_code, 200)

    # ===== Case 3: PII + public + pii_scan=False → 200 (escape hatch) =====
    r = _run_case({
        "text": "我的 moomoo 账号 13800138000 密码 abc123",
        "user": "admin",
        "tier": "public",
        "pii_scan": False,
    })
    _eq("(3) PII + public + escape → 200", r.status_code, 200)

    # ===== Case 4: clean + public → 200 =====
    r = _run_case({
        "text": "操作 moomoo 流程：1. 打开客户端 2. 登录 3. 选择股票",
        "user": "admin",
        "tier": "public",
    })
    _eq("(4) clean + public → 200", r.status_code, 200)

    print("\nAll tests PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())