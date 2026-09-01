import os
import urllib.request
import json

with open(r"<home_dir>AppData\Local\hermes\.env", encoding="utf-8") as f:
    for line in f:
        if line.startswith("MINIMAX_API_KEY="):
            key = line.split("=", 1)[1].strip().strip('"').strip("'")
            break

body = json.dumps({
    "model": "MiniMax-M3",
    "messages": [{"role": "user", "content":
        "Answer in 1-2 short sentences: If recall returns ONLY "
        "'Caroline: Yeah, I'm really lucky to have them. They've been there "
        "through everything, I've known these friends for four years' "
        "to query 'How long has Caroline had her current group of friends?', "
        "can an LLM find '4 years'? Why or why not?"
    }],
    "max_tokens": 200,
}).encode("utf-8")

req = urllib.request.Request(
    "https://api.minimax.io/v1/chat/completions",
    data=body,
    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=15) as resp:
    r = json.loads(resp.read().decode("utf-8"))
print(r["choices"][0]["message"]["content"])