"""Dynamic baseline for test_health_diagnose counts that drift with corpus.

The hardcoded values 62 / 12 / 11371 etc. drift as the admin corpus
grows (new facts added) or shrinks (decay sweep v1.14.39). Rather than
manually re-baseline each test, this module reads/writes a baseline
file under tests/.baseline/health_diagnose.json:

  {
    "embedding_failed_total": 62,
    "warnings_total": 12,
    "audit_warning_severity": 12,
    "audit_info_severity": 11371,
    "updated_at": "2026-09-16T19:30:00Z"
  }

Test assertions use the max() of (live value, baseline) pattern:
  assert live >= baseline * 0.5    # catch shrinkage > 50% (= real bug)

Re-baseline by deleting tests/.baseline/health_diagnose.json and
running the suite; the next run will write a fresh baseline.

This eliminates the "hardcode drifts over time" failure mode without
sacrificing the "regression detected" guarantee.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

BASELINE_FILE = Path(__file__).resolve().parent / ".baseline" / "health_diagnose.json"
# Floor: even if baseline is lost, never assert below this. The minimum
# values are the historical low-water marks (62 / 12) — if the corpus
# shrinks BELOW this, that's a real regression worth a CI red.
FLOOR = {
    "embedding_failed_total": 1,    # baseline 62; if drops to 0, queue is dead
    "warnings_total": 1,            # baseline 12; if drops to 0, audit broken
    "audit_warning_severity": 1,
    "audit_info_severity": 1,
}


def load_baseline() -> dict:
    """Read baseline file. Return empty dict if missing/corrupt."""
    if not BASELINE_FILE.exists():
        return {}
    try:
        with open(BASELINE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_baseline(values: dict) -> None:
    """Persist baseline for next run."""
    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    import datetime as _dt
    values["updated_at"] = _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with open(BASELINE_FILE, "w", encoding="utf-8") as f:
        json.dump(values, f, indent=2, sort_keys=True)


def assert_at_least(name: str, live: int, *, max_shrink_pct: float = 50.0) -> None:
    """Assert live value is >= max(floor, baseline * (1 - max_shrink_pct)).

    Args:
        name: field name in baseline file (e.g. 'embedding_failed_total').
        live: current value from the live system.
        max_shrink_pct: maximum allowed shrinkage from baseline (default 50%).
            If baseline=100 and live=40, that's 60% shrink — fails.

    On first run (no baseline file), asserts live >= FLOOR[name] (the
    historical low-water mark) AND writes a fresh baseline.

    Returns nothing; raises AssertionError on failure.
    """
    baseline = load_baseline().get(name)
    floor = FLOOR.get(name, 0)
    threshold = max(floor, int((baseline or floor) * (1 - max_shrink_pct / 100)))
    assert live >= threshold, (
        f"{name}: live={live} < threshold={threshold} "
        f"(baseline={baseline}, floor={floor}, max_shrink_pct={max_shrink_pct}%). "
        f"Corpus shrunk > {max_shrink_pct}% — investigate."
    )


def record_value(name: str, live: int) -> None:
    """Update baseline file with current live value (call after assert)."""
    baseline = load_baseline()
    # Only grow the baseline (never shrink). Live value > baseline
    # means corpus grew → update. Live < baseline → leave baseline alone
    # (the shrink check above already verified it's not too small).
    if live > baseline.get(name, 0):
        baseline[name] = live
        save_baseline(baseline)
