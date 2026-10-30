// escapeHtml -- one HTML escaper for the whole file.
//
// It used to live inside the dashboard IIFE, while several of its
// callers (fetchPeers, renderPeerRows) sit outside that IIFE. The
// result was a ReferenceError at runtime: the peer panel caught the
// error and rendered it as "fetch peers failed: escapeHtml is not
// defined", which reads like a server problem rather than a scoping
// one -- and the peer list never rendered at all.
//
// It is defined at the top level so every scope can reach it.
function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// v1.17.0: passcode gate. The dashboard is reachable from the public
// internet, so every request carries a session token. The token lives
// in sessionStorage: a reload does not ask again, closing the tab does.
//
// fetch is wrapped rather than each call site being patched, because a
// call site that forgets the header is a silent data leak, and there
// are dozens of them.
(function () {
  const TOKEN_KEY = 'astor_admin_token';
  const HDR = 'X-Astor-Admin-Token';

  function getToken() {
    try { return sessionStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; }
  }
  function setToken(t) {
    try { t ? sessionStorage.setItem(TOKEN_KEY, t) : sessionStorage.removeItem(TOKEN_KEY); }
    catch (e) { /* private mode: the page still works for this load */ }
  }

  const gate = document.getElementById('auth-gate');
  const errBox = document.getElementById('auth-error');
  let gateShown = false;

  function showGate(msg) {
    if (!gate) return;
    gate.hidden = false;
    gateShown = true;
    if (errBox) {
      errBox.textContent = msg || '';
      errBox.hidden = !msg;
    }
    const input = document.getElementById('auth-passcode');
    if (input) setTimeout(() => input.focus(), 30);
  }
  function hideGate() {
    if (!gate) return;
    gate.hidden = true;
    gateShown = false;
  }
  window.__astorShowGate = showGate;

  async function submitPasscode(code) {
    const res = await fetch('/v1/auth/passcode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ passcode: code }),
    });
    let body = {};
    try { body = await res.json(); } catch (e) { /* non-JSON error */ }
    if (!res.ok) {
      showGate(body.detail || body.error || 'that passcode is not right');
      return false;
    }
    setToken(body.token);
    hideGate();
    location.reload();
    return true;
  }

  document.addEventListener('DOMContentLoaded', () => {
    const btn = document.getElementById('auth-submit');
    const input = document.getElementById('auth-passcode');
    if (btn) btn.addEventListener('click', () => {
      submitPasscode((input && input.value) || '');
    });
    if (input) input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') submitPasscode(input.value || '');
    });
  });

  const origFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    // The auth endpoints themselves must not carry a stale token, and
    // the page load must not be blocked by our own gate.
    const isAuth = url.indexOf('/v1/auth/') !== -1;
    if (!isAuth && getToken()) {
      init = init || {};
      init.headers = Object.assign({}, init.headers || {}, { [HDR]: getToken() });
      if (typeof input !== 'string' && input && input.headers) {
        try { input.headers.set(HDR, getToken()); } catch (e) { /* ignore */ }
      }
    }
    return origFetch(input, init).then((res) => {
      if (res.status === 401) {
        setToken('');
        showGate('Enter the passcode to continue');
      }
      return res;
    });
  };
})();

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
      // S20 (2026-09-25): show server version in header so operator sees ship
      // bumps immediately. /v1/health is fast + has version field (no cache layer).
      fetchHealth().catch(() => {});
      return d;
    } catch (e) {
      console.error('dashboard fetch failed:', e);
      setError(e.message);
      return null;
    }
  }

  // S20: best-effort version pull. Failure → leave badge as "v…".
  async function fetchHealth() {
    try {
      const r = await fetch('/v1/health');
      if (!r.ok) return;
      const h = await r.json();
      const el = document.getElementById('version-badge');
      if (el && h.version) {
        el.textContent = 'v' + h.version;
        el.title = 'astor-memory v' + h.version + ' (' + (h.status || 'unknown') + ')';
      }
    } catch (e) {
      // silent
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
    const _sub = document.getElementById('hero-users-sub');
    if (_sub) {
      const bu = h.bound_users;
      _sub.textContent = bu != null ? (bu + ' bound via bindings') : '';
      _sub.title = 'Users with active bot-binding (may have 0 facts yet — freshly registered integrations)';
    }
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

    // v1.14.37 Ship N: Recent Capture panel — grouped facts by kind/tier/platform/all
        const rc = d.recent_capture || { by_kind: {}, by_tier: {}, by_platform: {}, counts: {} };
        // v1.16.43: show 5 rows per bucket, paginate
        const RC_PAGE_SIZE = 5;
        let _rcPage = 0;
        let _rcAxis = 'kind';
        function _renderRcPage(rc) {
          const body = document.getElementById('recent-capture-body');
          if (!body) return;
          body.innerHTML = '';
          let entries;
          if (_rcAxis === 'kind') {
            entries = Object.entries(rc.by_kind || {}).sort((a,b) => b[1].length - a[1].length);
          } else if (_rcAxis === 'tier') {
            entries = Object.entries(rc.by_tier || {}).sort((a,b) => b[1].length - a[1].length);
          } else if (_rcAxis === 'platform') {
            entries = Object.entries(rc.by_platform || {}).sort((a,b) => b[1].length - a[1].length);
          } else {
            const seen = new Set();
            const flat = [];
            for (const bucket of Object.values(rc.by_kind || {})) {
              for (const f of bucket) {
                if (!seen.has(f.id)) { seen.add(f.id); flat.push(f); }
              }
            }
            flat.sort((a,b) => (b.ts || '').localeCompare(a.ts || ''));
            entries = [['all facts', flat]];
          }
          if (entries.length === 0 || entries.every(([,v]) => v.length === 0)) {
            body.innerHTML = '<div class="rc-empty">No facts captured yet in this view.</div>';
            return;
          }
          // Flatten rows across all buckets in the current axis so user can page
          // through a single chronological list regardless of which bucket they
          // belong to. Each bucket gets its own header in the flattened slice.
          const flatRows = [];
          for (const [bucketName, rows] of entries) {
            if (rows.length === 0) continue;
            for (const f of rows) flatRows.push({ bucket: bucketName, fact: f });
          }
          const total = flatRows.length;
          const pageCount = Math.max(1, Math.ceil(total / RC_PAGE_SIZE));
          if (_rcPage >= pageCount) _rcPage = pageCount - 1;
          if (_rcPage < 0) _rcPage = 0;
          const slice = flatRows.slice(_rcPage * RC_PAGE_SIZE, (_rcPage + 1) * RC_PAGE_SIZE);
          // pager controls
          const pager = document.createElement('div');
          pager.className = 'rc-pager';
          const prev = document.createElement('button');
          prev.className = 'rc-page-btn';
          prev.textContent = '←';
          prev.disabled = (_rcPage === 0);
          prev.addEventListener('click', () => { _rcPage--; _renderRcPage(rc); });
          const next = document.createElement('button');
          next.className = 'rc-page-btn';
          next.textContent = '→';
          next.disabled = (_rcPage >= pageCount - 1);
          next.addEventListener('click', () => { _rcPage++; _renderRcPage(rc); });
          const counter = document.createElement('span');
          counter.className = 'rc-page-counter';
          counter.textContent = `${_rcPage + 1}/${pageCount} · ${total} total`;
          pager.appendChild(prev);
          pager.appendChild(counter);
          pager.appendChild(next);
          body.appendChild(pager);
          for (const { bucket, fact: f } of slice) {
            const li = document.createElement('li');
            li.className = 'rc-row';
            const tierTag = f.tier ? '<span class="rc-tag rc-tier">' + escapeHtml(f.tier) + '</span>' : '';
            const kindTag = f.kind ? '<span class="rc-tag rc-kind">' + escapeHtml(f.kind) + '</span>' : '';
            const platTag = f.platform ? '<span class="rc-tag rc-platform">' + escapeHtml(f.platform) + '</span>' : '';
            const meta = '#' + f.id + ' · imp ' + fmt(f.importance) + ' · ' + (f.ts || '').slice(0, 16);
            li.innerHTML = '<div class="rc-meta">'
              + tierTag + kindTag + platTag
              + '<span class="rc-meta-text">' + escapeHtml(meta) + '</span>'
              + '<span class="rc-bucket-inline">' + escapeHtml(bucket) + '</span>'
              + '</div>'
              + '<div class="rc-content">' + escapeHtml(f.content || '(empty)') + '</div>';
            const ul = document.createElement('ul');
            ul.className = 'rc-list';
            ul.appendChild(li);
            body.appendChild(ul);
          }
        }
        function _renderRecentCapture(axis) {
          _rcAxis = axis;
          _rcPage = 0;
          _renderRcPage(rc);
        }
        document.querySelectorAll('.rc-tab').forEach(btn => {
          btn.addEventListener('click', () => {
            document.querySelectorAll('.rc-tab').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            _renderRecentCapture(btn.dataset.axis);
          });
        });
        _renderRecentCapture('kind');


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

    // v1.16.25: Connections panel — live bindings to astor
    fetchConnections();
  }

  async function fetchConnections() {
    try {
      const r = await fetch('/v1/binding/list');
      if (!r.ok) return;
      const d = await r.json();
      renderConnections(d);
    } catch (e) {
      // silent — non-critical
    }
  }

  // v1.16.27 Connections panel — uses module-level state to avoid any
// function re-definition issues across page clicks.
let _connPage = 0;
let _connSorted = [];
// v1.17.8: the whole payload, so the panel-header switch can read the
// caller's own setting without a second request.
let _connPayload = null;

function renderConnections(d) {
  const tbody = document.querySelector('#connections-table tbody');
  if (!tbody) return;
  _connPayload = d;
  const bindings = d.bindings || [];
    // Sort: plan priority (admin > power > vip > free) then user_id.
    // v1.16.70: admin plan ranks 0 so the operator row surfaces at the top.
    const PLAN_RANK = { admin: 0, power: 1, vip: 2, free: 3 };
    _connSorted = bindings.slice().sort((a, b) => {
      const r = (PLAN_RANK[a.subscription_plan] != null ? PLAN_RANK[a.subscription_plan] : 9)
              - (PLAN_RANK[b.subscription_plan] != null ? PLAN_RANK[b.subscription_plan] : 9);
      if (r !== 0) return r;
      // Within the same plan tier: operator first, then alphabetical.
      const aOp = a.user_id === 'admin' ? 0 : 1;
      const bOp = b.user_id === 'admin' ? 0 : 1;
      if (aOp !== bOp) return aOp - bOp;
      return (a.user_id || '').localeCompare(b.user_id || '');
  });
  _drawConnPage();
}

function _drawConnPage() {
  // v1.17.8: the panel-header switch shows the operator's OWN
  // automatic mining. The per-row column answers a different
  // question -- who else has agreed to be mined -- and the operator
  // does not have to find their own row in a paginated table to
  // read their own switch.
  const _selfBtn = document.getElementById('auto-distill-toggle');
  if (_selfBtn) {
    const _on = !!(_connPayload && _connPayload.self_distill_opt_in);
    _selfBtn.textContent = 'Auto-distill: ' + (_on ? 'ON' : 'off');
    _selfBtn.classList.toggle('peer-allow-on', _on);
    _selfBtn.classList.toggle('peer-allow-off', !_on);
    _selfBtn.dataset.uid = (_connPayload && _connPayload.self_user_id) || '';
  }
  const tbody = document.querySelector('#connections-table tbody');
  const summary = document.getElementById('connections-summary');
  if (!tbody) return;
  const PAGE_SIZE = 10;
  const total = _connSorted.length;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  if (_connPage >= pageCount) _connPage = pageCount - 1;
  if (_connPage < 0) _connPage = 0;
  const slice = _connSorted.slice(_connPage * PAGE_SIZE, (_connPage + 1) * PAGE_SIZE);
  tbody.innerHTML = '';
  for (const b of slice) {
    const tr = document.createElement('tr');
    const roleClass = b.role === 'admin' ? 'role-admin' : 'role-user';
    tr.innerHTML = '<td>' + escapeHtml(b.platform_id || '—') + '</td>'
      + '<td>' + escapeHtml(b.user_id || '—') + '</td>'
      + '<td class="' + roleClass + '">' + escapeHtml(b.role || '—') + '</td>'
      // v1.16.79: 'admin' is an owner identity, not a subscription tier, so
      // showing it in the Plan column reads as though the operator is on some
      // plan they could be compared against other users on. It still SORTS by
      // PLAN_RANK above (admin first) — only the cell changes.
      //
      // v1.17.3: the check was on subscription_plan === 'admin', but the
      // column that decides who is the owner is role. An admin row whose
      // plan is 'free' rendered as "Plan: free", which says the operator
      // is on the cheapest tier — the opposite of what the ACL means.
      // role === 'admin' is the one that counts.
      + '<td>' + escapeHtml(
            b.role === 'admin' ? 'n/a'
                                : (b.subscription_plan || '—')) + '</td>'
      + '<td>' + escapeHtml(b.default_tier || '—') + '</td>'
      + '<td>' + escapeHtml(b.scope || '—') + '</td>'
      + '<td>' + escapeHtml((b.bound_at || '').slice(0, 16) || '—') + '</td>'
      // v1.17.5: the mining consent switch, per user. ON by default --
      // a user who has not opted out of sharing has their methods
      // promoted into the shared tier. The operator turns it off to
      // withdraw. Reuses the peer toggle's styling; the wording is
      // deliberately not "opt in" because the default IS in.
      + '<td><button class="peer-allow-toggle ' + _distillCls(b.distill_opt_in)
      + '" data-uid="' + escapeHtml(b.user_id || '') + '" '
      + 'data-action="toggle-distill">'
      + _distillText(b.distill_opt_in) + '</button></td>';
    tbody.appendChild(tr);
  }
  if (summary) {
    const platCount = new Set(_connSorted.map(b => (b.platform_id || '').split(':')[0])).size;
    const pageLabel = pageCount > 1 ? ' · page ' + (_connPage + 1) + '/' + pageCount : '';
    summary.innerHTML = total + ' active binding' + (total === 1 ? '' : 's')
      + ' across ' + platCount + ' platform' + (platCount === 1 ? '' : 's')
      + pageLabel + ' '
      + '<button class="conn-page-btn" id="conn-prev" type="button" '
      +   (_connPage <= 0 ? 'disabled' : '') + '>‹</button>'
      + '<button class="conn-page-btn" id="conn-next" type="button" '
      +   (_connPage >= pageCount - 1 ? 'disabled' : '') + '>›</button>';
    document.getElementById('conn-prev').onclick = () => { _connPage--; _drawConnPage(); };
    document.getElementById('conn-next').onclick = () => { _connPage++; _drawConnPage(); };
  }
}

  // v1.17.5: the mining consent switch has three states, and the
  // operator needs to see them apart. "auto" is the absence of a
  // decision -- for the operator's own tier it means the consent that
  // running the mining command carries; for anyone else it means their
  // tier is not mined. Showing it as "off" would claim a decision the
  // operator never made.
  function _distillText(v) {
    return v ? 'ON' : 'off';
  }
  function _distillCls(v) {
    return v ? 'peer-allow-on' : 'peer-allow-off';
  }

  // v1.17.5: the mining consent switch. One click, no confirm: the
  // flag is what decides whether this user's private methods reach
  // the shared tier, and it is reversible in the same place.
  async function onConnDistillToggle(ev) {
    const btn = ev.target.closest('button[data-action="toggle-distill"]');
    if (!btn) return;
    const action = btn.dataset.action;
    const uid = btn.dataset.uid;
    if (action === 'toggle-distill' && uid) {
    // ON -> off -> ON. One switch: on means "run automatically",
    // off means "only when someone asks".
    const cur = btn.textContent.trim();
    const want = cur === 'ON' ? false : true;
      btn.disabled = true;
      try {
        const r = await fetch('/v1/admin/user/' + encodeURIComponent(uid)
          + '/toggle_distill', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ allow_distill: want }),
        });
        if (!r.ok) {
          const d = await r.json().catch(() => ({}));
          alert('Toggle failed: ' + (d.error || r.status));
          return;
        }
        await fetchConnections();
      } catch (e) {
        alert('Toggle failed: ' + e);
      } finally {
        btn.disabled = false;
      }
    }
  }
  const _connTbody = document.querySelector('#connections-table tbody');
  if (_connTbody) {
    _connTbody.addEventListener('click', onConnDistillToggle);
  }

  // v1.17.8: the panel-header switch. It is the operator's own
  // automatic mining: on means the weekly job runs, off means it does
  // not. The manual command is unaffected either way -- the operator
  // said "turn off automatic and it becomes manual".
  const _autoBtn = document.getElementById('auto-distill-toggle');
  if (_autoBtn) {
    _autoBtn.addEventListener('click', async () => {
      const uid = _autoBtn.dataset.uid
        || (_connPayload && _connPayload.self_user_id) || 'admin';
      const on = _autoBtn.textContent.indexOf('ON') !== -1;
      _autoBtn.disabled = true;
      try {
        const r = await fetch('/v1/admin/user/' + encodeURIComponent(uid)
          + '/toggle_distill', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ allow_distill: !on }),
        });
        if (!r.ok) {
          const d = await r.json().catch(() => ({}));
          alert('Toggle failed: ' + (d.error || r.status));
          return;
        }
        await fetchConnections();
      } catch (e) {
        alert('Toggle failed: ' + e);
      } finally {
        _autoBtn.disabled = false;
      }
    });
  }

  // -- Health diagnosis modal ------------------------------------------------
  // Click any health card → fetch /v1/health/diagnose → render modal.

  async function showHealthModal(kind) {
    const modal = document.getElementById('health-modal');
    const body = document.getElementById('modal-body');
    const title = document.getElementById('modal-title');
    title.textContent = 'Health Diagnosis: ' + ({
      embed: 'Embedding Failed',
      warn: 'Audit Warnings',
      total: 'Audit Log Total',
    }[kind] || kind);
    body.innerHTML = 'loading…';
    modal.hidden = false;

    const user = (document.getElementById('recall-user') || {}).value || 'admin';
    const url = '/v1/health/diagnose?user=' + encodeURIComponent(user)
      + (ASTOR_DIR ? '&astor_dir=' + encodeURIComponent(ASTOR_DIR) : '');

    try {
      const r = await fetch(url);
      const d = await r.json();
      if (d.error) {
        body.innerHTML = '<div class="recall-error">' + escapeHtml(d.error) + (d.detail ? ': ' + escapeHtml(d.detail) : '') + '</div>';
        return;
      }
      body.innerHTML = renderDiagnosis(d, kind);
    } catch (e) {
      body.innerHTML = '<div class="recall-error">' + escapeHtml(e.message) + '</div>';
    }
  }

  function renderDiagnosis(d, kind) {
    let html = '';
    const emb = d.embedding_failed || {};
    const warn = d.warnings || {};
    const sev = d.audit_total_by_severity || {};

    // Embedding section
    html += '<div class="dx-section"><h3>Embedding Failed</h3>';
    html += '<div>total: <span class="dx-tag ' + (emb.total > 0 ? 'warn' : 'ok') + '">' + (emb.total || 0) + '</span>';
    if (emb.no_replay_queued === 0 && emb.total > 0) {
      html += ' <span class="dx-tag ok">all queued for replay</span>';
    } else if (emb.no_replay_queued > 0) {
      html += ' <span class="dx-tag warn">' + emb.no_replay_queued + ' NOT queued</span>';
    }
    html += '</div>';
    if (emb.errors && emb.errors.length) {
      html += '<div style="margin-top:6px"><b>Top errors:</b></div>';
      emb.errors.slice(0, 5).forEach(e => {
        html += '<div class="dx-row"><span class="dx-tag">' + e[1] + '</span> ' + escapeHtml(e[0].slice(0, 110)) + '</div>';
      });
    }
    if (emb.by_day && emb.by_day.length) {
      html += '<div style="margin-top:6px"><b>Daily trend:</b></div>';
      const max = Math.max.apply(null, emb.by_day.map(x => x[1]));
      emb.by_day.slice(0, 10).forEach(x => {
        const w = Math.round(x[1] / max * 60);
        html += '<div class="dx-row">' + x[0] + ' &nbsp;<span class="dx-tag">' + x[1] + '</span><span class="dx-day-bar" style="width:' + w + 'px"></span></div>';
      });
    }
    html += '</div>';

    // Warnings section
    html += '<div class="dx-section"><h3>Audit Warnings</h3>';
    html += '<div>total: <span class="dx-tag ' + (warn.total > 0 ? 'warn' : 'ok') + '">' + (warn.total || 0) + '</span></div>';
    if (warn.by_event && warn.by_event.length) {
      html += '<div style="margin-top:6px"><b>By event type:</b></div>';
      warn.by_event.forEach(e => {
        html += '<div class="dx-row"><span class="dx-tag">' + e[1] + '</span> ' + escapeHtml(e[0]) + '</div>';
      });
    }
    if (warn.samples && warn.samples.length) {
      html += '<div style="margin-top:6px"><b>Recent samples:</b></div>';
      warn.samples.slice(0, 5).forEach(s => {
        html += '<div class="dx-row">' + s[1].slice(0, 10) + ' &nbsp;<span class="dx-tag">' + escapeHtml(s[2]) + '</span> fact #' + s[3] + '<br>' + escapeHtml((s[4] || '').slice(0, 100)) + '</div>';
      });
    }
    html += '</div>';

    // Audit total by severity
    html += '<div class="dx-section"><h3>Audit Log (by severity)</h3>';
    Object.keys(sev).forEach(k => {
      html += '<div class="dx-row"><span class="dx-tag ' + (k === 'warning' ? 'warn' : 'ok') + '">' + k + '</span> ' + fmtInt(sev[k]) + '</div>';
    });
    html += '</div>';

    html += '<div style="margin-top:8px;color:var(--text-dim);font-size:11px;">user: ' + escapeHtml(d.user) + '</div>';
  return html;
  }

  function closeHealthModal() {
    document.getElementById('health-modal').hidden = true;
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

    // health diagnosis modal wiring
    document.getElementById('health-card-embed').addEventListener('click', () => showHealthModal('embed'));
    document.getElementById('health-card-warn').addEventListener('click', () => showHealthModal('warn'));
    document.getElementById('health-card-total').addEventListener('click', () => showHealthModal('total'));
    document.getElementById('modal-close').addEventListener('click', closeHealthModal);
    document.getElementById('health-modal').addEventListener('click', e => {
      if (e.target.id === 'health-modal') closeHealthModal();
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

// =====================================================================
// v1.15.19 Ship C: Peer Friends panel (PPS Phase 4 dashboard)
// CRUD against /v1/peer/* endpoints. Trust editable inline; allow-search
// toggle is a one-click switch; blacklist = drop-down + reason prompt.
// =====================================================================
// The last connection test per peer, kept here so it survives the
// panel reload that follows a click. Keyed by peer id.
const peerTestResults = {};

async function fetchPeers() {
  try {
    // v1.16.7: also fetch self peer_id for header + empty-state copy button
    const [pr, ir, qr, fr] = await Promise.all([
      fetch('/v1/peer/list'),
      fetch('/v1/identity').catch(() => null),
      // v1.16.51: quarantine is security-critical and had NO UI before.
      // A quarantined peer neither gossips nor is recalled from, so the
      // operator must be able to see and approve pending peers.
      fetch('/v1/peer/quarantine/list').catch(() => null),
      // v1.16.83: inbound friend requests — the prompt the operator is
      // asked about. Silently 404s on older servers.
      fetch('/v1/peer/friend-requests').catch(() => null),
    ]);
    let selfPeerId = null;
    if (ir && ir.ok) {
          const id = await ir.json();
          selfPeerId = id.peer_id || null;
          const el = document.getElementById('self-peer-id');
          if (el && selfPeerId) {
            // v1.16.43: show full peer_id (not truncated) + always include
            // a manual select fallback so users without clipboard API access
            // (HTTP origin in some browsers) can still copy.
            el.textContent = selfPeerId;
            el.title = 'Click to copy — full peer_id: ' + selfPeerId;
            el.style.cursor = 'pointer';
            el.style.userSelect = 'all';
            el.onclick = () => copyPeerIdToClipboard(selfPeerId);
            el.ondblclick = () => selectEl(el);
          }
          // v1.16.83: show the friend code — one string the other person
          // pastes into their dashboard. It carries the address along with
          // the identity, so adding someone is one paste and not a pair of
          // fields to transcribe by hand.
          const fc = document.getElementById('self-friend-code');
          if (fc) {
            const code = id.friend_code || selfPeerId;
            fc.textContent = 'code: ' + code;
            fc.title = 'Click to copy — send this to someone so they can add ' +
              'you with one paste. It is your peer_id plus the address other ' +
              'peers should send to.';
            fc.style.cursor = 'pointer';
            fc.style.userSelect = 'all';
            fc.onclick = () => copyPeerIdToClipboard(code);
            fc.ondblclick = () => selectEl(fc);
          }
        }
    if (!pr.ok) throw new Error('HTTP ' + pr.status);
    const d = await pr.json();
    renderPeerPanel(d.peers || [], selfPeerId);
    // v1.16.51: render quarantine strip (silently skipped if endpoint 404s)
    if (qr && qr.ok) {
      try { renderPeerQuarantine(await qr.json()); } catch (_) {}
    }
    // v1.16.83: render friend-requests prompt
    if (fr && fr.ok) {
      try { renderPeerFriendRequests(await fr.json()); } catch (_) {}
    }
    return d;
  } catch (e) {
    document.getElementById('peer-panel-body').innerHTML =
      '<div class="recall-error">fetch peers failed: ' + escapeHtml(e.message) + '</div>';
  }
}

// ======================================================================
// v1.16.51: peer quarantine — pending peers awaiting operator approval.
// API: GET /v1/peer/quarantine/list, POST /v1/peer/unquarantine
//   body: {peer_id: "astor:<32-hex>", restore_trust: bool}
// A quarantined peer is neither recalled from nor sent gossip, so the
// only way it becomes active is this panel.
// ======================================================================
// v1.16.83: the friend-requests prompt. Each row is one signed request —
// the alias and note were covered by the sender's signature, so what is
// displayed here is exactly what the sender vouched for.
async function renderPeerFriendRequests(payload) {
  const strip = document.getElementById('peer-friend-requests');
  const body = document.getElementById('peer-friend-requests-body');
  const rows = (payload && payload.requests) || [];
  const pending = rows.filter(r => r.status === 'pending');
  if (!pending.length) {
    strip.hidden = true;
    if (body) body.innerHTML = '';
    return;
  }
  strip.hidden = false;
  const count = document.getElementById('peer-fr-count');
  if (count) count.textContent = String(pending.length);
  body.innerHTML = '';
  for (const r of pending) {
    const row = document.createElement('div');
    row.className = 'peer-fr-row';
    row.dataset.id = r.id;

    const who = document.createElement('span');
    who.className = 'peer-fr-who';
    who.textContent = r.alias || r.from_peer_id;
    who.title = r.from_peer_id;

    const note = document.createElement('span');
    note.className = 'peer-fr-note';
    note.textContent = r.note ? ('— ' + r.note) : '';
    note.title = r.note || '';

    const since = document.createElement('span');
    since.className = 'peer-fr-since';
    since.textContent = 'since ' + (r.received_at || '?');

    const ok = document.createElement('button');
    ok.className = 'peer-fr-accept';
    ok.textContent = 'Accept';
    ok.onclick = () => resolveFriendRequest(r.id, true);

    const no = document.createElement('button');
    no.className = 'peer-fr-reject';
    no.textContent = 'Reject';
    no.onclick = () => resolveFriendRequest(r.id, false);

    row.append(who, note, since, ok, no);
    body.appendChild(row);
  }
}

async function resolveFriendRequest(id, accept) {
  const res = await fetch('/v1/peer/friend-requests/' + id + '/resolve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ accept: accept, trust: 60 }),
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok || !d.ok) {
    alert('Could not ' + (accept ? 'accept' : 'reject') + ' request #' +
      id + ': ' + (d.error || ('HTTP ' + res.status)));
    return;
  }
  fetchPeers();  // refresh — the row disappears once resolved
}

async function renderPeerQuarantine(payload) {
  const strip = document.getElementById('peer-quarantine-strip');
  const body = document.getElementById('peer-quarantine-body');
  const count = document.getElementById('peer-q-count');
  if (!strip || !body) return;
  const peers = (payload && payload.peers) || [];
  if (!peers.length) { strip.hidden = true; return; }
  strip.hidden = false;
  count.textContent = String(peers.length);

  // The table sits inside its own scroller: on a narrow screen the
  // columns would otherwise widen the whole page.
  let html = '<div class="table-scroll"><table class="peer-table"><thead><tr>'
    + '<th>peer_id</th><th>alias</th><th>trust</th>'
    + '<th>endpoint</th><th>seen</th><th>action</th>'
    + '</tr></thead><tbody>';
  for (const p of peers) {
    const pid = p.peer_id || '';
    const shortId = pid.length > 20 ? pid.slice(0, 10) + '\u2026' + pid.slice(-6) : pid;
    const ep = p.endpoint ? escapeHtml(p.endpoint) : '<span style="opacity:.55">\u2014</span>';
    const alias = p.alias ? escapeHtml(p.alias) : '<span style="opacity:.55">(none)</span>';
    const seen = p.updated_at ? escapeHtml(String(p.updated_at).slice(0, 19)) : '\u2014';
    html += '<tr>'
      + '<td><code title="' + escapeHtml(pid) + '">' + escapeHtml(shortId) + '</code></td>'
      + '<td>' + alias + '</td>'
      + '<td>' + (p.trust ?? 0) + '</td>'
      + '<td style="font-size:11px">' + ep + '</td>'
      + '<td style="font-size:11px">' + seen + '</td>'
      + '<td><button class="btn-primary btn-sm" data-q-approve="' + escapeHtml(pid) + '">Approve</button> '
      + '<button class="btn-ghost btn-sm" data-q-block="' + escapeHtml(pid) + '">Block</button></td>'
      + '</tr>';
  }
  html += '</tbody></table></div>';
  body.innerHTML = html;

  body.querySelectorAll('[data-q-approve]').forEach(btn => {
    btn.onclick = () => unquarantinePeer(btn.getAttribute('data-q-approve'), true, btn);
  });
  body.querySelectorAll('[data-q-block]').forEach(btn => {
    btn.onclick = () => unquarantinePeer(btn.getAttribute('data-q-block'), false, btn);
  });
}

async function unquarantinePeer(pid, approve, btn) {
  if (!pid) return;
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = '...';
  try {
    const res = await fetch('/v1/peer/unquarantine', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({peer_id: pid, restore_trust: approve}),
    });
    if (!res.ok) {
      const e = await res.json().catch(() => ({}));
      throw new Error(e.error || ('HTTP ' + res.status));
    }
    btn.textContent = approve ? 'approved' : 'blocked';
    await fetchPeers();
  } catch (e) {
    btn.disabled = false;
    btn.textContent = label;
    alert('Quarantine action failed: ' + e.message);
  }
}

// v1.16.7: helper — copy peer_id to clipboard with brief visual feedback
async function copyPeerIdToClipboard(peerId) {
  if (!peerId) return;
  let copied = false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(peerId);
      copied = true;
    } else {
      // fallback: temporary textarea + execCommand
      const ta = document.createElement('textarea');
      ta.value = peerId;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      copied = document.execCommand('copy');
      document.body.removeChild(ta);
    }
  } catch (e) {
    copied = false;
  }
  if (copied) {
    const el = document.getElementById('self-peer-id');
    if (el) {
      const orig = el.textContent;
      el.textContent = '✓ copied!';
      setTimeout(() => { el.textContent = orig; }, 1200);
    }
  } else {
    // Fallback: select the text + alert so user can Ctrl+C manually
    const el = document.getElementById('self-peer-id');
    if (el) {
      selectEl(el);
    }
    alert('peer_id: ' + peerId + '\n\n(copy failed — already selected, press Ctrl+C)');
  }
}

