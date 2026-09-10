/* Astor Dashboard frontend logic — fetch /v1/dashboard every 60s, render 6 sections */
(function () {
  'use strict';

  const REFRESH_MS = 60_000;
  // Optional ?astor_dir=... override; default uses server-side get_default_astor_dir().
  const params = new URLSearchParams(location.search);
  const ASTOR_DIR = params.get('astor_dir') || '';
  const API = '/v1/dashboard' + (ASTOR_DIR ? '?astor_dir=' + encodeURIComponent(ASTOR_DIR) : '');

  // chart.js instances we need to update
  let growthChart = null, peruserChart = null, importanceChart = null;

  // theme colors (must match style.css)
  const C = {
    accent: '#FF8C2E',
    accentDim: '#E66A0F',
    accentSoft: 'rgba(255, 140, 46, 0.18)',
    border: '#FFD9B8',
    textDim: '#3a2a1a',
    danger: '#ef4444',
    good: '#16a34a',
    warn: '#f59e0b',
    panel2: '#FFE8D0',
  };
  function rgba(hex, a) {
    const v = hex.replace('#', '');
    return `rgba(${parseInt(v.slice(0,2),16)},${parseInt(v.slice(2,4),16)},${parseInt(v.slice(4,6),16)},${a})`;
  }

  async function fetchDashboard() {
    try {
      const r = await fetch(API);
      if (!r.ok) {
        const t = await r.text();
        throw new Error('HTTP ' + r.status + ': ' + t.slice(0, 120));
      }
      const d = await r.json();
      render(d);
      return d;
    } catch (e) {
      console.error('dashboard fetch failed:', e);
      setError(e.message);
      return null;
    }
  }

  function setError(msg) {
    document.getElementById('cache-badge').textContent = 'ERROR';
    document.getElementById('cache-badge').className = 'cache-badge error';
    document.getElementById('last-event').textContent = 'fetch failed: ' + msg.slice(0, 60);
  }

  function fmt(n, digits) {
    if (n == null || isNaN(n)) return '—';
    return Number(n).toFixed(digits != null ? digits : 2);
  }

  function fmtInt(n) {
    if (n == null) return '—';
    return n.toLocaleString();
  }

  function fmtRelativeMin(min) {
    if (min == null) return '—';
    if (min < 1) return Math.round(min * 60) + 's ago';
    if (min < 60) return Math.round(min) + 'm ago';
    if (min < 1440) return Math.round(min / 60) + 'h ago';
    return Math.round(min / 1440) + 'd ago';
  }

  function render(d) {
    // cache badge
    const badge = document.getElementById('cache-badge');
    badge.textContent = 'cache: ' + (d._cache || '?');
    badge.className = 'cache-badge ' + (d._cache || '');

    // hero
    const h = d.hero || {};
    document.getElementById('hero-users').textContent = fmtInt(h.total_users);
    document.getElementById('hero-facts').textContent = fmtInt(h.active_facts);
    document.getElementById('hero-tomb').textContent = fmtInt(h.tombstoned);
    document.getElementById('hero-high').textContent = fmtInt(h.high_importance);
    const trend = h.trend_status || 'unknown';
    const trendEl = document.getElementById('hero-trend');
    trendEl.textContent = trend;
    trendEl.className = 'hero-value trend-value ' + trend;
    document.getElementById('last-event').textContent =
      'last event: ' + fmtRelativeMin(h.last_event_minutes_ago);

    // eval KPI
    const et = d.eval_trend || {};
    const win = et.window_30d_baselines || {};
    document.getElementById('kpi-hit').textContent = win.hit_rate ? fmt(win.hit_rate.mean) : '—';
    document.getElementById('kpi-mrr').textContent = win.mrr ? fmt(win.mrr.mean) : '—';
    document.getElementById('kpi-p95').textContent = win.p95_latency_ms ? Math.round(win.p95_latency_ms.mean) + 'ms' : '—';
    document.getElementById('kpi-runs').textContent = fmtInt(win.n || 0);

    // variants table
    const tbody = document.querySelector('#variants-table tbody');
    tbody.innerHTML = '';
    const variants = et.all_variants_last || {};
    Object.keys(variants).sort().forEach(name => {
      const v = variants[name];
      const tr = document.createElement('tr');
      tr.innerHTML = '<td>' + name + '</td>'
        + '<td class="hit">' + fmt(v.hit_rate) + '</td>'
        + '<td class="mrr">' + fmt(v.mrr) + '</td>'
        + '<td>' + (v.p95_ms != null ? Math.round(v.p95_ms) : '—') + '</td>';
      tbody.appendChild(tr);
    });

    // growth chart
    renderGrowth(d.growth_30d || {});

    // per-user chart
    renderPerUser(d.per_user || []);

    // importance chart
    renderImportance(d.importance_histogram || {});

    // keywords
    const kw = d.top_keywords || [];
    const kwList = document.getElementById('keywords-list');
    kwList.innerHTML = '';
    if (kw.length === 0) {
      kwList.innerHTML = '<span style="color:var(--text-dim);font-style:italic;">no keywords yet</span>';
    } else {
      kw.forEach(k => {
        const chip = document.createElement('span');
        chip.className = 'kw-chip';
        chip.innerHTML = '<strong>' + k.count + '</strong>' + escapeHtml(k.keyword);
        kwList.appendChild(chip);
      });
    }

    // recent facts
    const rf = d.recent_facts || [];
    const rfList = document.getElementById('recent-facts');
    rfList.innerHTML = '';
    if (rf.length === 0) {
      rfList.innerHTML = '<li style="color:var(--text-dim);font-style:italic;">no facts yet</li>';
    } else {
      rf.forEach(f => {
        const li = document.createElement('li');
        const meta = '#' + f.id + ' · imp ' + fmt(f.importance) + ' · ' + (f.ts || '').slice(0, 10);
        li.innerHTML = '<div class="fact-meta">' + escapeHtml(meta) + '</div>'
          + '<div class="fact-content">' + escapeHtml(f.content) + '</div>';
        rfList.appendChild(li);
      });
    }

    // health
    const h2 = d.health || {};
    const setHealth = (id, v) => {
      const el = document.getElementById(id);
      el.textContent = fmtInt(v);
      if (v > 0) el.classList.add('nonzero'); else el.classList.remove('nonzero');
    };
    setHealth('health-embed', h2.embedding_failed);
    setHealth('health-warn', h2.audit_warnings);
    setHealth('health-total', h2.audit_total);
  }

  function renderGrowth(g) {
    const labels = Object.keys(g);
    const data = labels.map(d => g[d]);
    const ctx = document.getElementById('growth-chart');
    if (growthChart) growthChart.destroy();
    growthChart = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: labels,
        datasets: [{
          label: 'facts promoted',
          data: data,
          backgroundColor: rgba(C.accent, 0.7),
          borderColor: C.accentDim,
          borderWidth: 1,
          borderRadius: 3,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          y: { beginAtZero: true, grid: { color: C.border }, ticks: { color: C.textDim, font: { size: 10 } } },
          x: { grid: { display: false }, ticks: { color: C.textDim, font: { size: 10 }, maxRotation: 0, autoSkipPadding: 8 } },
        },
      },
    });
  }

  function renderPerUser(users) {
    const top = users.slice(0, 10);
    const labels = top.map(u => u.user);
    const data = top.map(u => u.active);
    const ctx = document.getElementById('peruser-chart');
    if (peruserChart) peruserChart.destroy();
    peruserChart = new Chart(ctx, {
      type: 'bar',
      data: {
        labels: labels,
        datasets: [{
          label: 'active facts',
          data: data,
          backgroundColor: rgba(C.accent, 0.7),
          borderColor: C.accentDim,
          borderWidth: 1,
          borderRadius: 3,
        }],
      },
      options: {
        indexAxis: 'y',
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { beginAtZero: true, grid: { color: C.border }, ticks: { color: C.textDim, font: { size: 10 } } },
          y: { grid: { display: false }, ticks: { color: C.textDim, font: { size: 10 } } },
        },
      },
    });
  }

  function renderImportance(h) {
    const labels = Object.keys(h);
    const data = labels.map(k => h[k]);
    const colors = labels.map(k => {
      if (k.startsWith('critical')) return rgba(C.danger, 0.8);
      if (k.startsWith('high')) return rgba(C.accent, 0.8);
      if (k.startsWith('mid')) return rgba(C.accentDim, 0.6);
      return rgba(C.textDim, 0.5);
    });
    const ctx = document.getElementById('importance-chart');
    if (importanceChart) importanceChart.destroy();
    importanceChart = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: labels,
        datasets: [{
          data: data,
          backgroundColor: colors,
          borderColor: C.panel2,
          borderWidth: 2,
        }],
      },
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: {
          legend: { position: 'bottom', labels: { color: C.textDim, font: { size: 11 }, boxWidth: 12 } },
        },
      },
    });
  }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  // boot
  document.addEventListener('DOMContentLoaded', () => {
    fetchDashboard();
    setInterval(fetchDashboard, REFRESH_MS);
    document.getElementById('refresh-btn').addEventListener('click', () => fetchDashboard());

    // recall debugger wiring
    const recallQ = document.getElementById('recall-q');
    const recallBtn = document.getElementById('recall-run');
    recallBtn.addEventListener('click', runRecall);
    recallQ.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        runRecall();
      }
    });
  });

  async function runRecall() {
    const q = document.getElementById('recall-q').value.trim();
    if (!q) {
      renderRecallHint('Type a query first.');
      return;
    }
    const tier = document.getElementById('recall-tier').value;
    const user = document.getElementById('recall-user').value;
    const topK = parseInt(document.getElementById('recall-topk').value, 10);
    const btn = document.getElementById('recall-run');
    btn.disabled = true;
    btn.textContent = '...';

    const body = JSON.stringify({ query: q, tier: tier, user: user, top_k: topK });
    try {
      const r = await fetch('/v1/read', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: body,
      });
      if (!r.ok) {
        const t = await r.text();
        throw new Error('HTTP ' + r.status + ': ' + t.slice(0, 200));
      }
      const d = await r.json();
      renderRecall(d, q, tier, user, topK);
    } catch (e) {
      console.error('recall failed:', e);
      renderRecallError(e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Run';
    }
  }

  function renderRecallHint(msg) {
    document.getElementById('recall-results').innerHTML =
      '<div class="recall-hint">' + escapeHtml(msg) + '</div>';
  }

  function renderRecallError(msg) {
    document.getElementById('recall-results').innerHTML =
      '<div class="recall-error">' + escapeHtml(msg) + '</div>';
  }

  function renderRecall(d, q, tier, user, topK) {
    const results = d.results || [];
    const summary = '<div class="recall-summary">'
      + '<strong>' + escapeHtml(q) + '</strong>'
      + ' · tier=<code>' + escapeHtml(tier) + '</code>'
      + ' · user=<code>' + escapeHtml(user) + '</code>'
      + ' · top_k=<code>' + topK + '</code>'
      + ' · returned=<code>' + (d.count != null ? d.count : results.length) + '</code>'
      + ' · ' + (results.length ? results.length + ' results shown' : 'no results')
      + '</div>';
    let html = summary;
    if (results.length === 0) {
      html += '<div class="recall-hint">No matches — query too narrow, or fact not in this tier/user.</div>';
    } else {
      results.forEach((r, i) => {
        const sim = r.similarity != null ? r.similarity.toFixed(4) : '—';
        const meta = '#' + r.fact_id
          + ' · sim=' + sim
          + ' · imp=' + (r.importance != null ? r.importance.toFixed(2) : '—')
          + ' · conf=' + (r.confidence != null ? r.confidence.toFixed(2) : '—')
          + ' · ' + (r.score_kind || '—')
          + ' · ' + (r.topic || r.kind || '—');
        html += '<div class="recall-item">'
          + '<span class="rank">' + (i + 1) + '</span>'
          + '<span class="sim">' + sim + '</span>'
          + '<div class="meta">' + escapeHtml(meta) + '</div>'
          + '<div class="content">' + escapeHtml(r.content || '') + '</div>'
          + '</div>';
      });
    }
    document.getElementById('recall-results').innerHTML = html;
  }
})();
