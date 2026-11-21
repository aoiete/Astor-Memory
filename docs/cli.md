# CLI reference

Every `am` subcommand, generated from the CLI's own argument parser.
Regenerate with `python tools/gen_cli_doc.py`; the document is never
edited by hand, so a command that exists is always documented and a
command that was removed disappears from here.

## Index

| command | what it does |
| --- | --- |
| `am dashboard` | Dashboard access control |
| `am version` | Show version |
| `am export` | Export the memory as Markdown files (MEMORY.md + one bullet per memory) |
| `am bundle` | Pack the memory state into one file, and put it back |
| `am init` | Initialize astor-memory |
| `am seed` | Populate demo data (first run / screenshots / HN) |
| `am write` | Write a fact |
| `am procedure` | Write a Procedure Memory fact (trigger/diagnose/act/verify) |
| `am skill-distill` | Render a procedure memory as an agent skill document |
| `am skill-stale-check` | Report skill documents whose source facts changed or died |
| `am noise-scan` | Report machine-noise rows in a pool (read-only) |
| `am recall` | Recall facts |
| `am doctor` | Health check |
| `am scrub-public` | Replace machine-local identifiers in shared-tier facts |
| `am mine-public` | Promote reusable private-tier facts to the shared tier |
| `am config` | View / modify config |
| `am preflight` | Check that this host can run astor (run this right after install) |
| `am install` | Install Astor-Memory into another agent |
| `am reembed` | Compute + persist embeddings for all canonical facts |
| `am learn` | Write a fact + auto-detect success pattern + auto-promote recurring success |
| `am fail` | Write a fact + auto-detect failure pattern (kind=failure_pattern, tier=public) |
| `am postmortem` | Write a LESSON fact (kind=postmortem/lesson, tier=public, importance=0.99) |
| `am mental-model` | Hindsight-style fixed-question answer sheet (kind=mental_model) |
| `am knowledge-page` | Hindsight-style knowledge page (kind=knowledge_page) |
| `am cascade` | Replay cascade write queue (embed failures) |
| `am mcp` | MCP server (Model Context Protocol) |
| `am reflection` | Episodic reflection (merge + tombstone) |
| `am auto-link` | Zettelkasten auto-link (audit-safe) |
| `am decay-sweep` | [v1.14.73] Auto soft-tombstone facts by stale policy |
| `am bot` | Multi-user bot management (admin only) |
| `am admin` | admin system operations |
| `am lock-rule` | Manage LOCK rules that drive /v1/classify |
| `am recall-history` | List recent recall queries (from recall_log.jsonl) |
| `am recall-trail` | Trace which recall queries brought a fact back |
| `am consult` | Reactive consult: surface success_pattern / failure_pattern / lesson hits for query |
| `am recall-auto` | Auto-classify an error / tool-output string into the right recall zone |
| `am relay` | Relay service (postman, not reader) |
| `am pending` | List candidates that were written but never promoted |
| `am retired` | List the facts that were retired as not working |
| `am reactivate` | Restore a retired fact to current truth |
| `am confirm` | Promote a pending candidate |
| `am agent` | Which LLM provider each connecting agent wants used |
| `am reject` | Drop a pending candidate |
| `am peer` | Peer network identity + relationships (Phase 1: status only) |
| `am platform` | Manage bot-binding.db (platforms + bindings + users) |

## Commands

### `am dashboard`

```
usage: am dashboard [-h] {passcode} ...

positional arguments:
  {passcode}
    passcode  Show or set the dashboard passcode

options:
  -h, --help  show this help message and exit
```

### `am version`

```
usage: am version [-h]

options:
  -h, --help  show this help message and exit
```

### `am export`

```
usage: am export [-h] [--out OUT] [--tiers TIERS] [--astor-dir ASTOR_DIR]
                 [--git] [--dry-run]

options:
  -h, --help            show this help message and exit
  --out OUT             Output directory (default: ~/astor-export)
  --tiers TIERS         Comma-separated tiers to include (default: all)
  --astor-dir ASTOR_DIR
                        Astor runtime dir (default: $ASTOR_DIR)
  --git                 Init or reuse a git repo in the output and commit
  --dry-run             Report what would be written without writing it
```

