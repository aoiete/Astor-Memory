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

## Access control

The dashboard is reachable from the public internet if you put a tunnel
in front of it. It ships with a passcode gate so that "if you expose
it" never turns into "anyone can read your private tier".

### What the gate protects

| Surface | Local | Public without passcode |
|---|---|---|
| The page itself (`/dashboard/`) | served | served — it is the prompt |
| `/v1/dashboard` (the aggregate) | served | **401** |
| `/v1/peer/list`, `/v1/peer/audit`, `/v1/peer/quarantine/list` | served | **401** |
| `/v1/write`, `/v1/forget`, `/v1/peer/add` | served | **401** |
| `/v1/peer/identity`, `/v1/peer/public_search`, `/v1/peer/health`, `/v1/peer/open-search` | served | served — protocol surface, public tier only |
| `/v1/health` and the static assets | served | served — not data |

The page is served to anyone on purpose: the page *is* the passcode
prompt. Gating it would lock you out of your own dashboard with no way
to enter the code.

### Local requests are trusted

Anything running on this machine — the CLI, the Hermes adapter, cron
jobs, the local dashboard JS — talks to `127.0.0.1` and is not asked
for a passcode. Those processes already have filesystem access to
`ASTOR_DIR`, which is where the passcode file lives, so gating them
would add friction without adding security.

The check looks at the **Host header** as well as the remote address.
That matters here: a Cloudflare tunnel runs on the same machine and
forwards to `127.0.0.1`, so a request from the public internet arrives
looking local. Judging by address alone would have left the public
dashboard exactly as open as it was before the gate existed.

### Using it

```bash
# show the current passcode
am dashboard passcode

# set a new one (revokes every existing session)
am dashboard passcode --set 'a-long-passcode'

# a one-shot authenticated call
curl -H 'X-Astor-Passcode: <passcode>' https://<your-host>/v1/dashboard
```

In the browser, open the dashboard and type the passcode. The browser
keeps the resulting session token in `sessionStorage`, so a reload does
not ask again but closing the tab does. Sessions expire after 12 hours.

The passcode is generated automatically on first use and stored at
`$ASTOR_DIR/identity/dashboard_passcode`. It is never written into the
repository and never returned in a response body.

If you do not expose astor to the internet, you never need the
passcode — the gate simply never triggers.


## License

Same as Astor-Memory (see `LICENSE`).

## Origin

Built 2026-09-10 in one ship cycle. See `docs/releases/v1.14.13-dashboard.md`
for the full ship history.
