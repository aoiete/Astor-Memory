"""End-to-end: can private data reach the public tier?

Three layers have to agree for a fact to land in the shared tier:

  1. the write path classifies it (classify_visibility)
  2. the store refuses tier='public' unless visibility='commons'
  3. the scrubber strips machine-local paths from whatever is promoted

Layer 2 is the structural guarantee. Layer 1 is where the PII gate
lives -- an id or an email has to make the classifier say personal,
and "override_allowed: False" means the admin cannot wave it through.

This drives the real HTTP endpoint, because a test that calls
promote_candidate directly bypasses layer 1 and proves nothing about
the path a peer or an agent actually takes.

Run with the project interpreter:
    D:/AI/PY-311/Scripts/python.exe tools/check_public_leak.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PRIVATE_PAYLOAD = (
    "排查的时候发现 C:\\Users\\TestUser\\AppData\\Local\\hermes\\config.yaml "
    "里有 wxid0000abcd1234@im.wechat，Telegram 1234567890，"
    "邮箱 someone@example.invalid"
)
METHOD_PAYLOAD = "下载页面到 scratch 文件，然后抽取 js_content div"

LEAKS = {
    "windows user path": "TestUser",
    "wechat user id": "o9cq",
    "telegram chat id": "1234567890",
    "real email": "someone@example.invalid",
}


def post(url: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body or b"{}")
        except Exception:
            return e.code, {"raw": body[:300].decode("utf-8", "replace")}


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="astor_leak_")
    os.environ["ASTOR_DIR"] = tmp
    port = 17899
    import subprocess
    import time
    srv = subprocess.Popen(
        [sys.executable, "-m", "astor_memory.server", "--host", "127.0.0.1",
         "--port", str(port)], cwd=str(ROOT),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            time.sleep(1)
            try:
                with urllib.request.urlopen(f"{base}/v1/health", timeout=3) as r:
                    if r.status == 200:
                        break
            except Exception:
                continue
        else:
            print("  FAIL: server 没起来")
            return 1

        # 1. the private payload, asking for the shared tier
        st, body = post(f"{base}/v1/write", {
            "text": PRIVATE_PAYLOAD, "kind": "fact",
            "tier": "public", "visibility_hint": "commons",
            "user_id": "admin", "namespace": "leaktest",
        })
        print(f"  写入(请求 commons) -> HTTP {st}")
        if st >= 400:
            print("    body:", str(body)[:220])

        # 2. what actually landed in the public tier
        db = Path(tmp) / "public" / "memory" / "astor_bus_public.db"
        con = sqlite3.connect(str(db))
        rows = con.execute(
            "SELECT id, content, visibility FROM memory_canonical "
            "WHERE tombstoned = 0").fetchall()
        con.close()
        print(f"  public 层有 {len(rows)} 条")

        leaked = []
        for _id, content, vis in rows:
            for name, needle in LEAKS.items():
                if needle in (content or ""):
                    leaked.append((name, content[:90]))
            print(f"    id={_id} visibility={vis} content={str(content)[:100]}")

        # 3. the method payload, for contrast: this one SHOULD be publishable
        st2, _ = post(f"{base}/v1/write", {
            "text": METHOD_PAYLOAD, "kind": "method",
            "tier": "public", "visibility_hint": "commons",
            "user_id": "admin", "namespace": "leaktest",
        })
        con = sqlite3.connect(str(db))
        methods = con.execute(
            "SELECT content FROM memory_canonical WHERE kind='method' "
            "AND tombstoned = 0").fetchall()
        con.close()
        print(f"  写入(方法 fact) -> HTTP {st2}, 落地 {len(methods)} 条")

        print()
        if leaked:
            print(f"  FAIL: {len(leaked)} 个标识符进了公共层")
            for n, c in leaked:
                print(f"    - {n}: {c}")
            return 1
        print("  PASS: 私有标识符没有进入公共层")
        return 0
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:
            srv.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
