"""_regression_check.py — detect eval regression vs last baseline, alert via Telegram, optionally auto-rollback ASTOR_BM25_WEIGHT.

Companion to astor_eval_weekly_wrapper.bat (cron astor-eval-weekly).
Reads $ASTOR_METRICS_DIR/eval_history.jsonl (default <repo>/astor/metrics),
compares latest 'baseline' run to previous 'baseline' run,
alerts if hit_rate dropped > 0.05 OR mrr dropped > 0.05.

S3 (2026-09-08): added auto-rollback. If delta_mrr < -0.05 AND ASTOR_BM25_WEIGHT is not already at the safe default (0.4),
write ASTOR_BM25_WEIGHT=0.4 to start_server.bat + start_astor.sh, alert user.
Env: ASTOR_PROJECT_ROOT (default cwd), ASTOR_METRICS_DIR (default <repo>/astor/metrics),
HERMES_ENV (default ~/AppData/Local/hermes/.env).
"""
from __future__ import annotations
import json
import os
import re
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("ASTOR_PROJECT_ROOT", "."))
HISTORY = Path(os.environ.get("ASTOR_METRICS_DIR", str(ROOT / "astor" / "metrics"))) / "eval_history.jsonl"
# S15 (2026-09-08): last-good BM25 weight is read from eval_history.jsonl (most recent
# baseline run's kwargs.bm25_weight). This way rollback restores the previously-known-good
# config instead of hardcoded 0.4 (which was wrong vs v1.14.5 ship default 0.6).
SAFE_BM25_FALLBACK = "0.6"  # v1.14.5 default; only used if history is unreadable

TELEGRAM_TOKEN = None
TELEGRAM_CHAT = None
try:
    env_path = Path(os.environ.get("HERMES_ENV", str(Path.home() / "AppData/Local/hermes/.env")))
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
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

_START_FILES = [
    Path(os.environ.get("ASTOR_RUNTIME_BIN", "<runtime>/bin") + "/start_server.bat"),  # S21: generic fallback, set ASTOR_RUNTIME_BIN env var for actual path
    Path(os.environ.get("HERMES_SCRIPTS", "<hermes_home>/AppData/Local/hermes/scripts") + "/start_astor.sh"),  # S4: generic fallback; env var in production
]

def detect_bm25_weight() -> str | None:
    """Read current ASTOR_BM25_WEIGHT from runtime start_server.bat or start_astor.sh.
    Returns string or None if neither file has it set.
    """
    for f in _START_FILES:
        if f.exists():
            text = f.read_text(encoding="utf-8", errors="ignore")
            m = re.search(r"ASTOR_BM25_WEIGHT=(\d+\.\d+)", text)
            if m:
                return m.group(1)
    return None

def rollback_bm25(weight: str = SAFE_BM25_FALLBACK) -> int:
    """Write ASTOR_BM25_WEIGHT=<weight> to runtime start_server.bat + start_astor.sh.
    Returns count of files patched.
    """
    files_patched = 0
    for f in _START_FILES:
        if f.exists():
            text = f.read_text(encoding="utf-8", errors="ignore")
            new = re.sub(r"ASTOR_BM25_WEIGHT=\d+\.\d+", f"ASTOR_BM25_WEIGHT={weight}", text)
            if new != text:
                f.write_text(new, encoding="utf-8")
                files_patched += 1
    return files_patched


def _last_good_bm25_weight() -> str:
    """Read most recent baseline kwargs.bm25_weight from eval_history.jsonl.
    Falls back to SAFE_BM25_FALLBACK (v1.14.5 ship default 0.6) if no history.
    """
    if not HISTORY.exists():
        return SAFE_BM25_FALLBACK
    try:
        lines = [json.loads(l) for l in HISTORY.read_text(encoding="utf-8").splitlines() if l.strip()]
    except (OSError, json.JSONDecodeError):
        return SAFE_BM25_FALLBACK
    # S15 fix: find the MOST RECENT baseline run that didn't regress (mrr >= 0.85).
    # Reading reverse means we always get the last-good, not the last-bad.
    for entry in reversed(lines):
        if entry.get("variant") != "baseline":
            continue
        kwargs = entry.get("kwargs", {})
        if "bm25_weight" not in kwargs:
            continue
        mrr = entry.get("mrr", 0.0)
        # Only accept this as last-good if its mrr is healthy
        if mrr >= 0.85:
            return str(kwargs["bm25_weight"])
    return SAFE_BM25_FALLBACK


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
        bm25 = detect_bm25_weight()
        msg = (
            f"⚠️ astor eval REGRESSION\n"
            f"hit_rate: {prev['hit_rate_at_k']:.3f} → {cur['hit_rate_at_k']:.3f} ({delta_hit:+.3f})\n"
            f"mrr:      {prev['mrr']:.3f} → {cur['mrr']:.3f} ({delta_mrr:+.3f})\n"
            f"current ASTOR_BM25_WEIGHT: {bm25}\n"
            f"ts: {cur['ts']}\n"
        )
        # S3: auto-rollback if BM25 weight was changed (not at safe default)
        rolled_back = False
        if bm25 and bm25 != _last_good_bm25_weight():
            last_good = _last_good_bm25_weight()
            n = rollback_bm25(last_good)
            if n > 0:
                msg += f"\n🔄 AUTO-ROLLBACK: ASTOR_BM25_WEIGHT {bm25} → {last_good} ({n} files patched)\n"
                msg += "Restart astor server (or wait for MemoryServersWatch cycle) to apply."
                rolled_back = True
            else:
                msg += f"\n⚠️ detected BM25={bm25} but rollback failed (no files patched)\n"
        msg += f"check: D:\\AI\\astor-memory\\astor\\metrics\\"
        send_telegram(msg)
        print(f"ALERT SENT{' + ROLLBACK' if rolled_back else ''}")
    else:
        print("no regression (>5% drop threshold)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
