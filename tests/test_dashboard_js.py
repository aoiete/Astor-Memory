"""test_dashboard_js.py — verify dashboard frontend assets exist + sanity-check structure.

Tests:
- All 3 static files exist and have non-zero size
- HTML contains required DOM IDs (recall-q, recall-run, recall-results, health-*)
- HTML contains 4 chart canvases (growth, peruser, importance + room for future)
- CSS contains all expected selectors (recall-form, btn-primary, etc.)
- JS contains key functions: fetchDashboard, runRecall, renderRecall, escapeHtml
- JS uses XSS-safe escapeHtml on user content (content, topic, query echo)
- HTML links CDN chart.js (no need to bundle)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DASHBOARD = ROOT / "astor_memory" / "dashboard"


def test_files_exist():
    for name in ["index.html", "style.css", "app.js"]:
        p = DASHBOARD / name
        assert p.exists(), f"missing {p}"
        assert p.stat().st_size > 0, f"empty {p}"


def test_html_dom_ids():
    html = (DASHBOARD / "index.html").read_text(encoding="utf-8")
    required = [
        'id="hero-users"', 'id="hero-facts"', 'id="hero-tomb"',
        'id="hero-high"', 'id="hero-trend"', 'id="kpi-hit"',
        'id="kpi-mrr"', 'id="kpi-p95"', 'id="variants-table"',
        'id="growth-chart"', 'id="peruser-chart"', 'id="importance-chart"',
        'id="keywords-list"', 'id="recent-facts"',
        'id="health-embed"', 'id="health-warn"', 'id="health-total"',
        # Step 4: recall debugger
        'id="recall-q"', 'id="recall-tier"', 'id="recall-user"',
        'id="recall-topk"', 'id="recall-run"', 'id="recall-results"',
        'id="refresh-btn"', 'id="cache-badge"', 'id="last-event"',
    ]
    missing = [r for r in required if r not in html]
    assert not missing, f"HTML missing IDs: {missing}"


def test_html_chart_canvases():
    html = (DASHBOARD / "index.html").read_text(encoding="utf-8")
    canvases = re.findall(r'<canvas id="([^"]+)"', html)
    assert len(canvases) >= 3, f"expected >=3 canvases, got {canvases}"


def test_html_uses_cdn_chartjs():
    html = (DASHBOARD / "index.html").read_text(encoding="utf-8")
    assert "chart.js" in html.lower(), "Chart.js CDN reference missing"
    assert "cdn.jsdelivr.net" in html, "Chart.js not from jsdelivr CDN"


def test_html_health_modal():
    """Health diagnosis modal + 3 clickable cards."""
    html = (DASHBOARD / "index.html").read_text(encoding="utf-8")
    assert 'id="health-modal"' in html, "modal element missing"
    assert 'id="modal-close"' in html, "modal close button missing"
    for k in ("health-card-embed", "health-card-warn", "health-card-total"):
        assert f'id="{k}"' in html, f"clickable card missing: {k}"


def test_css_selectors():
    css = (DASHBOARD / "style.css").read_text(encoding="utf-8")
    required = [
        ":root", ".hero", ".grid-2", ".grid-3", ".card",
        ".hero-card", ".kpi-row", ".kpi", ".variants-table",
        ".kw-chip", ".recent-facts", ".health-card",
        # Step 4
        ".recall-form", ".btn-primary", ".recall-results",
        ".recall-item", ".recall-error", ".recall-summary",
        "@media (max-width: 720px)",
    ]
    missing = [r for r in required if r not in css]
    assert not missing, f"CSS missing selectors: {missing}"


def test_css_theme_vars():
    css = (DASHBOARD / "style.css").read_text(encoding="utf-8")
    for v in ["--bg", "--panel", "--accent", "--border", "--text",
              "--text-dim", "--danger", "--good"]:
        assert v + ":" in css, f"missing CSS var {v}"


def test_js_functions():
    js = (DASHBOARD / "app.js").read_text(encoding="utf-8")
    for fn in ["fetchDashboard", "runRecall", "renderRecall",
               "renderGrowth", "renderPerUser", "renderImportance",
               "escapeHtml", "renderRecallError", "renderRecallHint",
               "showHealthModal", "renderDiagnosis", "closeHealthModal"]:
        assert fn in js, f"missing JS function {fn}"


def test_js_health_modal_wiring():
    """Modal click handlers are wired to all 3 health cards."""
    js = (DASHBOARD / "app.js").read_text(encoding="utf-8")
    assert "health-card-embed" in js
    assert "health-card-warn" in js
    assert "health-card-total" in js
    assert "/v1/health/diagnose" in js


def test_js_xss_safe():
    """escapeHtml must be used on user-controlled content (fact content, query echo)."""
    js = (DASHBOARD / "app.js").read_text(encoding="utf-8")
    # fact content render must wrap with escapeHtml
    assert "escapeHtml(f.content)" in js, "fact content not escaped"
    assert "escapeHtml(r.content" in js, "recall content not escaped"
    assert "escapeHtml(k.keyword)" in js, "keyword not escaped"
    assert "escapeHtml(q)" in js, "query echo not escaped"
    # renderRecallError must escape
    assert "escapeHtml(msg)" in js, "recall error msg not escaped"


def test_js_uses_dashboard_endpoint():
    js = (DASHBOARD / "app.js").read_text(encoding="utf-8")
    assert "/v1/dashboard" in js, "fetch URL missing /v1/dashboard"
    assert "/v1/read" in js, "recall fetch URL missing /v1/read"


def test_js_handles_cache_field():
    js = (DASHBOARD / "app.js").read_text(encoding="utf-8")
    assert "_cache" in js, "missing _cache field handling"


if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = []
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed.append(t.__name__)
    if failed:
        print(f"\n{len(failed)}/{len(tests)} tests failed: {failed}")
        sys.exit(1)
    print(f"\n{len(tests)}/{len(tests)} tests passed")