### `am bundle`

```
usage: am bundle [-h] {export,import,inspect} ...

positional arguments:
  {export,import,inspect}
    export              Pack the memory state into a tar.gz
    import              Restore a bundle
    inspect             Show a bundle manifest

options:
  -h, --help            show this help message and exit
```

### `am init`

```
usage: am init [-h]

options:
  -h, --help  show this help message and exit
```

### `am seed`

```
usage: am seed [-h] [--user-id USER_ID] [--count COUNT] [--quiet]

options:
  -h, --help         show this help message and exit
  --user-id USER_ID  Owner user_id (default: admin)
  --count COUNT      Max demo facts (default: all bundles)
  --quiet            Suppress per-fact output
```

### `am write`

```
usage: am write [-h] [--user USER] [--mode MODE] [--tier TIER] [--allow-pii]
                text

positional arguments:
  text         Fact text

options:
  -h, --help   show this help message and exit
  --user USER  User ID
  --mode MODE  Extract mode: auto|none|regex|llm
  --tier TIER  Tier: public|source|private_<user>
  --allow-pii  Write to the public tier even if it contains PII
```

### `am procedure`

```
usage: am procedure [-h] [--trigger TRIGGER] [--analysis ANALYSIS]
                    [--action ACTION] [--verify VERIFY] [--text TEXT]
                    [--from-file FROM_FILE] [--tier {public,source,private}]
                    [--user USER] [--user-id USER_ID]
                    [--importance IMPORTANCE]

options:
  -h, --help            show this help message and exit
  --trigger TRIGGER     When this happens (the question shape)
  --analysis ANALYSIS   How to diagnose it
  --action ACTION       What to do
  --verify VERIFY       How to confirm it worked
  --text TEXT           Free-form write-up to split into the four parts
  --from-file FROM_FILE
                        Read the write-up from a file
  --tier {public,source,private}
                        Tier to write (default private)
  --user USER           Namespace / owner (default admin)
  --user-id USER_ID     Private-tier owner id (defaults to --user)
  --importance IMPORTANCE
```

### `am skill-distill`

```
usage: am skill-distill [-h] --fact FACT --out-dir OUT_DIR [--name NAME]
                        [--category CATEGORY] [--description DESCRIPTION]
                        [--tier {public,source,private}] [--user-id USER_ID]
                        [--extra-frontmatter EXTRA_FRONTMATTER] [--sanitize]

options:
  -h, --help            show this help message and exit
  --fact FACT           Procedure fact id to render
  --out-dir OUT_DIR     Directory to write the document into
  --name NAME           Document name (default: derived from the fact id)
  --category CATEGORY   Optional subdirectory under --out-dir
  --description DESCRIPTION
                        One-line description
  --tier {public,source,private}
  --user-id USER_ID     Private-tier owner id
  --extra-frontmatter EXTRA_FRONTMATTER
                        JSON object merged into the frontmatter metadata
  --sanitize            Strip PII and replace names/dates/places
```

### `am skill-stale-check`

```
usage: am skill-stale-check [-h] --skills-dir SKILLS_DIR
                            [--tier {public,source,private}]
                            [--user-id USER_ID]

options:
  -h, --help            show this help message and exit
  --skills-dir SKILLS_DIR
                        Directory to scan (recursively)
  --tier {public,source,private}
  --user-id USER_ID     Private-tier owner id
```

### `am noise-scan`

```
usage: am noise-scan [-h] [--tier {public,source,private,repo}]
                     [--user-id USER_ID] [--limit LIMIT] [--show] [--archive]

options:
  -h, --help            show this help message and exit
  --tier {public,source,private,repo}
  --user-id USER_ID     Private-tier owner id
  --limit LIMIT         Rows to list; 0 = all (default 50)
  --show                Print the row content
  --archive             Soft-tombstone the matched rows (reversible via am
                        restore)
```

### `am recall`

