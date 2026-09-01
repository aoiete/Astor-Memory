"""Debug: why are 70% of Gemini extractions returning empty?"""
import json
import urllib.request
import os
import re
import time
from pathlib import Path

with open(r"<home_dir>AppData\Local\hermes\.env", encoding="utf-8") as f:
    for line in f:
        if line.startswith("OPENROUTER_API_KEY="):
            KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
            break

DATASET = Path(r"D:\AI\agent-memory-benchmark-ll\.datasets\locomo\locomo10.json")
data = json.loads(DATASET.read_text(encoding="utf-8"))

# Find a few short, simple-looking turns that are likely to fail
SHORT_TURNS = []
for sess_key, sess in data[0]["conversation"].items():
    if sess_key.startswith("session_") and not sess_key.endswith("_date_time") and isinstance(sess, list):
        for t in sess:
            if isinstance(t, dict) and t.get("text") and len(t.get("text", "")) < 50:
                SHORT_TURNS.append((sess_key, t["text"], t.get("speaker", "")))
                if len(SHORT_TURNS) >= 5:
                    break
        if len(SHORT_TURNS) >= 5:
            break


def call(text, speaker, anchor):
    turn = f"{speaker}: {text}" if speaker else text
    prompt = (
        'You are an atomic fact extractor. Extract 1-3 atomic facts.\n'
        'Output a JSON list. Each: {"content": "...", "event_date": "YYYY-MM-DD or null", '
        '"event_date_precision": "day|month|year|none", "kind": "fact|preference|state"}\n'
        f'Relative dates against anchor={anchor}. No date -> {anchor} day.\n\n'
        f'Turn: {turn[:400]}'
    )
    body = json.dumps({
        "model": "google/gemini-3.7-flash",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 500,
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            r = json.loads(resp.read().decode("utf-8"))
            content = r["choices"][0]["message"].get("content", "")
            print(f"  RAW: {content[:300]}")
            return content
    except Exception as e:
        print(f"  ERROR: {e}")
        return None


for sess, text, speaker in SHORT_TURNS:
    print(f"\n--- {sess} ---")
    print(f"  [{speaker}] {text}")
    call(text, speaker, "2023-05-08")
    time.sleep(0.5)