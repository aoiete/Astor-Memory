"""The operator path must not be a way around the privacy rules.

`am confirm` publishes a pending candidate as a commons fact with
`force=True`, which skips the store's own check ("tier='public'
requires visibility='commons'"). The kind whitelist says the candidate
is a method or a lesson -- it says nothing about whether the content is
private. So the content has to be judged here, or the operator path
becomes a hole straight through the red line.

v1.17.33: the content is classified before it is published, and with
--llm the method may be abstracted out of it first. What gets published
is always the abstraction, never the original.
"""
import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from astor_memory.bus import AstorBus as Bus

# astor_memory.cli.main resolves to the main() *function* through the
# package's __init__, so reach the module explicitly.
cli_main = importlib.import_module('astor_memory.cli.main')

PRIVATE = "我的账户余额是 12345，密码是 hunter2"
CLEAN = "先设置止损，再确认仓位上限，最后按计划放大仓位。"


@pytest.fixture
def bus_with_candidate(tmp_path, monkeypatch):
    bus = Bus(tmp_path / "cand.db")
    made = {}

    def _mk(content, kind='lesson'):
        ev = bus.append_event(namespace='admin', agent_id='a', source='s',
                              action='write', content='c')
        cid = bus.insert_candidate(event_id=ev, namespace='admin',
                                   content=content, kind=kind)
        made['id'] = cid
        return cid

    monkeypatch.setattr(cli_main, "_pending_buses",
                        lambda args: [('public', bus)])
    yield bus, _mk
    bus.close()


class _Args:
    def __init__(self, **kw):
        self.candidate_id = 0
        self.force = True
        self.kind = ''
        self.tier = 'all'
        self.user = 'admin'
        self.llm = False
        for k, v in kw.items():
            setattr(self, k, v)


def _canonical(bus):
    return bus.conn.execute(
        "SELECT content, visibility FROM memory_canonical").fetchall()


def test_private_candidate_is_refused(bus_with_candidate):
    bus, mk = bus_with_candidate
    cid = mk(PRIVATE)
    args = _Args(candidate_id=cid, kind='lesson')
    assert cli_main.cmd_confirm(args) == 2
    assert _canonical(bus) == []


def test_clean_candidate_is_published(bus_with_candidate):
    bus, mk = bus_with_candidate
    cid = mk(CLEAN)
    args = _Args(candidate_id=cid, kind='lesson')
    assert cli_main.cmd_confirm(args) == 0
    rows = _canonical(bus)
    assert len(rows) == 1
    assert rows[0][1] == 'commons'
    assert rows[0][0] == CLEAN


def test_llm_publishes_the_abstraction_not_the_original(bus_with_candidate,
                                                         monkeypatch):
    bus, mk = bus_with_candidate
    cid = mk(PRIVATE)
    from astor_memory.nest import llm_judge
    monkeypatch.setattr(llm_judge, "abstract_to_shareable",
                        lambda text, **kw: {
                            "shareable": True,
                            "text": "先设置止损，再确认仓位上限，最后放大仓位。",
                            "reason": "abstracted", "provider": "fake",
                            "signals": {}})
    args = _Args(candidate_id=cid, kind='lesson', llm=True)
    assert cli_main.cmd_confirm(args) == 0
    rows = _canonical(bus)
    assert len(rows) == 1
    content = rows[0][0]
    assert rows[0][1] == 'commons'
    # the original private text is nowhere in the published fact
    assert '12345' not in content
    assert 'hunter2' not in content
    assert '账户余额' not in content


def test_llm_refusal_still_refuses(bus_with_candidate, monkeypatch):
    """An abstraction the rules would not accept must not be published.

    The LLM module judges its own proposal, but the publisher re-checks
    it anyway: this is the last line before the text reaches a tier
    every peer can read, and a swapped or buggy module must not be able
    to talk the rules out of it.
    """
    bus, mk = bus_with_candidate
    cid = mk(PRIVATE)
    from astor_memory.nest import llm_judge
    monkeypatch.setattr(llm_judge, "abstract_to_shareable",
                        lambda text, **kw: {
                            "shareable": True,
                            # still carries the private marker
                            "text": "先设置止损，再确认我的账户余额，最后放大仓位",
                            "reason": "abstracted", "provider": "fake",
                            "signals": {}})
    args = _Args(candidate_id=cid, kind='lesson', llm=True)
    assert cli_main.cmd_confirm(args) == 2
    assert _canonical(bus) == []