function selectEl(el) {
  try {
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  } catch (e) {}
}

function renderPeerPanel(peers, selfPeerId) {
  const body = document.getElementById('peer-panel-body');
  if (peers.length === 0) {
    // v1.16.7: show the empty-state template (more helpful than raw hint)
    const tmpl = document.getElementById('peer-empty-template');
    if (tmpl) {
      const node = tmpl.content.cloneNode(true);
      body.innerHTML = '';
      body.appendChild(node);
      const copyBtn = document.getElementById('peer-self-copy-btn');
      if (copyBtn && selfPeerId) {
        copyBtn.addEventListener('click', () => copyPeerIdToClipboard(selfPeerId));
      }
      return;
    }
    body.innerHTML = '<div class="recall-hint">No peers yet. Add one above to start building your PPS network.</div>';
    return;
  }
  // split kinds visually
  const byKind = (k) => peers.filter(p => (p.kind || 'friend') === k);
  const friendPeers = byKind('friend');
  const blacklistPeers = byKind('blacklist');
  const otherPeers = peers.filter(p => !['friend','blacklist'].includes(p.kind));
  let html = '';
  if (friendPeers.length > 0) {
    html += '<div class="peer-section-label">Peers</div>';
    html += renderPeerRows(friendPeers, 'friends');
  }
  if (otherPeers.length > 0) {
    html += '<div class="peer-section-label">Other</div>';
    html += renderPeerRows(otherPeers, 'other');
  }
  if (blacklistPeers.length > 0) {
    html += '<div class="peer-section-label peer-blacklist">Blacklisted</div>';
    html += renderPeerRows(blacklistPeers, 'blacklist');
  }
  body.innerHTML = html;
  // bind row actions
  body.querySelectorAll('[data-action]').forEach(btn => {
    btn.addEventListener('click', onPeerAction);
  });
  body.querySelectorAll('input.peer-trust-input').forEach(inp => {
    inp.addEventListener('change', onPeerTrustEdit);
  });
  // v1.16.27: peer pager buttons
  body.querySelectorAll('[data-peer-page]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const dir = e.currentTarget.getAttribute('data-peer-page');
      const sec = e.currentTarget.getAttribute('data-section');
      const totalForSec = (sec === 'friends' ? friendPeers
        : sec === 'blacklist' ? blacklistPeers : otherPeers).length;
      const pageCount = Math.ceil(totalForSec / 10);
      const cur = _peer_page[sec] || 0;
      if (dir === 'prev' && cur > 0) _peer_page[sec] = cur - 1;
      else if (dir === 'next' && cur < pageCount - 1) _peer_page[sec] = cur + 1;
      renderPeerPanel(peers, selfPeerId);
    });
  });
}

