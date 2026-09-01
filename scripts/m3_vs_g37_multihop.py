"""M3 vs Gemini3.7 multi-hop diagnostic — small A/B to compare.

Test: given a multi-hop query + facts, which model picks the right
bridge fact. Costs a few cents either way.
"""
import os
import urllib.request
import json
from pathlib import Path

with open(r"<home_dir>AppData\Local\hermes\.env", encoding="utf-8") as f:
    keys = {}
    for line in f:
        if line.startswith("MINIMAX_API_KEY="):
            keys["M3"] = line.split("=", 1)[1].strip().strip('"').strip("'")
        if line.startswith("OPENROUTER_API_KEY="):
            keys["OPENROUTER"] = line.split("=", 1)[1].strip().strip('"').strip("'")


def call(model: str, key: str, prompt: str, system: str = None) -> str:
    base = "https://api.minimax.io/v1" if model == "M3" else "https://openrouter.ai/api/v1"
    body = {
        "model": "MiniMax-M3" if model == "M3" else "google/gemini-3.7-flash",
        "messages": ([{"role": "system", "content": system}] if system else []) +
                    [{"role": "user", "content": prompt}],
        "max_tokens": 200,
        "temperature": 0.0,
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        r = json.loads(resp.read().decode("utf-8"))
        return r["choices"][0]["message"]["content"]


FACTS = [
    "Caroline met Melanie at an LGBTQ support group in May 2023",
    "Melanie painted a lake sunrise in 2022",
    "Caroline attended an adoption council meeting on July 15 2023",
    "Melanie has a daughter named Lily who is 7 years old",
    "Caroline lives in San Francisco and works at Mozilla",
]
QUERY = "What did Caroline learn from Melanie about her daughter's art?"

prompt = (
    "Given these facts:\n"
    + "\n".join(f"- {f}" for f in FACTS)
    + f"\n\nQuestion: {QUERY}\n"
    "Which single fact most directly bridges the entities needed to answer? "
    "Reply with just the fact text."
)
system = "You are a precise fact retrieval assistant."

print("=" * 70)
print("QUERY:", QUERY)
print("=" * 70)
print()

# gold fact (multi-hop bridge): fact about Melanie's daughter's art
gold = "Melanie has a daughter named Lily who is 7 years old"
print(f"GOLD BRIDGE: {gold}\n")

for model, key in keys.items():
    print(f"--- {model} ---")
    try:
        ans = call(model, key, prompt, system)
        print(f"  Pick: {ans[:100]}")
        match = "PASS" if any(w in ans for w in ["Melanie has a daughter", "Lily", "daughter"]) else "MISS"
        print(f"  Result: {match}\n")
    except Exception as e:
        print(f"  ERROR: {e}\n")