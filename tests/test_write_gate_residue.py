"""test_write_gate_residue.py — 写入口的测试残留拦截 (行为测试).

2026-10-23 (HaluMem 驱动): public 层曾有 301 条 e2e 残留, 来自
tests/test_bitemporal_e2e.py 用 tier=public 打 live server (绕过了
conftest 的 ASTOR_DIR 隔离)。这些 fact 的 access_count 最高到 500 ——
它们在污染每一次召回。

现在 /v1/write 会把测试残留**自动降级**到调用方自己的 private 桶:
  · 不拒绝写入 —— e2e 套件断言的是 live server, 拒绝会让它红
  · 但共享层 (public / source) 不再被污染
  · 被降级的行仍能被 tests/memory_hygiene.py 的同款规则识别并清理

这个文件验证的是**行为** (事实最终落在哪一层), 不是只测规则函数。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from astor_memory.server import create_app


def _tier_rows(runtime: Path, tier: str, user_id: str | None = None) -> list:
    """Read every non-tombstoned content row of a tier directly from disk."""
    if tier == "public":
        db = runtime / "public" / "memory" / "astor_bus_public.db"
    elif tier == "source":
        db = runtime / "source" / "memory" / "astor_bus_source.db"
    else:
        db = (runtime / "users" / (user_id or "admin") / "memory" /
              f"astor_bus_{user_id or 'admin'}.db")
    if not db.exists():
        return []
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute(
            "SELECT content FROM memory_canonical WHERE tombstoned=0"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [r[0] for r in rows]


@pytest.fixture()
def app_and_client(tmp_path, monkeypatch):
    runtime = tmp_path / "astor"
    monkeypatch.setenv("ASTOR_DIR", str(runtime))
    app = create_app()
    return app.test_client(), runtime


RESIDUE = "Maria 完成了 e2e_test_marker_abc12345_1792725303 测试"
KNOWLEDGE = "astor recall hybrid merge uses bm25 weight 0.6 for lexical path"


def test_residue_write_to_public_does_not_land_in_public(app_and_client):
    """A test artefact asked for tier=public must NOT end up in public."""
    client, runtime = app_and_client
    r = client.post("/v1/write", json={
        "text": RESIDUE,
        "user": "admin",
        "tier": "public",
    })
    # The write itself still succeeds — we demote, we don't reject.
    assert r.status_code == 200, r.get_data(as_text=True)

    public = _tier_rows(runtime, "public")
    assert RESIDUE not in public, "测试残留进了 public 层"
    assert not any("e2e_test_marker" in c for c in public), \
        "public 层仍有 e2e 残留"

    # ... and it landed in the caller's own private bucket instead.
    private = _tier_rows(runtime, "private", "admin")
    assert any("e2e_test_marker" in c for c in private), \
        "测试残留没有落到 admin private"


def test_real_knowledge_still_lands_in_public(app_and_client):
    """Normal knowledge must NOT be demoted — the gate must not eat facts."""
    client, runtime = app_and_client
    r = client.post("/v1/write", json={
        "text": KNOWLEDGE,
        "user": "admin",
        "tier": "public",
        # v1.17.15: only a shareable kind lands in the public tier.
        # "lesson" is on the shareable list, so real knowledge goes
        # there and a personal fact does not.
        "kind": "lesson",
    })
    assert r.status_code == 200, r.get_data(as_text=True)
    public = _tier_rows(runtime, "public")
    assert any(KNOWLEDGE in c for c in public), "真知识没有落进 public 层"


def test_residue_write_to_private_is_untouched(app_and_client):
    """private tier is the caller's own bucket — no demotion needed."""
    client, runtime = app_and_client
    r = client.post("/v1/write", json={
        "text": RESIDUE,
        "user": "admin",
        "tier": "private",
    })
    assert r.status_code == 200, r.get_data(as_text=True)
    private = _tier_rows(runtime, "private", "admin")
    assert any("e2e_test_marker" in c for c in private)


def test_prefix_probe_is_demoted_too(app_and_client):
    """Prefix-style probes must be caught by the same gate."""
    client, runtime = app_and_client
    probe = "astor write path instrumentation probe deadbeef1234"
    r = client.post("/v1/write", json={
        "text": probe,
        "user": "admin",
        "tier": "public",
    })
    assert r.status_code == 200, r.get_data(as_text=True)
    public = _tier_rows(runtime, "public")
    assert not any("instrumentation probe" in c for c in public)


def test_source_tier_residue_is_demoted(app_and_client):
    """source is a shared knowledge tier too — same treatment."""
    client, runtime = app_and_client
    r = client.post("/v1/write", json={
        "text": RESIDUE,
        "user": "admin",
        "tier": "source",
    })
    assert r.status_code == 200, r.get_data(as_text=True)
    source = _tier_rows(runtime, "source")
    assert not any("e2e_test_marker" in c for c in source)
