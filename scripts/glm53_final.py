"""GLM 5.3 Flash final test: json_object + strong system prompt."""
import json
import urllib.request
import time
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
        for t in sess:
            if isinstance(t, dict) and t.get("text") and ("yesterday" in t.get("text","").lower() or "adoption" in t.get("text","").lower()):
                SAMPLE.append((sess_key, t["text"]))
                if len(SAMPLE) >= 3:
                    break
        if len(SAMPLE) >= 3:
            break

# The KEY: tell GLM that facts is a JSON object with array value
PROMPT = """You are an atomic fact extractor. Extract facts from the conversation turn.

Output format: a JSON OBJECT with key "facts" containing a JSON ARRAY. Each array item:
{{"content": "fact text", "event_date": "YYYY-MM-DD or null", "event_date_precision": "day|month|year|none"}}

Example output:
{{"facts": [{{"content": "The user attended a meeting yesterday.", "event_date": "2023-05-07", "event_date_precision": "day"}}]}}

Turn ({sess}):
{text}

Output JSON object now:"""


def call(model: str, sess: str, text: str):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": PROMPT.format(sess=sess, text=text)}],
        "max_tokens": 500,
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }
    t0 = time.time()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            r = json.loads(resp.read().decode("utf-8"))
            elapsed = (time.time() - t0) * 1000
            msg = r["choices"][0]["message"]
            content = msg.get("content", "")
            usage = r.get("usage", {})
            return {
                "ok": True,
                "content": content,
                "elapsed_ms": elapsed,
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "cost": usage.get("cost", 0),
            }
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


for sess, turn in SAMPLE:
    print(f"\n{'=' * 70}")
    print(f"{sess} | {turn[:90]}")
    print("=" * 70)
    for model in ["google/gemini-3.7-flash", "z-ai/glm-5.3-flash"]:
        r = call(model, sess, turn)
        if r["ok"]:
            try:
                # Try parse as object with 'facts' key
                obj = json.loads(r["content"])
                if isinstance(obj, dict) and "facts" in obj:
                    facts = obj["facts"]
                    print(f"\n[{model}] OK | {r['elapsed_ms']:.0f}ms | {r['output_tokens']}t out | ${r['cost']*1e6:.2f}μ")
                    print(f"  {len(facts)} facts:")
                    for f in facts[:3]:
                        print(f"    - {f.get('content','')[:80]} (date={f.get('event_date')})")
                elif isinstance(obj, list):
                    print(f"\n[{model}] OK (returned list directly) | {r['elapsed_ms']:.0f}ms")
                    for f in obj[:3]:
                        print(f"    - {f.get('content','')[:80]}")
                else:
                    print(f"\n[{model}] OK but UNEXPECTED format: {str(obj)[:200]}")
            except Exception as e:
                print(f"\n[{model}] JSON parse FAIL: {e}")
                print(f"  raw: {r['content'][:200]}")
        else:
            print(f"\n[{model}] ERROR: {r.get('error','')[:200]}")