```
usage: am recall [-h] [--user USER] [--top-k TOP_K] [--kinds KINDS]
                 [--zone {all-zones,failure,lesson,none,success}]
                 [--jev-relevance {off,on}]
                 query

positional arguments:
  query                 Query text

options:
  -h, --help            show this help message and exit
  --user USER           User ID
  --top-k TOP_K         Number of results
  --kinds KINDS         Comma-separated kind filter (e.g.
                        user_preference,failure_pattern)
  --zone {all-zones,failure,lesson,none,success}
                        Named outcome zone (default: success). failure |
                        success | lesson | all-zones | none (raw)
  --jev-relevance {off,on}
                        Rerank top hits via jev (off | on; default off)
```

### `am doctor`

```
usage: am doctor [-h] [--memory] [--schema] [--migrate-user-meta] [--apply]

options:
  -h, --help           show this help message and exit
  --memory             Memory stats
  --schema             Schema check
  --migrate-user-meta  Report user_meta rows with unroutable default_tier /
                       subscription_plan
  --apply              With --migrate-user-meta, apply the repair
```

### `am scrub-public`

```
usage: am scrub-public [-h] [--apply]

options:
  -h, --help  show this help message and exit
  --apply     Actually rewrite (default: dry-run)
```

### `am mine-public`

```
usage: am mine-public [-h] [--apply] [--limit LIMIT] [--min-length MIN_LENGTH]

options:
  -h, --help            show this help message and exit
  --apply               Actually promote (default: dry-run)
  --limit LIMIT         Stop after N promotions (0: no limit)
  --min-length MIN_LENGTH
                        Shortest fact worth sharing (default: 40)
```

### `am config`

```
usage: am config [-h] [action] [key] [value]

positional arguments:
  action      show | get | set
  key         Config key (e.g. rate_limits.recall_per_hour)
  value       Value to set

options:
  -h, --help  show this help message and exit
```

### `am preflight`

```
usage: am preflight [-h] [--simulate-vcruntime X.Y] [--fix]

options:
  -h, --help            show this help message and exit
  --simulate-vcruntime X.Y
                        Pretend vcruntime140_1.dll reports this version.
                        Exercises the blocked path so the failure output can
                        be verified without downgrading anything. Windows
                        only.
  --fix                 Attempt to repair what can be repaired automatically.
                        Currently: download and install the Visual C++
                        redistributable when it is too old for onnxruntime.
                        Requires administrator rights; will prompt via UAC.
```

### `am install`

```
usage: am install [-h] --ide
                  {claude-code,cline,opencode,hermes,openclaw,cursor,continue,windsurf,aider}
                  [--mode {auto,priority,coexist,replace,verify}]
                  [--agent-dir AGENT_DIR] [--apply]

options:
  -h, --help            show this help message and exit
  --ide {claude-code,cline,opencode,hermes,openclaw,cursor,continue,windsurf,aider}
                        Target agent
  --mode {auto,priority,coexist,replace,verify}
                        Install mode
  --agent-dir AGENT_DIR
                        Agent config dir (default: ~)
  --apply               Actually write files (default: dry-run)
```

### `am reembed`

Re-computes embeddings for canonical facts that do not have one. It
walks every (tier, user) pair, including the private ones -- writing
into a private scope needs the owner identity, and a scope it cannot
write to is named in the output rather than skipped silently.

A fact in the store with no embedding is captured and not retained:
the semantic channel cannot reach it, so it comes back only on an
exact keyword match. `scripts/astor_doctor.py check` reports how many
there are per tier.

```
usage: am reembed [-h] [--limit LIMIT] [--batch-size BATCH_SIZE]

options:
  -h, --help            show this help message and exit
  --limit LIMIT         Max facts to re-embed (default: all)
  --batch-size BATCH_SIZE
                        Batch size for model.embed()
```

### `am learn`

```
usage: am learn [-h] [--user USER] [--tier TIER] [--threshold THRESHOLD]
                [--no-promote] [--allow-pii]
                text

positional arguments:
  text                  Fact text (often a "this works / 搞定了" statement)

options:
  -h, --help            show this help message and exit
  --user USER           User ID
  --tier TIER           Tier: public|source|private_<user> (default private)
  --threshold THRESHOLD
                        Auto-promote to public if similar success facts >=
                        threshold (default 3)
  --no-promote          Skip auto-promotion; just write with outcome=success
  --allow-pii           Write to the public tier even if it contains PII
```

### `am fail`

