"""Debug GLM 5.3 Flash error."""
import json
import urllib.request
import time

with open(r"<home_dir>AppData\Local\hermes\.env", encoding="utf-8") as f:
    for line in f:
        if line.startswith("OPENROUTER_API_KEY="):
            KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
            break

# Test 4 configurations
configs = [
    {"label": "plain", "model": "z-ai/glm-5.3-flash", "extra": {}},
    {"label": "+reasoning=False", "model": "z-ai/glm-5.3-flash", "extra": {"reasoning": {"enabled": False}}},
    {"label": "+json_object", "model": "z-ai/glm-5.3-flash", "extra": {"response_format": {"type": "json_object"}}},
    {"label": "+json_schema", "model": "z-ai/glm-5.3-flash", "extra": {"response_format": {"type": "json_schema", "json_schema": {"name": "f", "schema": {"type": "array"}}}}},
    {"label": "schema+no_reasoning", "model": "z-ai/glm-5.3-flash", "extra": {"response_format": {"type": "json_schema", "json_schema": {"name": "f", "schema": {"type": "array"}}}, "reasoning": {"enabled": False}}},
]

text = "I went to a LGBTQ support group yesterday."
prompt = f"Output a JSON list. Each item: {{content: '...', date: 'YYYY-MM-DD or null'}}. Text: {text}"

for cfg in configs:
    body = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 200,
        "temperature": 0.0,
    }
    body.update(cfg["extra"])
    t0 = time.time()
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            r = json.loads(resp.read().decode("utf-8"))
            msg = r["choices"][0]["message"]
            content = msg.get("content", "") or "[reasoning-only]"
            print(f"\n[{cfg['label']}] OK ({time.time()-t0:.1f}s)")
            print(f"  raw: {content[:150]}")
            try:
                parsed = json.loads(content)
                print(f"  JSON OK: {len(parsed) if isinstance(parsed, list) else parsed}")
            except Exception as e:
                print(f"  JSON parse FAIL: {e}")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")[:400]
        print(f"\n[{cfg['label']}] HTTP {e.code}: {err_body}")
    except Exception as e:
        print(f"\n[{cfg['label']}] ERROR: {e}")