let _peer_page = {};   // v1.16.27: per-section page state

function renderPeerRows(rows, sectionKey) {
  const PAGE_SIZE = 10;
  const page = _peer_page[sectionKey] || 0;
  const total = rows.length;
  const slice = rows.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
  let html = '<table class="peer-table"><thead><tr>'
    + '<th>peer_id</th><th>alias</th><th>trust</th>'
    + '<th>endpoint</th><th>pubkey</th>'
    + '<th title="inbound: that peer may search my public tier">allow</th>'
    + '<th title="outbound: I may search that peer">outbound</th>'
    + '<th title="whether a local miss would actually reach this peer">search</th>'
    + '<th>updated</th>'
    + '<th title="run a real search against this peer and say where it stopped">test</th>'
    + '<th>actions</th>'
    + '</tr></thead><tbody>';
  slice.forEach(p => {
    const pid = encodeURIComponent(p.peer_id);
    const alias = p.alias || '';
    const trust = p.trust != null ? p.trust : '?';
    // v1.17.3: a peer can have several addresses -- the LAN one and the
    // public one. Show them all; the dispatcher tries them in order.
    const _eps = (p.endpoints && p.endpoints.length) ? p.endpoints
                  : (p.endpoint ? [p.endpoint] : []);
    const endpoint = _eps.length
      ? _eps.map(function (e) { return escapeHtml(e); }).join('<br>')
      : '<span class="recall-hint">— none —</span>';
    const hasPub = p.has_pubkey
      ? '<span class="rc-tag rc-kind">✓</span>'
      : '<span class="recall-hint">—</span>';
    const allowCls = p.allow_search ? 'peer-allow-on' : 'peer-allow-off';
    const allowTxt = p.allow_search ? 'ON' : 'off';
    // v1.17.3: the outbound mirror. Independent of allow_search -- that
    // one says "that peer may search me", this one says "I may search
    // that peer". A peer can read from me without me reading from it.
    const outCls = (p.outbound_search === false) ? 'peer-allow-off' : 'peer-allow-on';
    const outTxt = (p.outbound_search === false) ? 'off' : 'ON';
    const updated = p.updated_at ? p.updated_at.slice(0, 10) : '—';
    // Why a peer is not searchable from here. Trust and endpoint are the
    // local gates; the remote's own allow-search is the fourth gate and
    // only they can turn it on, so say so instead of showing a bare "no".
    const search = (p.kind === 'blacklist' || p.kind === 'quarantine')
      ? '<span class="recall-hint">blocked</span>'
      : (p.outbound_search === false
        ? '<span class="recall-hint">outbound off</span>'
        : (trust < 50
          ? '<span class="recall-hint">trust &lt; 50</span>'
          : (!p.endpoint
            ? '<span class="recall-hint">no endpoint</span>'
            : '<span class="rc-tag rc-kind">✓</span>')));
    // v1.17.22: one click instead of four gates checked by hand. The
    // test runs the real search path, so "the port answers" and "the
    // peer answers" do not get confused.
    const tRes = peerTestResults[p.peer_id];
    const tCell = (p.kind === 'blacklist' || p.kind === 'quarantine')
      ? '<span class="recall-hint">blocked</span>'
      : (tRes
        ? ('<span class="peer-test-result ' + (tRes.ok ? 'peer-test-ok' : 'peer-test-fail')
           + '" title="' + escapeHtml(tRes.detail || '') + '">'
           + (tRes.ok ? '✓ ' : '✗ ') + escapeHtml(tRes.stage)
           + (tRes.latency_ms ? ' ' + tRes.latency_ms + 'ms' : '') + '</span>')
        : '');
    html += '<tr>'
      + '<td class="peer-pid" title="' + escapeHtml(p.peer_id) + '">'
      + escapeHtml(p.peer_id.length > 22 ? p.peer_id.slice(0,22) + '…' : p.peer_id)
      + '</td>'
      + '<td class="peer-alias">' + escapeHtml(alias) + '</td>'
      + '<td class="peer-trust">'
      + '<input type="number" class="peer-trust-input" min="0" max="100" '
      + 'value="' + trust + '" data-pid="' + escapeHtml(p.peer_id) + '" />'
      + '</td>'
      + '<td class="peer-endpoint">' + endpoint + '</td>'
      + '<td class="peer-pub">' + hasPub + '</td>'
      + '<td><button class="peer-allow-toggle ' + allowCls
      + '" data-pid="' + escapeHtml(p.peer_id) + '" data-action="toggle-allow">'
      + allowTxt + '</button></td>'
      + '<td><button class="peer-allow-toggle ' + outCls
      + '" data-pid="' + escapeHtml(p.peer_id) + '" data-action="toggle-outbound">'
      + outTxt + '</button></td>'
      + '<td>' + search + '</td>'
      + '<td class="recall-hint">' + escapeHtml(updated) + '</td>'
      + '<td class="peer-test-cell">' + tCell
      + '<button data-pid="' + escapeHtml(p.peer_id) + '" data-action="test-peer" '
      + 'data-mode="direct" class="peer-test-btn">test</button>'
      + '<button data-pid="' + escapeHtml(p.peer_id) + '" data-action="test-peer" '
      + 'data-mode="relay" class="peer-test-btn peer-test-relay">via relay</button>'
      + '</td>'
      + '<td>'
      + (p.kind === 'blacklist'
        ? '<button data-pid="' + escapeHtml(p.peer_id) + '" data-action="unblacklist" class="btn-refresh">unblock</button>'
        : '<button data-pid="' + escapeHtml(p.peer_id) + '" data-action="blacklist" class="btn-refresh">block</button>')
      + '<button data-pid="' + escapeHtml(p.peer_id) + '" data-action="delete" class="btn-refresh">×</button>'
      + '</td>'
      + '</tr>';
  });
  html += '</tbody></table>';
  if (total > PAGE_SIZE) {
    const pageCount = Math.ceil(total / PAGE_SIZE);
    html += '<div class="peer-pager">'
      + '<span class="peer-pager-info">' + (page + 1) + '/' + pageCount + ' (' + total + ')</span>'
      + '<button class="conn-page-btn" data-peer-page="prev" data-section="' + sectionKey + '" '
      + (page <= 0 ? 'disabled' : '') + '>‹</button>'
      + '<button class="conn-page-btn" data-peer-page="next" data-section="' + sectionKey + '" '
      + (page >= pageCount - 1 ? 'disabled' : '') + '>›</button>'
      + '</div>';
  }
  return html;
}

