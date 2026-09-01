"""A/B: Gemini 3.7 Flash vs GLM 4.5 (with structured output) for atomic fact."""
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

# Pick 3 turns with relative time + entities (the real test)
SAMPLE = []
for sess_key, sess in data[0]["conversation"].items():
    if sess_key.startswith("session_") and not sess_key.endswith("_date_time") and isinstance(sess, list):
        for t in sess:
            if isinstance(t, dict) and t.get("text") and ("yesterday" in t.get("text","").lower() or "last week" in t.get("text","").lower() or "adoption" in t.get("text","").lower()):
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


def call(model: str, sess: str, text: str, use_schema: bool) -> str:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT.format(sess=sess, text=text)}],
        "max_tokens": 400,
        "temperature": 0.0,
    }
    if use_schema:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "facts", "strict": True, "schema": SCHEMA},
        }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        r = json.loads(resp.read().decode("utf-8"))
        msg = r["choices"][0]["message"]
        if msg.get("content"):
            return msg["content"]
        if msg.get("reasoning_details"):
            return "[reasoning] " + (msg["reasoning_details"][0].get("text", "") if msg["reasoning_details"] else "")
        return f"NO CONTENT. msg={json.dumps(msg)[:200]}"


for sess, turn in SAMPLE:
    print(f"\n{'=' * 70}")
    print(f"{sess} | {turn[:100]}")
    print("=" * 70)
    for model, schema in [
        ("google/gemini-3.7-flash", False),
        ("google/gemini-3.7-flash", True),
        ("z-ai/glm-4.5", True),
    ]:
        print(f"\n[{model} {'+schema' if schema else ''}]")
        try:
            out = call(model, sess, turn, use_schema=schema)
            print(f"  {out[:300]}")
        except Exception as e:
            print(f"  ERROR: {e}")