```
usage: am fail [-h] [--user USER] [--tier TIER] [--allow-pii] text

positional arguments:
  text         Fact text describing a failed approach

options:
  -h, --help   show this help message and exit
  --user USER  User ID
  --tier TIER  Tier: public|source|private_<user> (default public)
  --allow-pii  Write to the public tier even if it contains PII
```

### `am postmortem`

```
usage: am postmortem [-h] [--user USER] [--tier TIER] [--tier-hint TIER_HINT]
                     [--behavior-class BEHAVIOR_CLASS]
                     text

positional arguments:
  text                  LESSON fact text (must include error + root cause +
                        fix)

options:
  -h, --help            show this help message and exit
  --user USER           User ID
  --tier TIER           Tier: public|source|private_<user> (default public,
                        v1.16+)
  --tier-hint TIER_HINT
                        tier_hint metadata field (default: lesson)
  --behavior-class BEHAVIOR_CLASS
                        behavior_class when tier_hint=behavior (default:
                        lesson)
```

### `am mental-model`

```
usage: am mental-model [-h] {list,rebuild} ...

positional arguments:
  {list,rebuild}
    list          List all mental_models for tier
    rebuild       Re-synthesize a mental_model from canonical facts

options:
  -h, --help      show this help message and exit
```

### `am knowledge-page`

```
usage: am knowledge-page [-h] {upsert,list,get} ...

positional arguments:
  {upsert,list,get}
    upsert           Insert or refresh a knowledge_page
    list             List knowledge_pages
    get              Get knowledge_page by slug + linked facts

options:
  -h, --help         show this help message and exit
```

### `am cascade`

```
usage: am cascade [-h] {replay,stats,purge} ...

positional arguments:
  {replay,stats,purge}
    replay              Replay pending cascade rows
    stats               Show cascade queue counts
    purge               Delete old succeeded/failed rows

options:
  -h, --help            show this help message and exit
```

### `am mcp`

```
usage: am mcp [-h] {serve} ...

positional arguments:
  {serve}
    serve     Run MCP stdio server

options:
  -h, --help  show this help message and exit
```

### `am reflection`

```
usage: am reflection [-h] {run} ...

positional arguments:
  {run}
    run       Run reflection on a tier

options:
  -h, --help  show this help message and exit
```

### `am auto-link`

```
usage: am auto-link [-h] {backfill} ...

positional arguments:
  {backfill}
    backfill  Backfill auto-links for existing facts

options:
  -h, --help  show this help message and exit
```

### `am decay-sweep`

```
usage: am decay-sweep [-h] {run,stats,stagnation} ...

positional arguments:
  {run,stats,stagnation}
    run                 Run decay sweep (dry-run by default)
    stats               Show decay-eligible counts only (no sweep)
    stagnation          Detect stagnation in recent decay sweeps (read-only)

options:
  -h, --help            show this help message and exit
```

### `am bot`

```
usage: am bot [-h]
              {on,off,add-user,list-users,promote,demote,bind-platform,unbind,status}
              ...

positional arguments:
  {on,off,add-user,list-users,promote,demote,bind-platform,unbind,status}
    on                  Enable multi-user mode
    off                 Disable multi-user mode
    add-user            Create empty 9-db layout for a new user
    list-users          List users on disk + their roles
    promote             Promote user to admin
    demote              Demote admin to user
    bind-platform       Map platform chat_id to user_id
    unbind              Remove a platform binding
    status              Show bot on/off + platform bindings

options:
  -h, --help            show this help message and exit
```

### `am admin`

```
usage: am admin [-h] {audit-log,demote,whoami} ...

positional arguments:
  {audit-log,demote,whoami}
    audit-log           Read audit rows
    demote              Move a public fact to private_<user> (admin only)
    whoami              Show current admin lock

options:
  -h, --help            show this help message and exit
```

### `am lock-rule`

```
usage: am lock-rule [-h] {add,list,delete,test} ...

positional arguments:
  {add,list,delete,test}
    add                 Seed a new LOCK rule
    list                List LOCK rules
    delete              Soft-delete a LOCK rule by rule_id
    test                Evaluate text against current LOCK rules

options:
  -h, --help            show this help message and exit
```

### `am recall-history`

