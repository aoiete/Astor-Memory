"""Recognise machine noise in a canonical pool.

Every deployment of Astor accumulates rows that are not knowledge: tool
transcripts that the extractor captured verbatim, test probes, and
status dumps.  They are noise everywhere -- they are not a property of
one user's tier -- and they crowd out the rows that do carry knowledge.

This module only *recognises*.  Deciding what to do with a match is the
caller's business; ``am noise-scan`` reports, an archive command would
tombstone.  Keeping the predicate separate from the action is what
makes the judgement reviewable before anything is deleted.

The patterns are deliberately narrow.  A wide prefix match would take
real lessons with it: "maker_pocket" alone hits 187 rows of genuine
R-class writing, and "session-hygiene-async tick" hits rows that end
with a real conclusion ("three copies identical md5, no drift").  Only
the shapes that cannot be knowledge are matched here:

  * ``kind='event'`` whose content is a maker_pocket JSON snapshot --
    a state dump, never a rule.
  * a maker_pocket fact that *is* a JSON snapshot.
  * middleware warn-test rows and ZZBUGPROBE rows -- test fixtures.
  * ``[LESSON]`` rows whose whole body is a shell command, which the
    capture hook wrote when the agent ran a command containing the
    word "error" (the keyword match is case-insensitive).
"""

from __future__ import annotations

# JSON state snapshots, either as an event row or as a fact.
MAKER_POCKET_SNAPSHOT = "(content LIKE 'maker_pocket %: {%')"

# Test fixtures and probes.  Anchored at the start of the content: a
# lesson that *mentions* the fixture name ("...middleware-warn-test 139
# + ZZBUGPROBE 11...") is still knowledge, not a fixture.
TEST_FIXTURES = (
    "(content LIKE 'middleware-warn-test%'"
    " OR content LIKE 'ZZBUGPROBE%')"
)

# A shell command masquerading as a lesson.  The leading "[LESSON] LESSON"
# form is a real lesson that happens to quote a command token, so it is
# excluded explicitly.
_SHELL_TOKENS = (
    'tail -', 'head -', 'cat ', 'ls -la', 'netstat', 'curl ', 'grep ',
    'nvidia-smi', 'pip ', 'git ', 'python -', 'cd /', '&&', '<<%EOF%',
    'powershell ',
)
SHELL_COMMAND_TRANSCRIPT = (
    "(content LIKE '[LESSON] %' AND ("
    + ' OR '.join("content LIKE '%" + t + "%'" for t in _SHELL_TOKENS)
    + ") AND content NOT LIKE '[LESSON] LESSON%')"
)


def noise_sql() -> str:
    """The WHERE fragment that matches machine-noise rows."""
    return (
        "(kind = 'event' AND content LIKE 'maker_pocket%')"
        + " OR " + MAKER_POCKET_SNAPSHOT
        + " OR " + TEST_FIXTURES
        + " OR " + SHELL_COMMAND_TRANSCRIPT
    )


def label_of(content: str, kind: str = '') -> str:
    """Which noise bucket a row falls into (for reports)."""
    c = str(content or '')
    if kind == 'event' and c.lstrip().startswith('maker_pocket'):
        return 'maker_pocket state snapshot (event)'
    if c.lstrip().startswith('maker_pocket'):
        return 'maker_pocket state snapshot'
    if 'middleware-warn-test' in c:
        return 'middleware warn test fixture'
    if 'ZZBUGPROBE' in c:
        return 'ZZBUGPROBE test probe'
    if c.lstrip().startswith('[LESSON]') and 'LESSON [' not in c[:40]:
        return 'shell command transcript'
    return 'machine noise'


def scan(bus, limit: int = 200) -> list[dict]:
    """Return the noise rows in a bus, newest first. Read-only."""
    sql = (
        "SELECT id, kind, content, access_count, last_confirmed_at "
        "FROM memory_canonical WHERE tombstoned=0 AND (" + noise_sql() + ") "
        "ORDER BY id LIMIT ?"
    )
    rows = bus.conn.execute(sql, (int(limit),)).fetchall()
    out = []
    for r in rows:
        out.append({
            'id': r[0],
            'kind': r[1],
            'label': label_of(r[2], r[1]),
            'access_count': r[3],
            'last_confirmed_at': r[4],
            'content': str(r[2] or ''),
        })
    return out


def count(bus) -> tuple[int, int]:
    """(noise_rows, active_rows) for a bus."""
    noise = bus.conn.execute(
        "SELECT COUNT(*) FROM memory_canonical WHERE tombstoned=0 AND ("
        + noise_sql() + ")").fetchone()[0]
    active = bus.conn.execute(
        "SELECT COUNT(*) FROM memory_canonical WHERE tombstoned=0").fetchone()[0]
    return int(noise), int(active)