async function onPeerAction(ev) {
  const btn = ev.currentTarget;
  const pid = btn.dataset.pid;
  const action = btn.dataset.action;
  if (!pid || !action) return;
  if (action === 'delete') {
    // The confirm lives here, and the DELETE happens here too. It used
    // to be the first branch of an if/else-if chain whose later
    // `else if (action === 'delete')` could never run -- the chain had
    // already matched -- so the button asked for confirmation and then
    // did nothing. The peer stayed.
    if (!confirm('Delete peer ' + pid + '?')) return;
    btn.disabled = true;
    const r = await fetch('/v1/peer/' + encodeURIComponent(pid), {method: 'DELETE'});
    btn.disabled = false;
    if (!r.ok) { alert('delete failed: HTTP ' + r.status); return; }
  } else if (action === 'blacklist') {
    const reason = prompt('Optional reason for blocking:', '');
    if (reason === null) return;
    btn.disabled = true;
    const r = await fetch('/v1/peer/' + encodeURIComponent(pid) + '/blacklist', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({reason: reason || null})
    });
    btn.disabled = false;
    if (!r.ok) { alert('block failed: HTTP ' + r.status); return; }
  } else if (action === 'unblacklist') {
    btn.disabled = true;
    const r = await fetch('/v1/peer/' + encodeURIComponent(pid) + '/unblacklist', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: '{}'
    });
    btn.disabled = false;
    if (!r.ok) { alert('unblock failed: HTTP ' + r.status); return; }
  } else if (action === 'toggle-allow') {
    const next = btn.textContent.trim() !== 'ON';
    btn.disabled = true;
    const r = await fetch('/v1/peer/' + encodeURIComponent(pid) + '/allow-search', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({allow: next})
    });
    btn.disabled = false;
    if (!r.ok) { alert('toggle failed: HTTP ' + r.status); return; }
  } else if (action === 'test-peer') {
    // v1.17.22: run the real search path and report where it stopped.
    // The stage is the useful part -- "connect" and "authorize" are
    // different problems with different fixes.
    const mode = btn.dataset.mode || 'direct';
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = '…';
    let res = {ok: false, stage: 'connect', detail: 'test failed'};
    try {
      const r = await fetch('/v1/peer/' + encodeURIComponent(pid) + '/test', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({mode: mode})
      });
      const body = await r.json().catch(() => ({}));
      if (body && body.stage) res = body;
      else if (!r.ok) res = {ok: false, stage: 'error',
                             detail: 'HTTP ' + r.status};
    } catch (e) {
      res = {ok: false, stage: 'error', detail: String(e)};
    }
    btn.disabled = false;
    btn.textContent = old;
    peerTestResults[pid] = res;
    await fetchPeers();
    return;
  } else if (action === 'toggle-outbound') {
    // v1.17.3: the outbound mirror of allow-search. allow = "that peer
    // may search me"; outbound = "I may search that peer".
    const next = btn.textContent.trim() !== 'ON';
    btn.disabled = true;
    const r = await fetch('/v1/peer/' + encodeURIComponent(pid) + '/outbound-search', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: next})
    });
    btn.disabled = false;
    if (!r.ok) { alert('toggle failed: HTTP ' + r.status); return; }
  }
  await fetchPeers();
}

