# Architecture (Quick Reference)

For the deep-dive, see [docs/architecture.md](docs/architecture.md). This
file is a quick-reference for engineers coming back to the code after
a few months.

## 3-store × 3-tier

```
            ┌─────────────────────────────────┐
            │       bus (events + facts)      │
            ├─────────────────────────────────┤
tier=public │       forge (LLM extracts)     │
            │       nest  (embeddings + lex) │
            ├─────────────────────────────────┤
tier=source │       bus / forge / nest        │  admin-only
            ├─────────────────────────────────┤
tier=private│       bus / forge / nest        │  per-user, ACL-gated
            └─────────────────────────────────┘
```

3 stores map to 3 lifecycle stages:
- **bus**: append-only canonical facts (the "memory")
- **forge**: LLM extraction cache (turn text → structured facts)
- **nest**: vector embeddings + BM25 lex (for retrieval)

3 tiers map to 3 ACL needs:
- **public**: shared knowledge (admin-curated rules)
- **source**: admin-only (operator's own rules)
- **private**: per-user (cross-user isolation)

## 4th DB family: lex

`lex/memory/astor_lex_*.db` — BM25 inverted index. Sibling DBs (not a
sibling store), separate from the 3 stores because heavy BM25 reads
would contend with bus writes.

## Where things live

```
astor_memory/
├── bus/         # bus (events + canonical facts + audit_log + promotion)
├── forge/       # LLM extraction cache
├── nest/        # vector embeddings + BM25 lex + reranker
├── _internal/   # acl, bot_binding, audit_logger, grants, layout
├── cli/         # `am` command-line interface
├── installer/   # install-time hooks
└── hermes_adapter.py   # hermes-agent plugin entry point
```

## ACL model

```
role ∈ {admin, user}
plan ∈ {free, vip, power}  # for users only

Public write:
  - admin: OK
  - user (any plan): OK
Source write:
  - admin: OK
  - user: 403
Private write (own user_id):
  - any role, any plan: OK
Private write (cross-user):
  - admin without grant: 403
  - user without grant: 403
  - granted: OK
```

See [docs/acl-v1.2-hardening.md](docs/acl-v1.2-hardening.md) for the
threat model.

## R-classes

Hard rules the operator has locked over time. See
[bus hard_rules table] for the canonical list. Examples:
- R218 — never kill the hermes gateway yourself
- R252 — edit source first, then patch Runtime
- R354 — /v1/forget must check fact ownership
- R365 — body.user_id fallback in before_request
- R380 — "patched + verified" must include actual code diff grep