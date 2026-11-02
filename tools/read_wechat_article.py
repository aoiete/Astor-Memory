"""Fetch a WeChat article the way that works (locked 2026-09-13).

The method, in order:

  1. curl with a Chrome UA, download the whole HTML
  2. unescape the JS-escaped body (\\n -> newline, \\" -> quote)
  3. pull the js_content div with a regex
  4. strip tags (span leaf=, br, img alt=)

It does not need browser-act or Perplexity.

Usage:
    D:/AI/PY-311/Scripts/python.exe tools/read_wechat_article.py <url> [out.txt]
"""
from __future__ import annotations

import html
import re
import subprocess
import sys
from pathlib import Path

CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def fetch(url: str) -> str:
    out = Path(__file__).resolve().parent.parent / ".tmp_wechat.html"
    subprocess.run(
        ["curl", "-sL", "--max-time", "60", "-A", CHROME_UA, "-o", str(out), url],
        check=False, capture_output=True)
    raw = out.read_text(encoding="utf-8", errors="replace")
    try:
        out.unlink()
    except OSError:
        pass
    return raw


def extract(raw: str) -> tuple[str, str]:
    title = ""
    m = re.search(r"<h1[^>]*class=\"rich_media_title\"[^>]*>(.*?)</h1>", raw, re.S)
    if not m:
        m = re.search(r"var msg_title\s*=\s*['\"](.*?)['\"]", raw, re.S)
    if m:
        title = re.sub(r"<[^>]+>", "", m.group(1)).strip()

    body = raw
    m = re.search(r'<div[^>]*id="js_content"[^>]*>(.*?)</div>\s*<script', raw, re.S)
    if not m:
        m = re.search(r'<div[^>]*id="js_content"[^>]*>(.*)', raw, re.S)
    if m:
        body = m.group(1)

    # the body arrives JS-escaped in places
    body = body.replace("\\n", "\n").replace('\\"', '"').replace("\\/", "/")
    body = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), body)
    body = html.unescape(body)

    body = re.sub(r"<span[^>]*leaf[^>]*>", "", body)
    body = re.sub(r"</?span[^>]*>", "", body)
    body = re.sub(r"<br\s*/?>", "\n", body)
    body = re.sub(r"<img[^>]*alt=\"([^\"]*)\"[^>]*>", r"\n[图: \1]\n", body)
    body = re.sub(r"<img[^>]*>", "\n[图]\n", body)
    body = re.sub(r"</p>", "\n", body)
    body = re.sub(r"<[^>]+>", "", body)
    body = re.sub(r"[ \t\xa0]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body)
    return title, body.strip()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    url = sys.argv[1]
    raw = fetch(url)
    if len(raw) < 5000:
        print(f"  fetch 太短 ({len(raw)} 字节) — 可能被拦")
        print(raw[:400])
        return 1
    title, body = extract(raw)
    print(f"  标题: {title}")
    print(f"  正文: {len(body)} 字")
    print("-" * 70)
    print(body)
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
        print(f"\n  已存: {sys.argv[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