async function onPeerTrustEdit(ev) {
  const inp = ev.currentTarget;
  const pid = inp.dataset.pid;
  const newVal = parseInt(inp.value, 10);
  if (isNaN(newVal) || newVal < 0 || newVal > 100) {
    alert('trust must be 0-100');
    inp.focus();
    return;
  }
  const r = await fetch('/v1/peer/' + encodeURIComponent(pid) + '/trust', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({trust: newVal})
  });
  if (!r.ok) {
    alert('trust update failed: HTTP ' + r.status);
    inp.focus();
  } else {
    inp.classList.add('peer-trust-saved');
    setTimeout(() => inp.classList.remove('peer-trust-saved'), 1200);
  }
}

// v1.16.83: a friend code is "<peer_id>[@host:port]" — the address rides
// along with the identity so one paste is enough. A bare peer_id is still
// accepted; it just cannot deliver a request.
function parseFriendCode(raw) {
  const text = (raw || '').trim();
  if (!text) return {error: 'paste a friend code'};
  const at = text.indexOf('@');
  const pid = (at >= 0 ? text.slice(0, at) : text).trim();
  const addr = at >= 0 ? text.slice(at + 1).trim() : '';
  if (!/^astor:[0-9a-f]{32}$/i.test(pid)) {
    return {error: 'not a friend code — expected astor:<32-hex>[@host:port]'};
  }
  if (at >= 0 && !addr) {
    return {error: 'the friend code has an empty address after @'};
  }
  return {peer_id: pid, endpoint: addr ? addr : null};
}