```
usage: am recall-history [-h] [--limit LIMIT] [--tier TIER] [--user USER]
                         [--hours HOURS] [--contains CONTAINS] [--reverse]

options:
  -h, --help           show this help message and exit
  --limit LIMIT        Max entries to show (default 20)
  --tier TIER          Filter by tier (public/source/private)
  --user USER          Filter by user_id
  --hours HOURS        Only show entries from the last N hours
  --contains CONTAINS  Substring filter on query text
  --reverse            Show oldest first (default newest first)
```

### `am recall-trail`

```
usage: am recall-trail [-h] [--hours HOURS] [--limit LIMIT] fact_id

positional arguments:
  fact_id        fact id to trace

options:
  -h, --help     show this help message and exit
  --hours HOURS  only the last N hours (0 = all time)
  --limit LIMIT  cap rows shown (default 20)
```

### `am consult`

```
usage: am consult [-h] [--user USER] [--tier TIER] [--top-k TOP_K] query

positional arguments:
  query          What you're stuck on / want to consult about

options:
  -h, --help     show this help message and exit
  --user USER    User ID
  --tier TIER    Tier to consult (public|source|private_<user>, default
                 public)
  --top-k TOP_K  Top K hits per kind (default 2)
```

### `am recall-auto`

```
usage: am recall-auto [-h] text

positional arguments:
  text        Error message or tool output (use "-" for stdin)

options:
  -h, --help  show this help message and exit
```

### `am relay`

```
usage: am relay [-h] {serve,register,add,ban,list,poll,lookup} ...

positional arguments:
  {serve,register,add,ban,list,poll,lookup}
    serve               Run the relay service
    register            Register this node with a relay
    add                 Add another relay this node can use
    ban                 Ban a node from the relay, or list and lift bans
    list                Show the relays this node uses, in order
    poll                Take what the relay has for us and answer it
    lookup              Check whether a peer is on the relay

options:
  -h, --help            show this help message and exit
```

### `am pending`

```
usage: am pending [-h] [--limit LIMIT] [--tier TIER] [--user USER]

options:
  -h, --help     show this help message and exit
  --limit LIMIT  How many to list (default 20)
  --tier TIER    Which tier to look in: all | public | private
  --user USER    Whose private tier to look in (default admin)
```

### `am retired`

```
usage: am retired [-h] [--limit LIMIT] [--tier TIER] [--user USER]

options:
  -h, --help     show this help message and exit
  --limit LIMIT  How many to list (default 20)
  --tier TIER    Which tier to look in: all | public | private
  --user USER    Whose private tier to look in (default admin)
```

### `am reactivate`

```
usage: am reactivate [-h] [--reason REASON] fact_id

positional arguments:
  fact_id          Fact id

options:
  -h, --help       show this help message and exit
  --reason REASON  Why it is being restored
```

### `am confirm`

```
usage: am confirm [-h] [--force] [--kind KIND] [--tier TIER] [--user USER]
                  [--llm]
                  candidate_id

positional arguments:
  candidate_id  Candidate id

options:
  -h, --help    show this help message and exit
  --force       Required: publishes the fact as commons
  --kind KIND   Kind to publish as (public only holds
                lessons/methods/workflows/models/patterns)
  --tier TIER   Which tier to look in: all | public | private
  --user USER   Whose private tier to look in (default admin)
  --llm         When the content reads as private, ask the LLM to abstract the
                method out of it and publish the abstraction instead. The
                rules judge the proposal; a proposal that still reads as
                private is refused.
```

### `am agent`

```
usage: am agent [-h] [--provider PROVIDER] [--model MODEL] [--list]
                [--remove REMOVE]
                [name]

positional arguments:
  name                 Agent name, e.g. hermes

options:
  -h, --help           show this help message and exit
  --provider PROVIDER  minimax | openai | openrouter | custom
  --model MODEL        Model name, e.g. MiniMax-M3.1-Flash-Preview
  --list               Show the table
  --remove REMOVE      Remove an agent from the table
```

### `am reject`

```
usage: am reject [-h] [--reason REASON] [--tier TIER] [--user USER]
                 candidate_id

positional arguments:
  candidate_id     Candidate id

options:
  -h, --help       show this help message and exit
  --reason REASON  Why it is dropped
  --tier TIER      Which tier to look in: all | public | private
  --user USER      Whose private tier to look in (default admin)
```

