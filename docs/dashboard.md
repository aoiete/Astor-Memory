# Astor Dashboard — Generic Template

## What this is

The dashboard shipped in v1.14.13–v1.14.18 is a **generic web UI for any
Astor-Memory installation**. It is not a hosted service — there is no
operator-specific data baked into the code.

The dashboard reads:

- `?astor_dir=<path>` (optional) — defaults to whatever
  `get_default_astor_dir()` returns on the host
- `?user=<user_id>` (optional) — defaults to `admin`

Every endpoint (`/v1/dashboard`, `/v1/health/diagnose`, `/v1/health`) is a
read-only aggregator over a user-specified ASTOR_DIR. There is no
authentication on top of these endpoints — it is the operator's job to
expose them only to trusted callers (private network, Cloudflare Access,
reverse-proxy auth, etc.).

## How to use it on your own installation

```bash
# 1. Install astor-memory (any 1.14.13+)
git clone https://github.com/aoiete/Astor-Memory
cd Astor-Memory
pip install -e .

# 2. Start your server (any port; 7803 is just the default)
python -m astor_memory.server --host 127.0.0.1 --port 7803

# 3. Open the dashboard
open http://127.0.0.1:7803/dashboard/

# 4. Or pass an explicit astor_dir + user via query:
open "http://127.0.0.1:7803/dashboard/?astor_dir=/path/to/your/runtime&user=yourname"
```

## What the dashboard shows

Six dimensions, all read-only:

- **Hero** — total users / active facts / tombstoned / high-importance /
  last-event delta / trend status
- **Eval trend** — last 30 days of `hit_rate / mrr / p95 latency` from
  `eval_history.jsonl` + per-variant latest
- **Per-user** — top 16 users by active fact count
- **Growth 30d** — daily promoted count
- **Keywords + Recent facts** — top 20 keywords (from
  `canonical.keywords`) + last 5 promoted facts (content shown — see
  "Privacy" below)
- **Importance histogram + Health** — 4-bucket importance distribution +
  embedding-failed / audit-warning counts (clickable → modal with detail)

Plus a live **Recall debugger** — POST `/v1/read` with
`{query, tier, user, top_k}` to see top-5 facts inline.

## Privacy

This template does not collect or transmit anything by itself. When you
host it:

- The dashboard **runs in your browser** and talks to **your** server.
- Nothing is sent to any third party (Chart.js is loaded from jsdelivr
  CDN — change `<script src=...>` to a vendored copy if you need offline).
- The default response payloads include **summary counts** and
  **recent-fact content**. If you do not want fact content exposed, edit
  `astor_memory/dashboard_data.py:_top_keywords_and_recent()` to return
  `recent_facts = []` (or restrict via reverse proxy).
- The `?astor_dir=` override means anyone who knows the URL can point
  the dashboard at any ASTOR_DIR your server can read. **Do not expose
  this on the public internet without Cloudflare Access, HTTP Basic
  Auth, or equivalent.**

## Customization

The dashboard lives in `astor_memory/dashboard/`:

- `index.html` — 5-section layout (hero + 4 rows of cards)
- `style.css` — warm-orange theme via CSS variables (`--bg`, `--accent`,
  `--border`, etc.). Override by adding a `:root` rule in your own CSS.
- `app.js` — vanilla fetch + Chart.js. Polls `/v1/dashboard` every 60s.

To change the theme, swap the `:root` block at the top of `style.css`:

```css
:root {
  --bg: #your-bg;
  --accent: #your-accent;
  --border: #your-border;
  ...
}
```

To change the layout, edit `index.html` — each card is one `<div class="card">`.

## License

Same as Astor-Memory (see `LICENSE`).

## Origin

Built 2026-09-10 in one ship cycle. See `docs/releases/v1.14.13-dashboard.md`
for the full ship history.