async function onPeerAdd() {
  const parsed = parseFriendCode(document.getElementById('peer-add-pid').value);
  if (parsed.error) {
    alert(parsed.error);
    return;
  }
  const pid = parsed.peer_id;
  const alias = document.getElementById('peer-add-alias').value.trim();
  const trustRaw = document.getElementById('peer-add-trust').value;
  const trust = parseInt(trustRaw, 10);
  if (isNaN(trust) || trust < 0 || trust > 100) {
    alert('trust must be 0-100');
    return;
  }
  const endpoint = parsed.endpoint || '';
  const note = document.getElementById('peer-add-note').value.trim();
  const body = {peer_id: pid, trust: trust};
  if (alias) body.alias = alias;
  // v1.16.83: the endpoint is what makes the peer reachable. Without it the
  // row is a local claim nobody can act on, which is why "added but nothing
  // happened" was the symptom — so it is stored, and reported when missing.
  if (endpoint) {
    body.endpoint = /^https?:\/\//i.test(endpoint)
      ? endpoint
      : 'http://' + endpoint;
  }
  const r = await fetch('/v1/peer/add', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  });
  if (!r.ok) {
    const t = await r.text();
    alert('add failed: HTTP ' + r.status + ' ' + t.slice(0, 160));
    return;
  }
  let sent = null;
  if (endpoint) {
    // Ask the other side to add us back. Their dashboard then shows the
    // prompt. A failure here is not fatal — the local record stands and
    // they can add us manually — but it must be visible, not silent.
    sent = await fetch('/v1/peer/friend-request', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        peer_id: pid, endpoint: endpoint, alias: alias, note: note,
      })
    });
  }
  document.getElementById('peer-add-pid').value = '';
  document.getElementById('peer-add-alias').value = '';
  document.getElementById('peer-add-note').value = '';
  document.getElementById('peer-add-trust').value = '60';
  await fetchPeers();
  if (!endpoint) {
    alert('Added locally, but their friend code had no address.\n\n' +
      'Ask them for the full code — it looks like\n' +
      'astor:<32-hex>@host:port — and they will get a prompt.');
  } else if (sent && !sent.ok) {
    const t = await sent.text().catch(() => '');
    alert('Peer added, but the friend request was NOT delivered: HTTP ' +
      sent.status + ' ' + t.slice(0, 200) + '\n\n' +
      'They will not see a prompt. Check that the endpoint is correct and ' +
      'their server is reachable.');
  } else {
    alert('Peer added and friend request sent.\n' +
      'They should see a prompt in their dashboard. If they do not, their ' +
      'server may not be reachable at that address.');
  }
}

