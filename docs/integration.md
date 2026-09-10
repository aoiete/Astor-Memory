# Integration Guide — Use astor-memory as your AI's Memory Backend

## Why astor-memory?

**astor-memory is a self-hostable memory service designed to be the shared
backend for ALL of your AI agents — regardless of which model or runtime
they use.**

The key principle (borrowed from the WPS/Office analogy in [Memmy's
design philosophy](https://mp.weixin.qq.com/s/2AC0yy6HdUMC9EU4DccgFA)):

> **Agent can change. Model can change. But accumulated context must not.**

astor-memory is built for this. It runs as a local REST service, stores
facts in SQLite, and exposes 25+ endpoints that any HTTP client can call.

## 30-second quickstart

```bash
# 1. Start the server
pip install astor-memory
astor-memory serve --port 7803

# 2. From any Python agent:
python -c "
from astor_memory.client import AstorClient
c = AstorClient(base_url='http://127.0.0.1:7803', user_id='my-agent')
c.write('User prefers Chinese replies', kind='user_preference', importance=0.8)
facts = c.read('user preferences', top_k=5)
for f in facts: print(f.id, f.content)
"
```

That's it. The same `astor-memory` service can be hit by:
- Hermes Agent (built-in adapter)
- Claude Code (via this client SDK)
- Codex CLI (via this client SDK)
- Your custom scripts (via REST or this SDK)
- An entirely different model tomorrow

## What stays the same across agents

| Property | Persists? | Why |
|---|---|---|
| User preferences | ✅ | Stored in canonical tier |
| R-class rules | ✅ | Importance 0.95, locked |
| Failure patterns | ✅ | Tagged for future recall |
| Conversation context | ❌ (per-session) | By design — context belongs to session |
| Embedding model version | ✅ | Versioned in metadata |
| ACL grants | ✅ | Stored per-user, per-tier |

## What you need to migrate

If you're switching from a different memory tool, here are the typical
things to preserve:

| From | Path |
|---|---|
| Memmy local browser storage | Export JSON → script `migrate_memmy.py` (TODO) |
| Chroma | `chroma export` → batch write to astor `/v1/write` |
| Manual notes | `astor_initial_report.py --user <name>` generates summary |

## How to integrate with your agent

### Python agent (recommended)

Use the official client SDK:

```python
from astor_memory.client import AstorClient

# At session start — recall user context
client = AstorClient(base_url=ASTOR_URL, user_id="my-agent")
context = client.read(query="user preferences", top_k=10)
system_prompt_block = "\n".join(f"- {f.content}" for f in context)

# During session — capture important findings
if "important insight":
    client.write(content="...", kind="lesson", importance=0.8)

# At session end — store follow-up items
client.write(content="TODO: review trading positions", kind="decision", importance=0.7)
```

### Non-Python agent (any HTTP client)

```bash
# Read
curl -X POST http://127.0.0.1:7803/v1/read \
  -H "Content-Type: application/json" \
  -d '{"query":"user preferences","top_k":5,"user_id":"my-agent"}'

# Write
curl -X POST http://127.0.0.1:7803/v1/write \
  -H "Content-Type: application/json" \
  -d '{"content":"User likes Python","kind":"fact","importance":0.6,"user_id":"my-agent"}'
```

### Hermes Agent (zero-config)

Already wired in. Just talk normally — the gateway hook captures signals
and `astor_recall` tool surfaces relevant facts at session start.

## Tier model

astor-memory uses 3 isolation tiers:

| Tier | ACL | Use case |
|---|---|---|
| `public` | anyone can read/write | documentation, general knowledge |
| `source` | agent-only | internal architecture notes |
| `private` | per-user | user preferences, personal context |

Default tier for client.write() is `private`. Override with `tier="source"`.

## Endpoints reference

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/health` | GET | server liveness |
| `/v1/read` | POST | hybrid vector + BM25 search |
| `/v1/write` | POST | add fact (auto-routes) |
| `/v1/forget` | POST | tombstone (soft-delete) |
| `/v1/fact/<id>/provenance` | GET | audit trail |
| `/v1/fact/<id>/lineage` | GET | parent/child graph |
| `/v1/fact/<id>/restore` | POST | un-tombstone |
| `/v1/grant` | POST | grant ACL access |
| `/v1/grant/list` | POST | list current grants |
| `/v1/merge/find` | POST | find duplicate candidates |
| `/v1/merge/apply` | POST | merge duplicates |
| `/v1/reflection/run` | POST | trigger self-reflection |
| `/v1/audit/health` | GET | audit subsystem health |
| `/v1/lex/stats` | GET | lexical index stats |
| ... 11 more | | see `python -m astor_memory.server --help` |

## Design philosophy

From the Memmy article (Sept 2026):

> "Agent can change. Model can change. **What cannot be lost is the
> previously accumulated context.**"
>
> "This reminds me of the WPS vs Office saga — **supporting the
> other's file format is unambiguously the right thing to do**.
> The same applies to agents."

astor-memory takes this philosophy and applies it to the memory layer:
- **Format-agnostic** — any HTTP client can call it, any agent can use it
- **Backward compatible** — fact schema is versioned; old facts still read
- **Forward compatible** — new kinds/fields added without breaking clients
- **Local-first** — runs on your laptop, not in someone else's cloud
- **Open source** — MIT license, fix it yourself

## Why not just use Memmy?

| | Memmy | astor-memory |
|---|---|---|
| Cross-agent memory | ✅ | ✅ |
| Local-first | ✅ | ✅ |
| Source code | open | open (MIT) |
| ACL isolation | basic | 3-tier + per-user grants |
| Embedding version control | implicit | explicit (`embedding_version` field) |
| Self-audit / grounding | manual | automated (`astor_grounding_audit.py`) |
| Skill extraction | yes | yes (`astor_skill_extractor.py`) |
| License | — | MIT |

You can use both — they're not mutually exclusive. astor-memory can act
as the persistent backend while Memmy provides the UI layer, or vice versa.

## Roadmap (v1.15+)

- LLM-assisted classification for ambiguous content
- Embedding-based skill cluster detection
- WebSocket streaming for real-time updates
- Multi-server replication (when local isn't enough)

## Getting help

- GitHub: https://github.com/<repo-owner>/Astor-Memory/issues
- Docs: https://github.com/<repo-owner>/Astor-Memory/tree/main/docs
- Examples: `examples/` directory in the repo