### `am peer`

```
usage: am peer [-h]
               {open-search,connect,read,status,init,sign,verify,friend,trust,audit,health,quarantine,blacklist,export,export-config,import-config,import,rekey,rekey-apply,rekey-log,rekey-accept,rekey-reject,rekey-pending,endpoint,send-rekey,send-topic,allow-search,unallow-search,endpoints,test,connect-mode,relay-key,relay-status,outbound-search,fanout,rl,search,topic}
               ...

positional arguments:
  {open-search,connect,read,status,init,sign,verify,friend,trust,audit,health,quarantine,blacklist,export,export-config,import-config,import,rekey,rekey-apply,rekey-log,rekey-accept,rekey-reject,rekey-pending,endpoint,send-rekey,send-topic,allow-search,unallow-search,endpoints,test,connect-mode,relay-key,relay-status,outbound-search,fanout,rl,search,topic}
    open-search         Open or close public-tier search for signature-
                        verified peers
    connect             Meet a node that cannot reach you: read its identity
                        and record it
    read                Search a peer's public tier (read-only, signed)
    status              Show this peer identity (peer_id + key + DB path)
    init                Initialize peer identity if not yet present
                        (idempotent)
    sign                Sign a payload (for testing)
    verify              Verify a signature against a public key
    friend              Manage friend list (Phase 2)
    trust               Update trust score for a peer
    audit               Show audit log feed for a peer (Ship I)
    health              Show health status for a peer (Ship J)
    quarantine          Quarantine a peer (Ship L) -- exclude from fanout, set
                        trust=0
    blacklist           Blacklist a peer (trust=0)
    export              Export social_graph to YAML for backup
    export-config       Full peer-config export (Ship M) - v1.1 bundle with
                        topics + quarantine + rate limits
    import-config       Full peer-config import (Ship M) - v1.0 or v1.1 bundle
    import              Import social_graph from YAML bundle
    rekey               Build a signed REKEY message (announces peer_id
                        change)
    rekey-apply         Apply a received REKEY message
    rekey-log           Show REKEY message audit log
    rekey-accept        Accept a pending rekey by id (Ship N)
    rekey-reject        Reject a pending rekey by id (Ship N)
    rekey-pending       List pending rekey requests awaiting confirmation
                        (Ship N)
    endpoint            Manage peer HTTP endpoint (Phase 3 transport)
    send-rekey          Build + POST signed REKEY msg to a peer (Phase 3
                        transport)
    send-topic          Build + POST my topic_index to a peer (Phase 3
                        transport)
    allow-search        Mark a friend as allowing my searches (opt-in)
    unallow-search      Revoke search permission for a friend
    endpoints           List or replace the addresses for a peer
    test                Run a real search against a peer and say where it
                        stopped
    connect-mode        Put a peer in direct or relay mode
    relay-key           Record a peer's encryption key
    relay-status        Show the relay state of every peer
    outbound-search     Set whether I may search a peer (default: yes)
    fanout              Auto peer-search on a local miss (on by default)
    rl                  PPS per-peer rate-limit admin: status / reset /
                        rebuild
    search              Demand-driven search across friends (read-only, no DB
                        write)
    topic               Manage topic_index for soft routing (S1)

options:
  -h, --help            show this help message and exit
```

### `am platform`

```
usage: am platform [-h]
                   {list,list-users,list-bindings,resolve,token-get,token-set,bind,unbind,add-user,set-plan,verify}
                   ...

positional arguments:
  {list,list-users,list-bindings,resolve,token-get,token-set,bind,unbind,add-user,set-plan,verify}
    list                List platforms
    list-users          List user_meta rows
    list-bindings       List bindings (active only)
    resolve             Resolve a chat_id -> user_id
    token-get           Print platform token (audit row written)
    token-set           Update token for an existing platform row
    bind                Create binding (chat_id -> user_id)
    unbind              Revoke a binding by binding_id or chat_id
    add-user            Add user_meta row (and 9-db layout)
    set-plan            Update a user's subscription_plan (admin only)
    verify              Verify all 6 invariants on bot-binding.db

options:
  -h, --help            show this help message and exit
```
