"""A/B test: Gemini 3.7 Flash vs GLM 5.3 Flash (handle reasoning field)."""
import os
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

SAMPLE = []
for sess_key, sess in data[0]["conversation"].items():
    if sess_key.startswith("session_") and not sess_key.endswith("_date_time") and isinstance(sess, list):
        for t in sess[:1]:
            if isinstance(t, dict) and t.get("text"):
                SAMPLE.append((sess_key, t["text"]))
                if len(SAMPLE) >= 3:
                    break
        if len(SAMPLE) >= 3:
            break

PROMPT = """Extract atomic facts from this conversation turn. Output ONLY a JSON list of facts.
Each fact: {{"content": "...", "event_date": "YYYY-MM-DD or null", "event_date_precision": "day|month|year|none"}}

Conversation turn ({sess}):
{text}

JSON list only, no markdown:"""


def call(model: str, sess: str, text: str) -> str:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": PROMPT.format(sess=sess, text=text)}],
        "max_tokens": 400,
        "temperature": 0.0,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        r = json.loads(resp.read().decode("utf-8"))
        msg = r["choices"][0]["message"]
        # GLM puts output in reasoning_details, others in content
        if msg.get("content"):
            return msg["content"]
        if msg.get("reasoning_details"):
            for d in msg["reasoning_details"]:
                if d.get("text"):
                    return d["text"]
        return f"NO CONTENT. full msg: {json.dumps(msg)[:300]}"


for sess, turn in SAMPLE:
    print(f"\n{'=' * 70}")
    print(f"{sess} | {turn[:90]}")
    print("=" * 70)
    for model in ["google/gemini-3.7-flash", "z-ai/glm-5.3-flash"]:
        print(f"\n[{model}]")
        try:
            out = call(model, sess, turn)
            print(f"  {out[:250]}")
        except Exception as e:
            print(f"  ERROR: {e}")