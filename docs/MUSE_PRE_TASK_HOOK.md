# Muse ↔ Astor Skill — Pre-Task Recall Hook

**Version**: 1.0 (2026-09-30)
**Purpose**: Make Muse always check astor for success/failure patterns BEFORE trying to fetch URLs.

---

## The Problem

Muse (and most AI agents) hit a `mp.weixin.qq.com` URL and:
1. Try `requests.get(url)` → blocked by anti-bot
2. Try `requests.get(url, headers={...})` → still blocked
3. Try playwright/selenium → too slow
4. Spend 5-10 turns guessing, then give up

The correct path (locked in astor fact **6215**) is:
> "**任何'读 X / 抓 X / 处理 X'类型任务 — 第 1 步必须是 astor_recall 查成功模式, 不是凭记忆猜路径 / 盲目试工具**"

---

## Paste this into Muse's pre-task hook

```python
import requests
from functools import wraps

ASTOR_BASE = "https://astor.flopworld.com"
PLATFORM = "muse"

def astor_recall(query: str, top_k: int = 5, chat_id: str = "") -> list[dict]:
    """
    Universal recall from astor. Used by Muse as PRE-TASK hook
    for any fetch-tool / URL-handling task.

    Returns: list of relevant facts/experiences ranked by relevance.
    """
    # Step 1: Resolve caller identity (for tier + user_id)
    who = requests.get(f"{ASTOR_BASE}/v1/binding/lookup",
                       params={"platform": PLATFORM, "chat_id": chat_id or "anonymous"},
                       timeout=5)
    who = who.json() if who.status_code == 200 else {
        "default_tier": "public", "user_id": "anonymous"
    }

    # Step 2: Recall (kw + ECV + path_score ranked)
    r = requests.post(f"{ASTOR_BASE}/v1/read", json={
        "query": query,
        "tier": who["default_tier"],
        "user_id": who["user_id"],
        "top_k": top_k,
    }, timeout=15)
    if r.status_code != 200:
        return []
    return r.json().get("results", [])


def astor_recall_experiences(query: str, top_k: int = 5, chat_id: str = "") -> list[dict]:
    """Recall from memory_experience (success_pattern/failure_pattern)."""
    who = requests.get(f"{ASTOR_BASE}/v1/binding/lookup",
                       params={"platform": PLATFORM, "chat_id": chat_id or "anonymous"},
                       timeout=5)
    who = who.json() if who.status_code == 200 else {
        "default_tier": "public", "user_id": "anonymous"
    }
    r = requests.post(f"{ASTOR_BASE}/v1/experience/match", json={
        "query": query,
        "tier": who["default_tier"],
        "top_k": top_k,
    }, timeout=15)
    if r.status_code != 200:
        return []
    return r.json().get("results", [])


# ---------------------------------------------------------------------------
# PRE-TASK HOOK — wrap any fetch-tool call
# ---------------------------------------------------------------------------

URL_PATTERN_TRIGGERS = ["mp.weixin.qq.com", "weixin.qq.com", "公众号", "wechat article",
                         "url", "http://", "https://", "fetch", "scrape", "read article"]

def is_fetch_task(user_message: str) -> bool:
    """Detect if Muse's incoming task is a fetch/URL task."""
    msg = user_message.lower()
    return any(t in msg for t in URL_PATTERN_TRIGGERS)


def pre_task_recall_hook(user_message: str, chat_id: str = "") -> dict:
    """
    Run BEFORE any fetch-tool call. Returns the success patterns /
    failure patterns / user preferences that should guide the task.

    Muse agent MUST inject this output into its prompt before
    deciding which tool to call.
    """
    if not is_fetch_task(user_message):
        return {"enabled": False}

    query = user_message[:200]
    facts = astor_recall(query, top_k=3, chat_id=chat_id)
    experiences = astor_recall_experiences(query, top_k=3, chat_id=chat_id)

    # Extract actionable guidance
    guidance = {
        "enabled": True,
        "query": query,
        "relevant_facts": [
            f["content"] for f in facts[:3]
            if f.get("content") and len(f["content"]) < 500
        ],
        "success_patterns": [
            e["action_summary"] for e in experiences
            if e.get("outcome") == "success_pattern"
        ],
        "failure_patterns": [
            e["action_summary"] for e in experiences
            if e.get("outcome") in ("failure_pattern", "lesson", "correction")
        ],
    }
    return guidance


# ---------------------------------------------------------------------------
# USE in Muse's main loop
# ---------------------------------------------------------------------------

def process_user_message(user_message: str, chat_id: str = "") -> str:
    """Muse's main handler — recall BEFORE doing anything else."""

    # Step 1: PRE-TASK RECALL — get astor's stored patterns
    guidance = pre_task_recall_hook(user_message, chat_id)

    if guidance.get("enabled"):
        # Inject guidance into Muse's prompt
        prompt_addendum = f"""

[astor memory hint — DO THIS FIRST before any fetch tool]
Relevant facts ({len(guidance['relevant_facts'])}):
{chr(10).join('- ' + f for f in guidance['relevant_facts'])}

Success patterns that worked before:
{chr(10).join('- ' + f for f in guidance['success_patterns'])}

Failure patterns / lessons:
{chr(10).join('- ' + f for f in guidance['failure_patterns'])}

If success patterns exist, USE THEM DIRECTLY (don't re-discover).
If failure patterns exist, AVOID those approaches.
"""
        # Now Muse processes the user_message + prompt_addendum together
        # ... Muse's existing LLM call goes here ...
        # Muse's prompt MUST include the prompt_addendum
        pass

    # Step 2: After task succeeds/fails, save pattern back to astor
    # (Muse's existing handler decides outcome, then calls astor_save_pattern)
```