document.getElementById('peer-add-btn').addEventListener('click', onPeerAdd);

// v1.17.1: identity card, open-search switch, add-by-URL.
// The identity card makes the peer id copyable instead of something the
// operator has to dig out of a log file. The switch is the control that
// decides whether other nodes may query this node's public tier at all,
// so it belongs in the dashboard rather than the CLI only. Add-by-URL
// removes the friend-code copy dance: a node publishes its own identity,
// so pointing at the URL is enough.
async function loadPeerIdentity() {
  const idEl = document.getElementById('peer-self-id');
  if (!idEl) return;
  try {
    const r = await fetch('/v1/peer/identity');
    if (!r.ok) { idEl.textContent = '(identity unavailable)'; return; }
    const j = await r.json();
    idEl.textContent = j.peer_id || '(no peer id)';
    idEl.dataset.peerId = j.peer_id || '';
    idEl.dataset.friendCode = j.friend_code || '';
  } catch (e) {
    idEl.textContent = '(identity unavailable)';
  }
}

async function loadOpenSearchState() {
  const box = document.getElementById('peer-open-search');
  const hint = document.getElementById('peer-open-search-hint');
  if (!box) return;
  try {
    const r = await fetch('/v1/peer/open-search');
    if (!r.ok) { if (hint) hint.textContent = 'status unavailable'; return; }
    const j = await r.json();
    const openTo = Array.isArray(j.open_to) ? j.open_to : [];
    box.checked = openTo.includes('*');
    if (hint) {
      hint.textContent = box.checked
        ? 'any signature-verified peer may query the public tier'
        : (openTo.length ? ('open to ' + openTo.length + ' peer(s)') : 'closed to everyone');
    }
  } catch (e) {
    if (hint) hint.textContent = 'status unavailable';
  }
}

async function onOpenSearchToggle() {
  const box = document.getElementById('peer-open-search');
  const hint = document.getElementById('peer-open-search-hint');
  if (!box) return;
  const want = box.checked;
  if (hint) hint.textContent = 'saving...';
  try {
    const r = await fetch('/v1/peer/open-search', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({allow: want, peer_id: '*'})
    });
    if (!r.ok) {
      const t = await r.text();
      box.checked = !want;
      if (hint) hint.textContent = 'failed: HTTP ' + r.status;
      alert('could not change the open-search setting: HTTP ' + r.status + ' ' + t.slice(0, 160));
      return;
    }
    await loadOpenSearchState();
  } catch (e) {
    box.checked = !want;
    if (hint) hint.textContent = 'failed';
  }
}

async function loadFanoutState() {
  const box = document.getElementById('peer-fanout');
  const hint = document.getElementById('peer-fanout-hint');
  if (!box) return;
  try {
    const r = await fetch('/v1/peer/fanout');
    if (!r.ok) { if (hint) hint.textContent = 'status unavailable'; return; }
    const j = await r.json();
    box.checked = !!j.enabled;
    if (!hint) return;
    // v1.17.3: the switch alone does not say whether the thing works.
    // Report what it actually did: how many peers it could have asked,
    // how many times it fired, and when it last fired.
    const peers = (j.eligible_peers === -1) ? '?' : j.eligible_peers;
    const fired = j.triggered || 0;
    const last = j.last_triggered_at
      ? String(j.last_triggered_at).replace('T', ' ').replace('Z', ' UTC')
      : 'never';
    let state;
    if (j.source === 'env') {
      state = 'forced by ASTOR_PEER_FANOUT_DEFAULT';
    } else if (!j.enabled) {
      state = 'local only — a miss returns empty';
    } else if (peers === 0) {
      state = 'ON but no eligible peer (need trust≥50 + endpoint + their allow)';
    } else {
      state = `ON · ${peers} peer(s) · fired ${fired}x · last ${last}`;
    }
    hint.textContent = state;
  } catch (e) {
    if (hint) hint.textContent = 'status unavailable';
  }
}

