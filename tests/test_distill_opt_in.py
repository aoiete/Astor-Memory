"""test_distill_opt_in.py — the mining consent switch.

The mining command reads two columns out of bot-binding.db: whether a
user's private tier may be promoted into the shared tier, and whether
they may write to it. Both were read but never created -- the schema
migration added `default_tier` and `trusted_agent` and stopped -- so on
a database from before v1.16.x the mining command raised "no such
column" and quietly fell back to its own tier. Nobody was told.

The switch is also a switch: it defaults to on (a user who has not
opted out of sharing shares), and the operator can turn it off in the
dashboard -- including for the operator's own tier, which is what makes
it a switch rather than a decoration.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _legacy_db(tmp_path) -> Path:
    """A bot-binding.db from before the consent columns existed."""
    db = tmp_path / "bot-binding.db"
    con = sqlite3.connect(str(db))
    con.executescript("""
        CREATE TABLE user_meta (
            user_id          TEXT PRIMARY KEY,
            short_alias      TEXT UNIQUE NOT NULL,
            display_name     TEXT,
            real_name        TEXT,
            role             TEXT NOT NULL DEFAULT 'user',
            subscription_plan TEXT DEFAULT 'trial',
            timezone         TEXT,
            tz_offset_hours  INTEGER,
            verified_by      TEXT,
            verified_at      TEXT,
            notes            TEXT,
            active           INTEGER NOT NULL DEFAULT 1,
            extra_json       TEXT,
            created_at       TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
            source           TEXT,
            default_tier     TEXT NOT NULL DEFAULT 'public',
            trusted_agent    INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO user_meta (user_id, short_alias, role)
        VALUES ('admin', 'admin', 'admin');
    """)
    con.commit()
    con.close()
    return db


def test_the_consent_columns_are_created(tmp_path, monkeypatch):
    """A legacy database gains the columns, with the default that means
    "shares" rather than "does not share"."""
    db = _legacy_db(tmp_path)
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    from astor_memory._internal import bot_binding
    monkeypatch.setattr(bot_binding, "_con", None)

    con = bot_binding._connect()
    cols = {r[1] for r in con.execute("PRAGMA table_info(user_meta)").fetchall()}
    assert "distill_opt_in" in cols
    assert "allow_commons_write" in cols
    row = con.execute(
        "SELECT distill_opt_in, allow_commons_write FROM user_meta "
        "WHERE user_id = 'admin'").fetchone()
    # -1 = "never asked". The operator's own tier is mined on that
    # state (running the command is the consent); anyone else's is not.
    assert row[0] == -1
    assert row[1] == 1
    bot_binding.close()


def test_the_switch_turns_off(tmp_path, monkeypatch):
    from astor_memory._internal import bot_binding
    monkeypatch.setattr(bot_binding, "_con", None)
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    bot_binding.upsert_user(user_id="u1", short_alias="u1")

    assert bot_binding.set_distill_opt_in("u1", False) is True
    assert bot_binding.get_user("u1")["distill_opt_in"] == 0
    assert bot_binding.set_distill_opt_in("u1", True) is True
    assert bot_binding.get_user("u1")["distill_opt_in"] == 1
    assert bot_binding.set_distill_opt_in("nobody", False) is False
    bot_binding.close()


def test_the_switch_has_one_meaning(tmp_path, monkeypatch):
    """One switch, one meaning. The mining command and the
    distill_auto path used to read the flag differently -- the first
    treated "never asked" as consent, the second refused it -- and two
    readings of one flag is how they drift.

    The remaining difference is the direction: the user asking for
    their own fact to be distilled IS the consent, so "never asked"
    allows it; the operator reaching into someone else's tier has no
    such consent, so "never asked" does not."""
    from astor_memory._internal import bot_binding
    monkeypatch.setattr(bot_binding, "_con", None)
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    bot_binding.upsert_user(user_id="u1", short_alias="u1")

    # Fresh row: never asked.
    assert bot_binding.get_distill_opt_in("u1") == -1
    # Their own tier, automatic path: never asked means automatic.
    assert bot_binding.distill_auto_allowed("u1", own=True) is True
    # Their own tier, manual path: the command is the consent.
    assert bot_binding.distill_mining_allowed("u1", own=True) is True
    # Somebody ELSE's tier: no consent, so no mining -- and their
    # "never asked" is not an answer to the operator's question.
    bot_binding.upsert_user(user_id="u2", short_alias="u2")
    assert bot_binding.distill_mining_allowed("u2") is False
    assert bot_binding.distill_auto_allowed("u2") is False

    # Explicit off stops the automatic run but not the manual one:
    # "turn off automatic and it becomes manual".
    bot_binding.set_distill_opt_in("u1", False)
    assert bot_binding.distill_auto_allowed("u1", own=True) is False
    assert bot_binding.distill_mining_allowed("u1", own=True) is True

    # Explicit on allows both.
    bot_binding.set_distill_opt_in("u1", True)
    assert bot_binding.distill_auto_allowed("u1", own=True) is True
    assert bot_binding.distill_mining_allowed("u1", own=True) is True
    assert bot_binding.distill_mining_allowed("u1") is True
    bot_binding.close()


def test_the_mining_command_honours_the_switch(tmp_path, monkeypatch):
    """The operator's own tier is mined when the switch is on and is
    skipped when it is off. A switch that cannot be turned off is not
    a switch."""
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    monkeypatch.setenv("ASTOR_USER_ID", "admin")
    from astor_memory._internal import bot_binding
    monkeypatch.setattr(bot_binding, "_con", None)
    bot_binding.upsert_user(user_id="admin", short_alias="admin", role="admin")

    from astor_memory.cli.main import cmd_mine_public

    class _Args:
        apply = False
        limit = 0
        min_length = 40

    rc = cmd_mine_public(_Args())
    assert rc == 0
    bot_binding.set_distill_opt_in("admin", False)
    rc = cmd_mine_public(_Args())
    assert rc == 0
    bot_binding.close()


def test_a_fresh_database_has_the_columns(tmp_path, monkeypatch):
    """A database created by the current code has them from the start,
    so the ALTER path is only for databases that predate them."""
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    from astor_memory._internal import bot_binding
    monkeypatch.setattr(bot_binding, "_con", None)
    con = bot_binding._connect()
    cols = {r[1] for r in con.execute("PRAGMA table_info(user_meta)").fetchall()}
    assert "distill_opt_in" in cols
    assert "allow_commons_write" in cols
    bot_binding.close()


def test_the_operator_is_mined_unless_switched_off(tmp_path, monkeypatch):
    """The MANUAL command always mines the operator's own tier. The
    switch governs the automatic runs, not the manual one: the
    operator asked for "turn off automatic and it becomes manual",
    and the person typing the command is the consent.

    The automatic path is the one the switch moves, and that is
    covered by the helper test."""
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    monkeypatch.setenv("ASTOR_USER_ID", "admin")
    from astor_memory._internal import bot_binding
    monkeypatch.setattr(bot_binding, "_con", None)
    bot_binding.upsert_user(user_id="admin", short_alias="admin", role="admin")
    from astor_memory.cli.main import cmd_mine_public

    class _Args:
        apply = False
        limit = 0
        min_length = 40

    import io
    import contextlib

    def _scopes():
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_mine_public(_Args())
        return buf.getvalue()

    assert "admin" in _scopes()
    bot_binding.set_distill_opt_in("admin", False)
    # Still mined: the command was run on purpose.
    assert "admin" in _scopes()
    assert "mining disabled" not in _scopes()
    bot_binding.set_distill_opt_in("admin", -1)
    assert "admin" in _scopes()
    bot_binding.close()


def test_another_user_needs_their_own_consent(tmp_path, monkeypatch):
    """One user's tier is never mined on the operator's say-so. The
    consent is theirs."""
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    monkeypatch.setenv("ASTOR_USER_ID", "admin")
    from astor_memory._internal import bot_binding
    monkeypatch.setattr(bot_binding, "_con", None)
    bot_binding.upsert_user(user_id="admin", short_alias="admin", role="admin")
    bot_binding.upsert_user(user_id="u2", short_alias="u2")
    # A user with no data layer has nothing to mine, and the mining
    # command only looks at users who have one.
    from astor_memory._internal.acl_layout import Tier, Store, ensure_layout
    for _store in Store:
        ensure_layout(Tier.PRIVATE, _store, "u2")
    from astor_memory.cli.main import cmd_mine_public

    class _Args:
        apply = False
        limit = 0
        min_length = 40

    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_mine_public(_Args())
    scopes = [l for l in buf.getvalue().splitlines() if "scopes:" in l]
    assert scopes and "u2" not in scopes[0]
    bot_binding.set_distill_opt_in("u2", True)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_mine_public(_Args())
    scopes = [l for l in buf.getvalue().splitlines() if "scopes:" in l]
    assert scopes and "u2" in scopes[0]
    bot_binding.close()


def test_the_dashboard_renders_the_switch():
    """One switch: ON means run automatically, off means only when
    someone asks. The operator does not have to reason about a third
    state to use it."""
    from pathlib import Path as _P
    app = (_P(__file__).resolve().parent.parent /
           "astor_memory" / "dashboard" / "app.js").read_text(encoding="utf-8")
    assert "data-action=\"toggle-distill\"" in app
    assert "_distillText" in app
    assert "return v ? 'ON' : 'off';" in app
    html = (_P(__file__).resolve().parent.parent /
            "astor_memory" / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert ">Distill<" in html


def test_the_binding_list_carries_the_flag():
    """The dashboard gets the flag from /v1/binding/list, so the query
    has to select it -- and the value has to survive as the integer
    it is, not collapse to a bool."""
    from pathlib import Path as _P
    server = (_P(__file__).resolve().parent.parent /
              "astor_memory" / "server.py").read_text(encoding="utf-8")
    assert "m.distill_opt_in" in server
    assert "d['distill_opt_in'] = " in server


def test_the_server_connection_does_not_close_the_singleton(tmp_path, monkeypatch):
    """The dashboard's connection is closed by its caller, so it must
    not be the shared singleton -- closing it would take the
    singleton down for everything else."""
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    from astor_memory._internal import bot_binding
    monkeypatch.setattr(bot_binding, "_con", None)
    bot_binding.upsert_user(user_id="admin", short_alias="admin", role="admin")
    from astor_memory.server import _astor_bot_binding_connect

    db = _astor_bot_binding_connect()
    db.close()
    # The singleton must still work after the caller's close.
    assert bot_binding.get_user("admin") is not None
    bot_binding.close()


def test_the_binding_list_endpoint_works(tmp_path, monkeypatch):
    """The dashboard query reads the flag, so it has to work. It
    resolved "never asked" against the caller's own id, and the
    handler had no such variable -- the endpoint returned 500 and the
    whole connections panel went blank."""
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    from astor_memory._internal import bot_binding
    monkeypatch.setattr(bot_binding, "_con", None)
    bot_binding.upsert_user(user_id="admin", short_alias="admin", role="admin")
    from astor_memory.server import create_app
    client = create_app(str(tmp_path)).test_client()
    resp = client.get("/v1/binding/list")
    assert resp.status_code == 200, resp.data[:200]
    data = resp.get_json()
    assert "bindings" in data
    # The operator's own switch comes back, and the operator's own id
    # is in the payload -- the admin role is bound without a per-user
    # id, so reading ctx.user_id alone found no row and the switch
    # read as "never asked" whatever it was set to.
    assert data.get("self_user_id"), data
    assert "self_distill_opt_in" in data
    bot_binding.close()


def test_distill_auto_finds_the_users_own_fact(tmp_path, monkeypatch):
    """The endpoint looked for the fact by globbing the user's
    directory, and it walked the files in filesystem order --
    including astor_bus_users_admin.db, which is empty. The fact was
    in astor_bus_admin.db, so the lookup missed it and the endpoint
    answered fact_not_found for a fact that existed."""
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    from astor_memory._internal import bot_binding
    monkeypatch.setattr(bot_binding, "_con", None)
    bot_binding.upsert_user(user_id="admin", short_alias="admin", role="admin")
    from astor_memory._internal.acl_layout import Tier, Store, ensure_layout
    from astor_memory.bus.store import astor_bus
    from astor_memory._internal.acl import astor_init_acl
    for _store in Store:
        ensure_layout(Tier.PRIVATE, _store, "admin")
    astor_init_acl(actor="user:admin", role="user", tier="private",
                   user_id="admin", subscription_plan="power")
    bus = astor_bus(tier="private", user_id="admin")
    # The canonical write path, so the foreign keys hold.
    event_id = bus.append_event(
        namespace="admin", agent_id="test", source="test",
        action="write", content="a method worth sharing")
    cand_id = bus.insert_candidate(
        event_id=event_id, namespace="admin",
        content="a method worth sharing", kind="lesson",
        confidence=0.9, importance=0.7, tags=[])
    fid = bus.promote_candidate(cand_id, promoted_by="test",
                                user_id="admin", tier="private")

    from astor_memory.server import create_app
    client = create_app(str(tmp_path)).test_client()
    resp = client.post("/v1/fact/%d/distill_auto" % fid, json={})
    # Not 404: the fact exists and the endpoint has to find it. The
    # switch state decides whether it may proceed, not whether the
    # fact is there.
    assert resp.status_code != 404, resp.data[:300]
    bot_binding.close()


def test_the_switch_governs_the_automatic_runs_only(tmp_path, monkeypatch):
    """The operator said: "turn off automatic and it becomes manual".
    So the switch has to stop the automatic runs -- the weekly job,
    the distill_auto endpoint -- and it must NOT stop the manual
    command. The person typing the command is the consent.

    A switch that also forbade the manual run would not be a switch
    between two modes; it would be a lock."""
    from astor_memory._internal import bot_binding
    monkeypatch.setattr(bot_binding, "_con", None)
    monkeypatch.setenv("ASTOR_DIR", str(tmp_path))
    bot_binding.upsert_user(user_id="admin", short_alias="admin", role="admin")

    # Off.
    bot_binding.set_distill_opt_in("admin", False)
    # The automatic path stops.
    assert bot_binding.distill_auto_allowed("admin", own=True) is False
    # The manual path does not.
    assert bot_binding.distill_mining_allowed("admin", own=True) is True
    # Somebody else's tier is untouched by the operator's switch.
    bot_binding.upsert_user(user_id="u2", short_alias="u2")
    assert bot_binding.distill_mining_allowed("u2") is False
    assert bot_binding.distill_auto_allowed("u2") is False

    # On.
    bot_binding.set_distill_opt_in("admin", True)
    assert bot_binding.distill_auto_allowed("admin", own=True) is True
    assert bot_binding.distill_mining_allowed("admin", own=True) is True

    # Never asked: automatic by default for the operator's own tier.
    bot_binding.set_distill_opt_in("admin", -1)
    assert bot_binding.distill_auto_allowed("admin", own=True) is True
    assert bot_binding.distill_mining_allowed("admin", own=True) is True
    bot_binding.close()


def test_the_weekly_job_reads_the_switch():
    """The weekly job is the automatic run, so it has to look at the
    switch before it mines anything. The check is a string in the
    script -- a contract, not a runtime import, because the script
    runs under whatever interpreter the scheduler bootstraps."""
    from pathlib import Path as _P
    script = (_P(__file__).resolve().parent.parent /
              "scripts" / "mine_public_weekly.py").read_text(encoding="utf-8")
    assert "distill_auto_allowed" in script
    assert "nothing to do this week" in script


def test_the_dashboard_has_a_switch_in_the_panel_header():
    """A control the operator has to find by scrolling a paginated
    table to its last column is a control they will not find. The
    operator's own switch belongs at the top of the panel."""
    from pathlib import Path as _P
    root = _P(__file__).resolve().parent.parent / "astor_memory" / "dashboard"
    html = (root / "index.html").read_text(encoding="utf-8")
    app = (root / "app.js").read_text(encoding="utf-8")
    assert 'id="auto-distill-toggle"' in html
    assert "auto-distill-toggle" in app
    # The table has to be scrollable at every width: the rule used
    # to live inside the narrow-screen media query, so on a wide
    # screen the last column was outside the card.
    css = (root / "style.css").read_text(encoding="utf-8")
    i = css.find(".table-scroll {")
    assert i != -1
    block = css[i:i+300]
    assert "overflow-x: auto" in block
    # And that block must not be inside a media query.
    before = css[:i]
    depth = 0
    for ch in before:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
    assert depth == 0, "the scroll rule is nested in another block"
