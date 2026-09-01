# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.11.x  | Active |
| 1.10.x  | Security fixes only |
| < 1.10  | No longer maintained |

## Reporting a Vulnerability

**Do not file public GitHub issues for security issues.**

Email: **security@astor-memory.local**
GitHub: **Security Advisories** tab (private)

We respond within 7 days, patch within 30 days for confirmed issues, follow 90-day coordinated disclosure.

## In Scope (we fix)

astor-memory is a local memory store. Real attack surface:

- **Cross-user data leak**: user A reads user B's memory in multi-tenant deployments
- **Memory injection**: crafted input writes to wrong tier (private → public)
- **Auth bypass**: plugin reads/writes without proper `user_id` scoping
- **SQL injection** in any of the 9-DB layout (bus / forge / nest / public / source / private)
- **Pickle / YAML deserialization** in plugin loader
- **Auth bypass** in `astor-memory-server` HTTP API
- **Hardcoded secrets** in source or test fixtures

## Out of Scope

- astor-memory is a storage layer — not a content filter
- User-stored harmful content is user's responsibility
- Dependency CVEs without proven exploit path
- Theoretical issues without PoC

## Security Architecture (TL;DR)

- **3-tier × 3-store** (public / source / private × bus / forge / nest)
- **OPT-IN to private**: data is public unless explicitly flagged
- **Per-user_id keying**: all writes scoped by `user_id`
- **SQLite WAL**: no network exposure by default
- **No telemetry**: zero outbound calls

Full threat model: `docs/security/threat-model.md`
