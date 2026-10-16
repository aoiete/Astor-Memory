# Quickstart

5-minute end-to-end demo.

## 1. Install

```bash
pip install astor-memory
am init
am admin lock --user-id=admin
```

## 2. Write a fact

```bash
am write --text "Astor-Memory is a self-owned memory system for AI agents."
```

## 3. Read it back

```bash
am read --query "memory system agents"
```

You should see your fact with score ~1.0 (exact match) or close to it.

## 4. Multi-user setup

```bash
# Add a non-admin user
am bot add-user --user-id=alice --role=user --plan=vip --active=1

# Write to alice's private tier
am write --text "alice's secret recipe" --tier=private --user-id=alice

# Try to read alice's private fact as admin (you should be denied by ACL)
am read --query "secret recipe" --tier=private --user-id=alice
# Returns: 403 cross_user_forbidden
```

## 5. Rerank (optional)

If you have an OpenAI-compatible API key, set it:

```bash
export OPENROUTER_API_KEY=<your-key>
export ASTOR_RERANK=1
am read --query "memory system"
```

Rerank re-orders the top-K results via LLM judgment. Disable with
`ASTOR_RERANK=0` if you don't want LLM calls.

## 6. Forgetting

```bash
# Forget a fact by id
am forget --fact-id=1

# Or by content match
am forget --query "memory system"
```

## 7. Two nodes, 60 seconds (peer gossip)

The `public` tier gossips between nodes you approve. `private` and
`source` never leave either machine.

```bash
# Node A on :7803, node B on :7804 — separate ASTOR_DIRs
export ASTOR_DIR=/tmp/astor-a
astor-server --port 7803 &
export ASTOR_DIR=/tmp/astor-b
astor-server --port 7804 &

# Each node announces itself; note the peer_id from /v1/identity
curl -s localhost:7803/v1/identity
curl -s localhost:7804/v1/identity

# Add each as the other's peer (paste the returned peer_id)
curl -X POST localhost:7803/v1/peer/add \
  -H 'Content-Type: application/json' \
  -d '{"peer_id":"<B-peer-id>","endpoint":"http://127.0.0.1:7804"}'

curl -X POST localhost:7804/v1/peer/add \
  -H 'Content-Type: application/json' \
  -d '{"peer_id":"<A-peer-id>","endpoint":"http://127.0.0.1:7803"}'

# Approve the pending peer (it sits in quarantine until you do)
curl -X POST localhost:7803/v1/peer/unquarantine \
  -H 'Content-Type: application/json' -d '{"peer_id":"<B-peer-id>"}'

# Write a public fact on A
curl -X POST localhost:7803/v1/write \
  -H 'Content-Type: application/json' \
  -d '{"text":"hello from node A","tier":"public","kind":"fact"}'

# Read it back on B
curl -X POST localhost:7804/v1/read \
  -H 'Content-Type: application/json' \
  -d '{"query":"hello from node A","tier":"public","top_k":5}'

# Inspect the network
curl -s localhost:7803/v1/peer/list
curl -s localhost:7803/v1/peer/health
```

A private fact written on A never appears on B — only `public` gossips.

## What's next?

- [README.md](README.md) — full feature list
- [docs/architecture.md](docs/architecture.md) — 3-store × 3-tier design
- [OPERATOR_RUNBOOK.md](OPERATOR_RUNBOOK.md) — operational tasks