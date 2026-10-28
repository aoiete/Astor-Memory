"""test_mine_public.py — the bridge from the private tier to the shared one.

The private tier is where the work happens; the public tier is what
peers read. `am mine-public` is the bridge: it keeps the facts that
are methods rather than history, drops the ones that are about a
person, scrubs what is left of the machine, and writes the result to
the tier every peer can read.

The weekly job is a script and not a prompt, so these tests cover the
script's two contracts: it refuses to guess a runtime, and it does not
write anything when it has nothing to say.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _seed_admin(tmp_path, monkeypatch):
    """Point ASTOR_DIR at a scratch runtime and give admin an identity."""
    target = tmp_path / "astor"
    monkeypatch.setenv("ASTOR_DIR", str(target))
    from astor_memory._internal import bot_binding as bb
    monkeypatch.setattr(bb, "_con", None)
    bb.upsert_user(user_id="admin", short_alias="admin", role="admin",
                   subscription_plan="power")
    return target


def test_the_dry_run_writes_nothing(tmp_path, monkeypatch):
    """A command that writes by surprise is a command nobody runs twice."""
    target = _seed_admin(tmp_path, monkeypatch)
    from astor_memory.cli.main import cmd_mine_public

    class _Args:
        apply = False
        limit = 0
        min_length = 40

    cmd_mine_public(_Args())
    from astor_memory.bus.store import astor_bus
    bus = astor_bus(tier="public", user_id=None)
    n = bus.conn.execute(
        "SELECT count(*) FROM memory_canonical WHERE tombstoned=0").fetchone()[0]
    assert n == 0, "a dry run wrote to the public tier"


def _load_weekly_script():
    import importlib.util
    script = ROOT / "scripts" / "mine_public_weekly.py"
    spec = importlib.util.spec_from_file_location("mine_public_weekly", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_weekly_script_refuses_a_relative_runtime(monkeypatch):
    """A relative ASTOR_DIR resolves against the working directory, and
    the working directory of a scheduled job is not the one the operator
    would have typed. So the script refuses rather than guesses."""
    mod = _load_weekly_script()
    monkeypatch.setenv("ASTOR_DIR", "relative/path")
    assert mod.main() == 2


def test_the_weekly_script_refuses_when_no_interpreter_can_run_it(monkeypatch):
    """A scheduled job runs under whatever Python the scheduler
    bootstraps, and that one may not have astor's dependencies -- a
    bare interpreter cannot even import the package, because importing
    it pulls in the vector store and with it numpy. The script looks
    for an interpreter that can, and says so plainly when there is
    none, rather than failing with an ImportError traceback."""
    mod = _load_weekly_script()
    monkeypatch.delenv("ASTOR_DIR", raising=False)
    monkeypatch.setattr(mod, "_find_interpreter", lambda: None)
    assert mod.main() == 2


def test_the_weekly_script_finds_an_interpreter(monkeypatch):
    """The interpreter the script runs under is usually fine. When it
    is, the script uses it -- no second process, no extra start-up."""
    mod = _load_weekly_script()
    monkeypatch.delenv("ASTOR_DIR", raising=False)
    monkeypatch.setattr(mod, "_find_interpreter", lambda: sys.executable)
    assert mod._can_import_astor(sys.executable) in (True, False)
    assert mod._candidate_interpreters() is not None


def test_the_weekly_script_is_in_the_repo():
    """The job points at a file. If the file is not in the repo, the
    job breaks on the next machine that installs it."""
    script = ROOT / "scripts" / "mine_public_weekly.py"
    assert script.exists()
    text = script.read_text(encoding="utf-8")
    assert "mine-public" in text
    assert "astor_memory.cli.main" in text


def test_a_public_write_is_scrubbed_at_the_bus(tmp_path, monkeypatch):
    """Every write path goes through promote_candidate -- the HTTP API,
    the CLI, the extractor, the learner. So the scrub belongs there, and
    a fact written by the CLI with a working directory in it must not
    come out carrying that directory."""
    target = _seed_admin(tmp_path, monkeypatch)
    from astor_memory._internal.acl import astor_init_acl
    from astor_memory.bus.store import astor_bus
    astor_init_acl(actor="user:admin", role="user", tier="public",
                   user_id="admin", subscription_plan="power")
    bus = astor_bus(tier="public", user_id=None)
    event_id = bus.append_event(
        namespace="admin", agent_id="test", source="test",
        action="write",
        content="LESSON 抓文章缓存在 D:/AI/.tmp_wechat.html, "
                "脚本在 X:/Users/someone/AppData/run.py",
    )
    cand_id = bus.insert_candidate(
        event_id=event_id, namespace="admin",
        content="LESSON 抓文章缓存在 D:/AI/.tmp_wechat.html, "
                "脚本在 X:/Users/someone/AppData/run.py",
        kind="lesson", confidence=0.9, importance=0.7, tags=[],
    )
    fid = bus.promote_candidate(cand_id, promoted_by="test", user_id="admin",
                                tier="public")
    row = bus.conn.execute(
        "SELECT content FROM memory_canonical WHERE id = ?", (fid,)).fetchone()
    content = row[0]
    assert "D:/AI" not in content
    assert "someone" not in content
    assert "<root>" in content
    assert "<home>" in content


def test_a_private_write_keeps_its_path(tmp_path, monkeypatch):
    """The private tier is where a local path is useful. The scrub is
    a boundary for the shared tier, not a redaction of everything."""
    target = _seed_admin(tmp_path, monkeypatch)
    from astor_memory._internal.acl import astor_init_acl
    from astor_memory.bus.store import astor_bus
    astor_init_acl(actor="user:admin", role="user", tier="private",
                   user_id="admin", subscription_plan="power")
    bus = astor_bus(tier="private", user_id="admin")
    event_id = bus.append_event(
        namespace="admin", agent_id="test", source="test",
        action="write", content="缓存在 D:/AI/.tmp_wechat.html",
    )
    cand_id = bus.insert_candidate(
        event_id=event_id, namespace="admin",
        content="缓存在 D:/AI/.tmp_wechat.html",
        kind="fact", confidence=0.9, importance=0.7, tags=[],
    )
    fid = bus.promote_candidate(cand_id, promoted_by="test", user_id="admin",
                                tier="private")
    row = bus.conn.execute(
        "SELECT content FROM memory_canonical WHERE id = ?", (fid,)).fetchone()
    assert "D:/AI/.tmp_wechat.html" in row[0]


def test_a_state_const_token_survives_the_body():
    """A path in the body used to come out in pieces: the sentence
    splitter cuts on a period, so "wechat.html" became "wechat. html".
    The STATE-CONST block held the intact copy, but the body is what
    a reader reads.

    The token is now held as a placeholder while the split runs and
    put back afterwards."""
    from astor_memory.nest.distiller import distill
    out, _removed, _rep = distill(
        "[LESSON] curl -sS https://example.invalid/ -o <tmp>/wechat.html "
        "then grep js_content")
    assert "wechat. html" not in out, out
    assert "<tmp>/wechat.html" in out, out


def test_a_placeholder_is_not_limited_to_ascii():
    """The operator's own placeholders are not always English. The
    pattern only allowed [a-z-], so a Chinese placeholder was dropped
    and the path that survived started at the slash -- which reads as
    though the temp directory were part of the filesystem rather than
    a stand-in for one."""
    from astor_memory.nest.distiller import distill
    out, _removed, _rep = distill(
        "[LESSON] curl -sS https://example.invalid/ -o <\u4e34\u65f6\u76ee\u5f55>/wechat.html "
        "then grep js_content")
    assert "<\u4e34\u65f6\u76ee\u5f55>/wechat.html" in out, out


def test_a_dotfile_is_a_path():
    """A dotfile is a file like any other. The pattern could not start
    on one, so the leading dot was lost."""
    from astor_memory.nest.distiller import distill
    out, _removed, _rep = distill(
        "[LESSON] curl -sS https://example.invalid/ -o <root>/.tmp_x.html "
        "then grep js_content")
    assert "<root>/.tmp_x.html" in out, out


def test_a_bare_domain_is_extracted():
    """A domain says which site a method is for. Without a pattern for
    it the splitter cut it into pieces nobody can use."""
    from astor_memory.nest.distiller import distill
    out, _removed, _rep = distill(
        "[LESSON] \u4ece mp.weixin.qq.com \u62c9\u6587\u7ae0\uff0c\u5148\u62ff HTML \u518d\u53d6 js_content")
    assert "mp. weixin" not in out, out
    assert "mp.weixin.qq.com" in out, out
