"""astor_memory/export/markdown.py — the memory as a readable artifact.

Astor stores memory in SQLite, which is good for querying and bad for
reading: you cannot open it, you cannot diff it, and you cannot hand
it to another tool. The Agent Memory Repo format (Cognition, 2026-10-05)
picks the opposite trade: plain Markdown files in a Git repository, so
the memory is something a person can open, review, diff and roll back.

This module produces that view without changing how astor stores
anything. The export is a projection -- the database stays the source
of truth, and re-exporting overwrites the files.

The layout follows the spec's shape:

    MEMORY.md          the short entry point: what is in here and where
    <kind>.md          one bullet per memory, with open [key: value] tags
    tombstoned.md      what was removed and why

Two things the spec asks for that astor can answer better than the
plain format does, because the database already tracks them:

  - provenance: every bullet carries where it came from, not just when.
  - time: a bullet states its validity window and whether a later
    revision replaced it, so a reader can tell a live rule from a
    stale one without trusting the prose.

Facts about entities are cross-linked with [[wiki links]] so related
memory can be followed by hand, which is the whole point of keeping
the export in Markdown.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Iterable

# Kinds that get their own file. Everything else lands in fact.md.
SPECIAL_KINDS = (
    "rule", "user_preference", "lesson", "success_pattern", "failure_pattern",
    "correction", "mental_model", "knowledge_page", "reflection", "observation",
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str, limit: int = 40) -> str:
    s = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return (s[:limit].rstrip("-") or "x")


def _bus_paths(astor_dir: Path) -> list:
    """Every bus database under this astor directory."""
    return sorted(Path(astor_dir).rglob("astor_bus_*.db"))


def _read_rows(astor_dir: Path, tiers: Iterable[str] | None = None) -> list:
    rows = []
    for db in _bus_paths(astor_dir):
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
            con.row_factory = sqlite3.Row
        except Exception:
            continue
        try:
            want = ",".join("?" * len(list(tiers))) if tiers else None
            sql = ("SELECT * FROM memory_canonical WHERE 1=1"
                   + (" AND tier IN (%s)" % want if want else "")
                   + " ORDER BY importance DESC, id")
            args = list(tiers) if tiers else []
            for r in con.execute(sql, args):
                d = {k: r[k] for k in r.keys()}
                d["_db"] = db.name
                rows.append(d)
        except Exception:
            pass
        finally:
            con.close()
    return rows


def _entities(row: dict) -> list:
    raw = row.get("entities_json")
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except Exception:
        return []
    out = []
    for e in items if isinstance(items, list) else []:
        if isinstance(e, dict):
            v = str(e.get("value") or "").strip()
            if v:
                out.append(v)
    return out


def _tag_block(row: dict) -> str:
    """The open [key: value] metadata the spec allows on every entry."""
    tags = []
    add = tags.append
    add("id:%s" % row.get("id"))
    if row.get("kind"):
        add("kind:%s" % row["kind"])
    if row.get("memory_class"):
        add("class:%s" % row["memory_class"])
    if row.get("confidence") is not None:
        add("conf:%.2f" % float(row["confidence"]))
    if row.get("importance") is not None:
        add("imp:%.2f" % float(row["importance"]))
    if row.get("scope_type"):
        add("scope:%s" % row["scope_type"])
    if row.get("status"):
        add("status:%s" % row["status"])
    if row.get("verdict"):
        add("verdict:%s" % row["verdict"])
    if row.get("valid_from"):
        add("from:%s" % str(row["valid_from"])[:10])
    if row.get("valid_until"):
        add("until:%s" % str(row["valid_until"])[:10])
    if row.get("expires_at"):
        add("expires:%s" % str(row["expires_at"])[:10])
    if row.get("superseded_by"):
        add("superseded_by:%s" % row["superseded_by"])
    if row.get("invalidated_reason"):
        add("invalidated:%s" % row["invalidated_reason"])
    if row.get("provenance_kind"):
        add("prov:%s" % row["provenance_kind"])
    if row.get("provenance_agent"):
        add("by:%s" % row["provenance_agent"])
    if row.get("source_ref"):
        add("src:%s" % row["source_ref"])
    if row.get("promoted_by"):
        add("promoted:%s" % row["promoted_by"])
    if row.get("added"):
        pass
    return " ".join("[%s]" % t for t in tags)


def _wiki_links(row: dict) -> str:
    """Links to other memories that mention the same entity."""
    ents = _entities(row)
    if not ents:
        return ""
    out = []
    for e in ents[:3]:
        out.append("[[%s]]" % e)
    return " ".join(out)


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def render(rows: list, astor_dir: Path) -> dict:
    """Build the file set. Returns {relative path: text}."""
    live = [r for r in rows if not r.get("tombstoned")]
    dead = [r for r in rows if r.get("tombstoned")]

    # The same memory can exist in more than one tier's database. For a
    # readable export, one memory should be one bullet: keep the copy
    # with the highest importance and note how many there were.
    seen = {}
    for r in live:
        key = (r.get("kind"), _one_line(r.get("content") or ""))
        if not key[1]:
            continue
        prev = seen.get(key)
        if prev is None or (r.get("importance") or 0) > (prev.get("importance") or 0):
            seen[key] = r
    dupes = len(live) - len(seen)
    live = list(seen.values())

    by_kind = defaultdict(list)
    for r in live:
        by_kind[r.get("kind") or "fact"].append(r)

    files = {}
    counts = {}
    per_file = defaultdict(lambda: defaultdict(int))
    for kind, group in sorted(by_kind.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        name = "%s.md" % (_slug(kind) if kind in SPECIAL_KINDS else "fact")
        if name in files:
            files[name] += "\n## %s\n\n" % kind
        else:
            files[name] = "# %s\n\n" % kind
        files[name] += ("%d entries of kind %s. One bullet per memory; the "
                        "bracketed tags are open metadata, not a fixed "
                        "schema.\n\n" % (len(group), kind))
        for r in group:
            body = _one_line(r.get("content") or "")
            if not body:
                continue
            link = _wiki_links(r)
            line = "- %s %s%s" % (body, _tag_block(r), (" " + link) if link else "")
            files[name] += line + "\n"
        counts[kind] = len(group)
        per_file[name][kind] = len(group)

    if dead:
        lines = ["# tombstoned", "",
                 "%d entries left the canonical table. The reason is on "
                 "every line, so a wrong removal can be traced instead of "
                 "guessed at." % len(dead), ""]
        moved = [r for r in dead
                 if "moved" in str(r.get("invalidated_reason") or "").lower()]
        removed = [r for r in dead if r not in moved]
        if removed:
            lines += ["## removed (%d)" % len(removed), ""]
        for r in sorted(removed, key=lambda x: -(x.get("importance") or 0)):
            body = _one_line(r.get("content") or "")
            if not body:
                continue
            lines.append("- %s %s" % (body, _tag_block(r)))
        if moved:
            lines += ["", "## moved to another tier (%d)" % len(moved), "",
                      "These were relocated, not judged wrong. They are still "
                      "alive in the tier they moved to.", ""]
        for r in sorted(moved, key=lambda x: -(x.get("importance") or 0)):
            body = _one_line(r.get("content") or "")
            if not body:
                continue
            lines.append("- %s %s" % (body, _tag_block(r)))
        files["tombstoned.md"] = "\n".join(lines) + "\n"

    total = len(live)
    index = ["# MEMORY", "",
             "The short entry point. Read this first; it says what is here "
             "and where each kind lives.", "",
             "- %d live entries across %d kinds" % (total, len(by_kind)), ""]
    for name, kinds in sorted(per_file.items(), key=lambda kv: -sum(kv[1].values())):
        detail = ", ".join("%s %d" % (k, n) for k, n in
                           sorted(kinds.items(), key=lambda kv: -kv[1]))
        index.append("- [[%s]] — %s" % (name, detail))
    if dupes:
        index.append("")
        index.append("%d duplicate copies across tiers were collapsed."
                     % dupes)
    if dead:
        index.append("- [[tombstoned.md]] — removed entries (%d)" % len(dead))
    index += ["",
              "Regenerate with `am export`. The database is the source of "
              "truth; these files are a projection of it."]
    files["MEMORY.md"] = "\n".join(index) + "\n"
    return files


def export(astor_dir: Path, out_dir: Path, tiers=None, git: bool = False,
           dry_run: bool = False) -> dict:
    """Write the export. Returns a small report."""
    astor_dir = Path(astor_dir)
    out_dir = Path(out_dir)
    rows = _read_rows(astor_dir, tiers)
    files = render(rows, astor_dir)
    if dry_run:
        return {"files": len(files), "rows": len(rows), "out": str(out_dir),
                "dry_run": True}
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (out_dir / name).write_text(text, encoding="utf-8")
    # Keep the index first in the file list so the directory reads well.
    (out_dir / "README.md").write_text(
        "# astor memory export\n\n"
        "Plain Markdown projection of the astor database, in the shape of "
        "the Agent Memory Repo format: MEMORY.md is the entry point, "
        "everything else is one bullet per memory.\n\n"
        "Regenerate with `am export`. Do not edit by hand -- the next "
        "export overwrites it.\n", encoding="utf-8")
    report = {"files": len(files) + 1, "rows": len(rows),
              "out": str(out_dir), "git": False}
    if git:
        report["git"] = _git_commit(out_dir)
    return report


def _git_commit(out_dir: Path) -> bool:
    """Init or reuse a repo and commit the export.

    A separate repository per export is the point: the memory can be
    reviewed and rolled back on its own, without dragging whatever else
    lives next to it into the history.
    """
    try:
        if not (out_dir / ".git").exists():
            subprocess.run(["git", "init", "-q"], cwd=str(out_dir), check=True,
                           capture_output=True, timeout=60)
        add = subprocess.run(["git", "add", "-A"], cwd=str(out_dir),
                             capture_output=True, timeout=60)
        if add.returncode != 0:
            return False
        msg = "astor memory export"
        # An explicit identity: a bare `git commit` fails on a machine
        # that never set user.name, and the export would silently end
        # up with no commit at all.
        subprocess.run(["git", "-c", "user.name=astor-export",
                        "-c", "user.email=astor@localhost",
                        "commit", "-q", "-m", msg],
                       cwd=str(out_dir), capture_output=True, timeout=60)
        chk = subprocess.run(["git", "log", "--oneline", "-1"], cwd=str(out_dir),
                              capture_output=True, text=True, timeout=60)
        return bool((chk.stdout or "").strip())
    except Exception:
        return False
