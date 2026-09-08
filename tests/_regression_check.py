"""_regression_check.py — detect eval regression vs last baseline.

Companion to astor_eval_weekly_wrapper.bat (cron astor-eval-weekly).
Reads D:\AI\astor-memory\astor\metrics\eval_history.jsonl,
compares latest 'baseline' run to previous 'baseline' run,
prints alert if hit_rate dropped > 0.05 OR mrr dropped > 0.05.
"""
from __future__ import annotations
import json
import os
import urllib.request
from pathlib import Path

HISTORY = Path(r"D:\AI\astor-memory\astor\metrics\eval_history.jsonl")
TELEGRAM_TOKEN = None
TELEGRAM_CHAT = None
try:
    # load .env lightly
    env = Path(r"C:\Users\TheNuts\AppData\Local\hermes\.env")
    if env.exists():
        for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                TELEGRAM_TOKEN = line.split("=", 1)[1].strip().strip('"\'')
            elif line.startswith("TELEGRAM_ADMIN_CHAT_ID="):
                TELEGRAM_CHAT = line.split("=", 1)[1].strip().strip('"\'')
except Exception:
    pass

def send_telegram(text: str) -> None:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT):
        print(f"[no-telegram-credentials] {text}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        urllib.request.urlopen(urllib.request.Request(
            url, data=json.dumps({"chat_id": TELEGRAM_CHAT, "text": text}).encode(),
            headers={"Content-Type": "application/json"}), timeout=10).read()
        print(f"[telegram-sent] {text[:60]}...")
    except Exception as e:
        print(f"[telegram-err] {e}: {text[:60]}...")

def main() -> int:
    if not HISTORY.exists():
        print("no eval_history.jsonl yet; nothing to compare")
        return 0
    lines = [json.loads(l) for l in HISTORY.read_text(encoding="utf-8").splitlines() if l.strip()]
    baseline_runs = [l for l in lines if l.get("variant") == "baseline"]
    if len(baseline_runs) < 2:
        print(f"only {len(baseline_runs)} baseline run(s); need >=2 for regression check")
        return 0
    cur, prev = baseline_runs[-1], baseline_runs[-2]
    delta_hit = cur["hit_rate_at_k"] - prev["hit_rate_at_k"]
    delta_mrr = cur["mrr"] - prev["mrr"]
    print(f"baseline hit_rate: {prev['hit_rate_at_k']:.3f} -> {cur['hit_rate_at_k']:.3f}  (delta={delta_hit:+.3f})")
    print(f"baseline mrr:      {prev['mrr']:.3f} -> {cur['mrr']:.3f}  (delta={delta_mrr:+.3f})")
    if delta_hit <= -0.05 or delta_mrr <= -0.05:
        send_telegram(
            f"⚠️ astor eval REGRESSION\n"
            f"hit_rate: {prev['hit_rate_at_k']:.3f} → {cur['hit_rate_at_k']:.3f} ({delta_hit:+.3f})\n"
            f"mrr:      {prev['mrr']:.3f} → {cur['mrr']:.3f} ({delta_mrr:+.3f})\n"
            f"ts: {cur['ts']}\n"
            f"check: D:\\AI\\astor-memory\\astor\\metrics\\"
        )
        print("ALERT SENT")
    else:
        print("no regression (>5% drop threshold)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
