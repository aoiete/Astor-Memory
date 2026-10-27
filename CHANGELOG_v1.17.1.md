## v1.17.1 — 2026-10-27

The dashboard learned to say no.

Astor can be reached from the public internet. The dashboard had no
authentication, so anyone who opened the URL could read the aggregate
payload — which includes private-tier fact text and the list of bound
users — and could call the write endpoints directly. This release adds a
passcode gate, and the peer panel gains the three controls that were
missing from it.

### The passcode gate

    am dashboard passcode                # show it
    am dashboard passcode --set '...'    # set a new one, revokes sessions

The passcode is generated on first use and stored in the runtime
directory. Entering it in the browser returns a session token that lives
in `sessionStorage`, so a reload does not ask again but closing the tab
does. Sessions expire after 12 hours.

What it protects:

| Surface | Local | Public without the passcode |
|---|---|---|
| the dashboard page | served | served — it is the prompt |
| `/v1/dashboard` | served | 401 |
| `/v1/peer/list`, `/v1/peer/audit`, `/v1/peer/quarantine/list` | served | 401 |
| `/v1/write`, `/v1/forget`, `/v1/peer/add` | served | 401 |
| the peer protocol reads | served | served — public tier only |
| `/v1/health`, the static assets | served | served — not data |

The page is served to anyone on purpose. The page *is* the passcode
prompt; gating it would lock the operator out with no way to enter the
code. The shell contains no data. The data lives behind the endpoints
above, and those are gated.

Local requests are trusted, and the check looks at the Host header as
well as the remote address. That detail matters: a reverse proxy on the
same machine forwards to `127.0.0.1`, so a request from the public
internet arrives looking local. Judging by address alone would have
left the dashboard exactly as open as it was before the gate existed.

The peer exemption is an explicit list rather than a prefix. The
protocol surface is open — a remote node needs to know its own identity
and to run a public-tier query — but `/v1/peer/list` returns the whole
relationship graph and `/v1/peer/audit` returns the query log. Those are
operator data, not protocol.

### Peer panel

Three things the operator had to go to a log file or a shell for:

- **Identity card.** The node's own peer id, with a copy button, and the
  friend code when one is published.
- **Open-to-public-search switch.** Whether any signature-verified peer
  may query this node's public tier, without leaving the dashboard.
- **Add by URL.** Paste a node's URL and its peer id is looked up
  automatically, so the friend code does not have to be transcribed by
  hand. The lookup runs on the operator's machine, so the browser does
  not hit the remote origin's CORS policy and no credential leaves the
  host.

New endpoint: `GET /v1/peer/lookup?url=...` returns the public identity
document of the node at that URL. It stores nothing. Endpoints that are
never peers — the cloud metadata addresses — are refused.

### Fixes

- The peer panel never rendered. `escapeHtml` was defined inside the
  dashboard IIFE while `fetchPeers` and `renderPeerRows` sat outside it,
  so the panel threw `escapeHtml is not defined` and reported "fetch
  peers failed". The escaper now lives at the top level.
- Static assets are served with `Cache-Control: no-cache`. The edge
  cache held them for four hours, so a rebuilt dashboard kept serving
  the previous build — which reads as "my change did nothing".
- The auth gate is registered after the ACL binding hook. Registered
  first, it returned before the binding and the health endpoint answered
  "astor_acl not initialized" instead of its own payload.
- The open-search switch no longer lets a failed refresh overwrite the
  success message of an add that actually succeeded.

### Tests

`tests/test_dashboard_auth.py` — 25 checks covering the passcode, the
session lifetime, the local-versus-public decision (including the
reverse-proxy case), the peer exemption list, the rate limit, the
endpoint validation, and the route table. The route table check exists
because a decorator inserted next to another route silently stole it:
`/dashboard/` was being served by the auth handler.

The suite was verified against three deliberately broken variants — the
host-header check removed, the peer exemption widened to a prefix, and
the gate moved ahead of the ACL hook — and each one fails the tests
that are supposed to catch it.

### The theme was broken, not just the new features

The dashboard had been restyled to a light warm theme, but a set of
rules still carried fallbacks from the dark theme it used to be. Five
CSS variables — `--card`, `--bg-card`, `--bg-soft`, `--bg-elev`,
`--mono` — were referenced but never defined, so every rule that used
them got the hardcoded dark fallback instead of a theme colour. The
passcode gate was one of them: a near-black card with `--text`
rendered on it, which under the light theme is dark text on a dark
card — a contrast ratio of 1.0, invisible.

Two more of the same kind:

- `--accent` (`#FF8C2E`) is a fill colour. It was also being used as
  text colour on the light background, where it reached a ratio of
  1.95. The headings, the peer ids and the role tags were all fading
  out.
- Buttons put white text on the accent fill, at 2.32.

The fix keeps the theme exactly as it was and separates the two jobs:

    --accent-text: #A84807    the same hue, dark enough to read as text
    --on-accent:   #2A1706    the text colour on an accent fill
    --warn-text / --danger-text / --good-text

Dark text on the bright orange reads at 7.40, where white on it read
at 2.32 — so the buttons keep the colour they had and become legible.

All five missing variables are now defined, so no rule falls back to a
colour from a theme that no longer exists.

The verification was programmatic rather than by eye: a pass over every
text node in the live page computing WCAG contrast (340 nodes, 20
failures before, 0 after), and a static pass over the stylesheet
resolving every variable and compositing every alpha background (18
failures before, 0 after).

Two smaller findings came out of the same pass:

- `white-space: nowrap` in the base rule outranks `overflow-wrap:
  anywhere`, so the friend code in the header was clipped rather than
  wrapped on a narrow screen.
- The edge cache held static assets for four hours, so a rebuilt
  dashboard kept serving the previous build. They now carry
  `Cache-Control: no-cache`, and the asset URLs carry a version.
