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

## What's next?

- [README.md](README.md) — full feature list
- [docs/architecture.md](docs/architecture.md) — 3-store × 3-tier design
- [OPERATOR_RUNBOOK.md](OPERATOR_RUNBOOK.md) — operational tasks