---

## Example: how Muse uses this for "muse 看微信文章"

```python
# User says to Muse: "看看这篇微信文章 https://mp.weixin.qq.com/s/RmbEJ28DNQ4bI-olYTX5mA"
msg = "看看这篇微信文章 https://mp.weixin.qq.com/s/RmbEJ28DNQ4bI-olYTX5mA"
guidance = pre_task_recall_hook(msg, chat_id=CHAT_ID)

# guidance = {
#     "enabled": True,
#     "relevant_facts": [
#       "触发条件: 用户消息含 mp.weixin|weixin|公众号 时, agent 第一动作必须是 astor_recall(查 success_pattern/failure_pattern)",
#       "微信公众号文章: curl + Chrome UA + Referer header + js_content regex"
#     ],
#     "success_patterns": [
#       "wechat 文章 mp.weixin.qq.com/s/... 读取方法 (locked 2026-09-13): curl + Chrome UA + Referer",
#       "wechat 公众号 = curl + Chrome UA + js_content extract 模式"
#     ],
#     "failure_patterns": [
#       "web_extract 被反爬 阻断 (server side UA check)"
#     ]
# }

# Muse now knows: use curl + Chrome UA, not web_extract
# Implements it correctly the first time.
```

---

## Auto-save pattern back (Muse writes back to astor)

```python
def astor_save_pattern(outcome: str, action_summary: str, trigger_keywords: list[str],
                        importance: float = 0.7, chat_id: str = ""):
    """Save a success/failure pattern back to astor. Muse calls this
    after every fetch task succeeds or fails."""
    who = requests.get(f"{ASTOR_BASE}/v1/binding/lookup",
                       params={"platform": PLATFORM, "chat_id": chat_id or "anonymous"},
                       timeout=5)
    who = who.json() if who.status_code == 200 else {
        "default_tier": "public", "user_id": "anonymous"
    }
    r = requests.post(f"{ASTOR_BASE}/v1/experience", json={
        "outcome": outcome,                              # success_pattern/failure_pattern
        "action_summary": action_summary,                # 1-line description
        "trigger_keywords": trigger_keywords,             # ["wechat", "mp.weixin.qq.com"]
        "importance": importance,
        "tier": who["default_tier"],
        "user_id": who["user_id"],
    }, timeout=15)
    return r.json()

# After a successful fetch:
astor_save_pattern(
    outcome="success_pattern",
    action_summary="muse fetched wechat article with curl + Chrome UA + js_content regex, 1450 chars extracted",
    trigger_keywords=["muse", "wechat", "fetch", "mp.weixin.qq.com"],
    importance=0.85,
    chat_id=CHAT_ID,
)

# After a failed attempt:
astor_save_pattern(
    outcome="failure_pattern",
    action_summary="muse tried web_extract on mp.weixin.qq.com — anti-bot blocked, don't retry",
    trigger_keywords=["muse", "web_extract", "wechat", "anti_bot"],
    importance=0.9,
    chat_id=CHAT_ID,
)
```

---

## Why this works

1. **Astor is SSoT** for success/failure patterns (locked fact 6215)
2. **Muse reads BEFORE fetching** — the locked rule finally applies to Muse
3. **Muse writes back** — the bus grows with each successful/failed task
4. **Self-improving loop** — Muse gets smarter every fetch task
