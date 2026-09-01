"""Test GLM 5.3 Flash with reasoning disabled + strict JSON schema."""
import json
import urllib.request
from pathlib import Path

with open(r"<home_dir>AppData\Local\hermes\.env", encoding="utf-8") as f:
    for line in f:
        if line.startswith("OPENROUTER_API_KEY="):
            KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
            break

DATASET = Path(r"D:\AI\agent-memory-benchmark-ll\.datasets\locomo\locomo10.json")
data = json.loads(DATASET.read_text(encoding="utf-8"))

# 3 turns with key signals
SAMPLE = []
for sess_key, sess in data[0]["conversation"].items():
    if sess_key.startswith("session_") and not sess_key.endswith("_date_time") and isinstance(sess, list):
        for t in sess:
            if isinstance(t, dict) and t.get("text") and ("yesterday" in t.get("text","").lower() or "adoption" in t.get("text","").lower() or "last week" in t.get("text","").lower()):
                SAMPLE.append((sess_key, t["text"]))
                if len(SAMPLE) >= 3:
                    break
        if len(SAMPLE) >= 3:
            break

PROMPT = """Extract atomic facts from this conversation turn. Output a JSON list of facts.
Each fact: {{"content": "...", "event_date": "YYYY-MM-DD or null", "event_date_precision": "day|month|year|none"}}

Turn ({sess}):
{text}"""

SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "content": {"type": "string"},
            "event_date": {"type": ["string", "null"]},
            "event_date_precision": {"type": "string", "enum": ["day", "month", "year", "none"]},
        },
        "required": ["content", "event_date", "event_date_precision"],
    },
}


def call(model: str, sess: str, text: str) -> dict:
    """Returns dict with content, ok, latency, error."""
    import time
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT.format(sess=sess, text=text)}],
        "max_tokens": 400,
        "temperature": 0.0,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "facts", "strict": True, "schema": SCHEMA},
        },
    }
    if "glm" in model:
        # GLM models need explicit reasoning disabled
        body["reasoning"] = {"enabled": False}

    t0 = time.time()
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            r = json.loads(resp.read().decode("utf-8"))
            elapsed = (time.time() - t0) * 1000
            msg = r["choices"][0]["message"]
            content = msg.get("content", "")
            if not content and msg.get("reasoning_details"):
                content = "[reasoning] " + (msg["reasoning_details"][0].get("text", "") if msg["reasoning_details"] else "")
            usage = r.get("usage", {})
            return {
                "ok": True,
                "content": content,
                "elapsed_ms": elapsed,
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "cost": usage.get("cost", 0),
            }
    except Exception as e:
        return {"ok": False, "error": str(e)}


for sess, turn in SAMPLE:
    print(f"\n{'=' * 70}")
    print(f"{sess} | {turn[:100]}")
    print("=" * 70)
    for model in ["google/gemini-3.7-flash", "z-ai/glm-5.3-flash"]:
        r = call(model, sess, turn)
        if r["ok"]:
            # Try to parse JSON
            try:
                parsed = json.loads(r["content"])
                print(f"\n[{model}] OK | {r['elapsed_ms']:.0f}ms | {r['output_tokens']}t out | ${r['cost']*1e6:.2f}μ")
                print(f"  {len(parsed)} facts:")
                for f in parsed[:3]:
                    print(f"    - {f.get('content','')[:80]} (date={f.get('event_date')})")
            except (json.JSONDecodeError, TypeError):
                print(f"\n[{model}] JSON parse FAIL | {r['elapsed_ms']:.0f}ms")
                print(f"  raw: {r['content'][:200]}")
        else:
            print(f"\n[{model}] ERROR: {r.get('error','?')[:200]}")