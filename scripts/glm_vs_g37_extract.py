"""A/B test: Gemini 3.7 Flash vs GLM 5.3 Flash for LoCoMo atomic fact extraction."""
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

# Pick 5 conversational turns from conv-26
SAMPLE = []
for sess_key, sess in data[0]["conversation"].items():
    if sess_key.startswith("session_") and not sess_key.endswith("_date_time") and isinstance(sess, list):
        for t in sess[:2]:
            if isinstance(t, dict) and t.get("text"):
                SAMPLE.append(t["text"])
                if len(SAMPLE) >= 5:
                    break
        if len(SAMPLE) >= 5:
            break

PROMPT = """Extract atomic facts from this conversation turn. Output JSON list of facts.
Each fact: {"content": "...", "event_date": "YYYY-MM-DD or null", "event_date_precision": "day|month|year|none"}

Conversation turn:
{text}

JSON only:"""

SYSTEM = "You are a precise atomic fact extractor. Output strict JSON."


def call(model: str, text: str) -> str:
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": PROMPT.format(text=text)},
        ],
        "max_tokens": 300,
        "temperature": 0.0,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        r = json.loads(resp.read().decode("utf-8"))
        return r["choices"][0]["message"]["content"]


print("=" * 70)
for i, turn in enumerate(SAMPLE, 1):
    print(f"\n--- Turn {i} ---")
    print(f"INPUT: {turn[:90]}")
    for model in ["google/gemini-3.7-flash", "z-ai/glm-5.3-flash"]:
        print(f"\n[{model}]")
        try:
            out = call(model, turn)
            print(f"  {out[:200]}")
        except Exception as e:
            print(f"  ERROR: {e}")