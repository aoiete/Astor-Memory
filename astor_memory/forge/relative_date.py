"""relative_date.py — v1.10.9

Lightweight deterministic relative-date resolver used as a post-processor on
extracted facts. Catches the cases where the LLM extractor still leaves
`event_date` empty or fails to anchor "yesterday / last week" to an absolute
date, and resolves them against the document's timestamp anchor.

Why we need it (despite the LLM prompt also asking for temporal normalization):
  - The regex extractor never sees the document timestamp anchor, so its facts
    are systematically missing event_date for any relative reference.
  - The LLM extractor sometimes returns event_date=null for relative phrases
    when the anchor wasn't surfaced clearly in the chunk.
  - Deterministic post-processing guarantees every temporal fact lands in the
    memory_canonical.event_date column, which the temporal boost path in
    /v1/read relies on.

The resolver is intentionally cheap (regex-only, no LLM call). It walks the
fact.content text and matches a fixed set of English relative-time phrases,
then applies them to the anchor date.

Output:
  - For each (fact_dict, anchor_date_str) pair, returns an updated dict with
    event_date + event_date_precision filled when a relative phrase was found.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Iterable

try:
    from dateutil import parser as _dt_parser
except ImportError:
    _dt_parser = None

_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_DMY_RE = re.compile(r"\b(\d{1,2})[/\-\.](\d{1,2})(?:[/\-\.](\d{2,4}))?\b")
_MONTH_NAME_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|"
    r"October|November|December|"
    r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+(\d{1,2})(?:,?\s+(\d{4}))?\b",
    re.IGNORECASE,
)

_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_WEEKDAY_RE = re.compile(
    r"\b(" + "|".join(_WEEKDAYS) + r"|last\s+(?:" + "|".join(_WEEKDAYS) + r")"
    r"|next\s+(?:" + "|".join(_WEEKDAYS) + r"))\b",
    re.IGNORECASE,
)

_RELATIVE_PHRASES: list[tuple[re.Pattern[str], timedelta | str]] = [
    (re.compile(r"\btoday\b", re.IGNORECASE), timedelta(days=0)),
    (re.compile(r"\byesterday\b", re.IGNORECASE), timedelta(days=-1)),
    (re.compile(r"\btomorrow\b", re.IGNORECASE), timedelta(days=1)),
    (re.compile(r"\bthe day before yesterday\b", re.IGNORECASE), timedelta(days=-2)),
    (re.compile(r"\bthe other day\b", re.IGNORECASE), timedelta(days=-3)),
    (re.compile(r"\blast night\b", re.IGNORECASE), timedelta(days=-1)),
    (re.compile(r"\bthis morning\b", re.IGNORECASE), timedelta(days=0)),
    (re.compile(r"\btonight\b", re.IGNORECASE), timedelta(days=0)),
    (re.compile(r"\blast week\b", re.IGNORECASE), "week-1"),
    (re.compile(r"\bthis week\b", re.IGNORECASE), "week0"),
    (re.compile(r"\bnext week\b", re.IGNORECASE), "week+1"),
    (re.compile(r"\blast weekend\b", re.IGNORECASE), "week-1-weekend"),
    (re.compile(r"\bthis weekend\b", re.IGNORECASE), "week0-weekend"),
    (re.compile(r"\bnext weekend\b", re.IGNORECASE), "week+1-weekend"),
    (re.compile(r"\b(\d+)\s+days?\s+ago\b", re.IGNORECASE), "days_back"),
    (re.compile(r"\b(\d+)\s+weeks?\s+ago\b", re.IGNORECASE), "weeks_back"),
    (re.compile(r"\b(\d+)\s+months?\s+ago\b", re.IGNORECASE), "months_back"),
    (re.compile(r"\b(\d+)\s+years?\s+ago\b", re.IGNORECASE), "years_back"),
    (re.compile(r"\ba\s+(?:couple\s+of\s+)?weeks?\s+ago\b", re.IGNORECASE), "weeks_2"),
    (re.compile(r"\ba\s+few\s+days?\s+ago\b", re.IGNORECASE), timedelta(days=-3)),
    (re.compile(r"\bjust now\b", re.IGNORECASE), timedelta(days=0)),
    (re.compile(r"\bnow\b", re.IGNORECASE), timedelta(days=0)),
]


def _to_iso(d: date | datetime) -> str:
    return (d.date() if isinstance(d, datetime) else d).isoformat()


def _parse_anchor(anchor_str: str) -> date | None:
    """Accept ISO date, ISO datetime, or any string with a date prefix."""
    if not anchor_str:
        return None
    s = anchor_str.strip()[:25]
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except Exception:
        pass
    if _dt_parser is not None:
        try:
            dt = _dt_parser.parse(s, fuzzy=False)
            return dt.date()
        except Exception:
            pass
    m = _YEAR_RE.search(s)
    if m:
        try:
            return date(int(m.group(0)), 1, 1)
        except Exception:
            return None
    return None


def _last_weekday(d: date, weekday_name: str) -> date:
    target = _WEEKDAYS.index(weekday_name.capitalize())
    diff = (d.weekday() - target) % 7
    return d - timedelta(days=diff)


def _next_weekday(d: date, weekday_name: str) -> date:
    target = _WEEKDAYS.index(weekday_name.capitalize())
    diff = (target - d.weekday()) % 7
    if diff == 0:
        diff = 7
    return d + timedelta(days=diff)


def _extract_year_month(content: str) -> tuple[int | None, int | None, int | None]:
    m = _MONTH_NAME_RE.search(content)
    if m:
        month_name, day, year = m.groups()
        month_num = _month_name_to_num(month_name)
        if month_num is not None and year:
            try:
                return int(year), month_num, int(day)
            except Exception:
                pass
        if month_num is not None and day:
            return None, month_num, int(day)
    return None, None, None


_MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _month_name_to_num(name: str) -> int | None:
    return _MONTH_MAP.get(name.lower().rstrip(".").strip())


def resolve_relative_dates(fact: dict, anchor: str | date | None) -> dict:
    """Return a new fact dict with event_date/event_date_precision filled.

    Returns the dict unchanged if no relative phrase was matched.
    """
    if not isinstance(fact, dict):
        return fact
    if fact.get("event_date"):
        return fact
    content = (fact.get("content") or fact.get("context") or "") or ""
    if not content:
        return fact

    if isinstance(anchor, date):
        anchor_date = anchor
    else:
        anchor_date = _parse_anchor(anchor or "")
    if anchor_date is None:
        return fact

    matched: tuple[date, str] | None = None

    for pattern, delta in _RELATIVE_PHRASES:
        m = pattern.search(content)
        if not m:
            continue
        if delta == "days_back":
            n = int(m.group(1))
            matched = (anchor_date - timedelta(days=n), "day")
            break
        if delta == "weeks_back":
            n = int(m.group(1))
            matched = (anchor_date - timedelta(weeks=n), "day")
            break
        if delta == "months_back":
            n = int(m.group(1))
            year = anchor_date.year
            month = anchor_date.month - n
            while month <= 0:
                month += 12
                year -= 1
            matched = (date(year, month, min(anchor_date.day, 28)), "month")
            break
        if delta == "years_back":
            n = int(m.group(1))
            matched = (anchor_date.replace(year=anchor_date.year - n), "year")
            break
        if delta == "weeks_2":
            matched = (anchor_date - timedelta(weeks=2), "day")
            break
        if delta == "week-1":
            matched = (anchor_date - timedelta(weeks=1), "day")
            break
        if delta == "week0":
            matched = (anchor_date, "day")
            break
        if delta == "week+1":
            matched = (anchor_date + timedelta(weeks=1), "day")
            break
        if delta == "week-1-weekend":
            fri = anchor_date - timedelta(days=anchor_date.weekday() + 2)
            matched = (fri, "day")
            break
        if delta == "week0-weekend":
            days_to_sat = (5 - anchor_date.weekday()) % 7
            matched = (anchor_date + timedelta(days=days_to_sat), "day")
            break
        if delta == "week+1-weekend":
            days_to_sat = (5 - anchor_date.weekday()) % 7
            matched = (anchor_date + timedelta(days=days_to_sat + 7), "day")
            break
        if isinstance(delta, timedelta):
            matched = (anchor_date + delta, "day")
            break

    if matched is None:
        wm = _WEEKDAY_RE.search(content)
        if wm:
            token = wm.group(1).lower()
            try:
                if token.startswith("last "):
                    wd = token.split()[1]
                    matched = (_last_weekday(anchor_date, wd), "day")
                elif token.startswith("next "):
                    wd = token.split()[1]
                    matched = (_next_weekday(anchor_date, wd), "day")
                elif token.capitalize() in _WEEKDAYS:
                    if anchor_date.weekday() == _WEEKDAYS.index(token.capitalize()):
                        matched = (anchor_date, "day")
                    else:
                        matched = (_last_weekday(anchor_date, token), "day")
            except Exception:
                matched = None

    if matched is None:
        y, mo, d = _extract_year_month(content)
        if y is not None and mo is not None and d is not None:
            try:
                matched = (date(y, mo, d), "day")
            except Exception:
                matched = None
        elif y is not None and mo is not None:
            try:
                matched = (date(y, mo, 1), "month")
            except Exception:
                matched = None
        elif y is not None:
            try:
                matched = (date(y, 1, 1), "year")
            except Exception:
                matched = None
        else:
            dm = _DMY_RE.search(content)
            if dm:
                try:
                    d1, d2, d3 = dm.groups()
                    day = int(d1)
                    month = int(d2)
                    if d3:
                        year = int(d3) if len(d3) == 4 else 2000 + int(d3)
                    else:
                        year = anchor_date.year
                    matched = (date(year, month, min(day, 28)), "day")
                except Exception:
                    matched = None

    if matched is None:
        return fact

    resolved_date, precision = matched
    new_fact = dict(fact)
    new_fact["event_date"] = _to_iso(resolved_date)
    new_fact["event_date_precision"] = precision
    return new_fact


def resolve_relative_dates_batch(
    facts: Iterable[dict],
    anchor: str | date | None,
) -> list[dict]:
    return [resolve_relative_dates(f, anchor) for f in facts]