async function onFanoutToggle() {
  const box = document.getElementById('peer-fanout');
  const hint = document.getElementById('peer-fanout-hint');
  if (!box) return;
  const want = box.checked;
  if (hint) hint.textContent = 'saving...';
  try {
    const r = await fetch('/v1/peer/fanout', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: want})
    });
    if (!r.ok) {
      const t = await r.text();
      box.checked = !want;
      if (hint) hint.textContent = 'failed: HTTP ' + r.status;
      alert('could not change the auto peer-search setting: HTTP ' + r.status + ' ' + t.slice(0, 160));
      return;
    }
    await loadFanoutState();
  } catch (e) {
    box.checked = !want;
    if (hint) hint.textContent = 'failed';
  }
}

async function onPeerAddByUrl() {
  const input = document.getElementById('peer-add-url');
  const msg = document.getElementById('peer-add-url-msg');
  if (!input) return;
  const raw = (input.value || '').trim();
  if (!raw) return;
  let url = raw;
  if (!/^https?:\/\//i.test(url)) url = 'http://' + url;
  if (msg) msg.textContent = 'looking up...';
  try {
    // The lookup runs on the operator's machine (see /v1/peer/lookup), so
    // the browser does not hit the remote origin's CORS policy and no
    // credential leaves this host.
    const look = await fetch('/v1/peer/lookup?url=' + encodeURIComponent(url));
    if (!look.ok) {
      let detail = '';
      try { detail = (await look.json()).detail || ''; } catch (e) { /* ignore */ }
      if (msg) msg.textContent = 'lookup failed: HTTP ' + look.status;
      alert('could not read the identity at that URL.\n\nHTTP ' + look.status +
            (detail ? '\n' + detail : '') +
            '\n\nCheck the address, and that the node is reachable from this machine.');
      return;
    }
    const j = await look.json();
    const pid = j.peer_id;
    if (!pid) {
      if (msg) msg.textContent = 'no peer id at that URL';
      return;
    }
    const add = await fetch('/v1/peer/add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({peer_id: pid, endpoint: url, trust: 60})
    });
    if (!add.ok) {
      const t = await add.text();
      if (msg) msg.textContent = 'add failed: HTTP ' + add.status;
      alert('add failed: HTTP ' + add.status + ' ' + t.slice(0, 160));
      return;
    }
    input.value = '';
    // Refresh the list, but do not let a failure in the refresh undo
    // the fact that the peer was added. The add already succeeded; a
    // refresh that throws is a display problem, not an add failure.
    try {
      await fetchPeers();
    } catch (e) {
      /* the list will refresh on the next poll */
    }
    if (msg) msg.textContent = 'added ' + pid;
  } catch (e) {
    if (msg) msg.textContent = 'lookup failed';
  }
}

  // v1.17.1: cross-node peer search.
  // The endpoint (/v1/peer/search) has been there all along -- it asks
  // every eligible peer, over the signed peer protocol, for matching
  // facts from their public tier. Nothing in the dashboard reached it,
  // so the whole federation was invisible from the UI.
  async function onPeerSearch() {
    const input = document.getElementById('peer-search-q');
    const msg = document.getElementById('peer-search-msg');
    const box = document.getElementById('peer-search-results');
    if (!input) return;
    const q = (input.value || '').trim();
    if (!q) { if (msg) msg.textContent = 'type something to search for'; return; }
    if (msg) msg.textContent = 'searching...';
    if (box) { box.hidden = true; box.innerHTML = ''; }
    try {
      const r = await fetch('/v1/peer/search?q=' + encodeURIComponent(q) + '&limit=10');
      const j = await r.json();
      if (!r.ok) {
        if (msg) msg.textContent = 'search failed: HTTP ' + r.status;
        return;
      }
      const results = j.results || [];
      const perPeer = j.per_peer || [];
      const targets = j.targets || 0;
      if (msg) {
        msg.textContent = targets === 0
          ? (j.hint || 'no eligible peers')
          : (results.length + ' result(s) from ' + targets + ' peer(s)');
      }
      if (!box) return;
      if (perPeer.some(p => p.error)) {
        const bad = perPeer.filter(p => p.error).map(p => p.peer_id + ': ' + p.error);
        const d = document.createElement('div');
        d.className = 'peer-search-note';
        d.textContent = 'some peers did not answer -- ' + bad.join('; ');
        box.appendChild(d);
      }
      if (results.length === 0) {
        const d = document.createElement('div');
        d.className = 'peer-search-note';
        d.textContent = 'nothing matched';
        box.appendChild(d);
      }
      results.forEach(res => {
        const d = document.createElement('div');
        d.className = 'peer-search-item';
        const src = document.createElement('code');
        src.className = 'peer-search-src';
        src.textContent = String(res.source_peer_id || '').slice(0, 22);
        const rel = document.createElement('span');
        rel.className = 'peer-search-rel';
        rel.textContent = 'rel ' + Number(res.relevance || 0).toFixed(3);
        const txt = document.createElement('div');
        txt.className = 'peer-search-text';
        txt.textContent = String(res.content || '');
        d.appendChild(src);
        d.appendChild(rel);
        d.appendChild(txt);
        box.appendChild(d);
      });
      box.hidden = false;
    } catch (e) {
      if (msg) msg.textContent = 'search failed';
    }
  }

(function bindPeerSelfControls() {
  const copyBtn = document.getElementById('peer-self-copy');
  if (copyBtn) {
    copyBtn.addEventListener('click', () => {
      const el = document.getElementById('peer-self-id');
      if (!el) return;
      const pid = el.dataset.peerId || el.textContent || '';
      if (navigator.clipboard) navigator.clipboard.writeText(pid);
      copyBtn.textContent = 'copied';
      setTimeout(() => { copyBtn.textContent = 'copy'; }, 1200);
    });
  }
  const codeBtn = document.getElementById('peer-self-code');
  if (codeBtn) {
    codeBtn.addEventListener('click', () => {
      const el = document.getElementById('peer-self-id');
      if (!el) return;
      const code = el.dataset.friendCode || '';
      if (!code) {
        alert('no friend code yet -- set ASTOR_PUBLIC_ENDPOINT to publish one');
        return;
      }
      if (navigator.clipboard) navigator.clipboard.writeText(code);
      codeBtn.textContent = 'copied';
      setTimeout(() => { codeBtn.textContent = 'friend code'; }, 1200);
    });
  }
  const openBox = document.getElementById('peer-open-search');
  if (openBox) openBox.addEventListener('change', onOpenSearchToggle);
  const fanoutBox = document.getElementById('peer-fanout');
  if (fanoutBox) fanoutBox.addEventListener('change', onFanoutToggle);
  const urlBtn = document.getElementById('peer-add-url-btn');
  const _searchBtn = document.getElementById('peer-search-btn');
  if (_searchBtn) _searchBtn.addEventListener('click', onPeerSearch);
  const _searchIn = document.getElementById('peer-search-q');
  if (_searchIn) _searchIn.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') onPeerSearch();
  });
  if (urlBtn) urlBtn.addEventListener('click', onPeerAddByUrl);
  loadPeerIdentity().catch(() => {});
  loadOpenSearchState().catch(() => {});
  loadFanoutState().catch(() => {});
})();


document.getElementById('peer-refresh-btn').addEventListener('click', fetchPeers);
// Auto-fetch on dashboard load (in addition to the 60s loop)
fetchPeers().catch(() => {});
setInterval(() => fetchPeers().catch(() => {}), REFRESH_MS);
