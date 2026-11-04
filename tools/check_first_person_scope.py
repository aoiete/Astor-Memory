"""Measure the first-person signal's reach in the shared tier.

Reproduces the numbers in
`docs/design/first-person-options-c-and-d.md`:

  - how many shareable-kind facts the first-person signal blocks alone
  - which branch of the regex is responsible for each block
  - how many currently-personal facts each candidate rule would release

Read-only: it never writes to any database.

    python tools/check_first_person_scope.py
"""
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SRC))

from astor_memory.nest.visibility_classifier import (  # noqa: E402
    AUTO_COMMONS_KINDS, has_pii, has_emotion, has_geographic, has_first_person,
)

BRANCHES = [
    ("我 (singular)", re.compile(r'我(?!们|自己)')),
    ("我的", re.compile(r'我的')),
    ("我们 (plural)", re.compile(r'我们')),
    ("我自己", re.compile(r'我自己')),
    ("my", re.compile(r'\bmy\b')),
    ("our (plural)", re.compile(r'\bour\b')),
    ("mine", re.compile(r'\bmine\b')),
    ("us (plural)", re.compile(r'\bus\b')),
    ("me", re.compile(r'\bme\b')),
    ("家里", re.compile(r'家里')),
    ("family", re.compile(r'family')),
    ("home (bare substring)", re.compile(r'home')),
    ("home address", re.compile(r'home address')),
]

# personal markers: possessive forms, private nouns, and stated user
# positions, in Chinese and English
MARKER = re.compile(
    r'我的|我自己|我们的|\bmy\b|\bmine\b|\bme\b|'
    r'家里|家人|家人生日|family|home address|'
    r'账户|余额|存款|工资|收入|资产|信用卡|贷款|投资组合|持仓|'
    r'老婆|老公|女朋友|男朋友|父母|父亲|母亲|爸爸|妈妈|孩子|女儿|儿子|'
    r'住址|地址|手机号|微信号|邮箱|身份证|银行卡|社保|户口|'
    r'用户主张|用户希望|用户偏好|用户要求|用户规定|用户不满|用户批评|'
    r'用户明确|用户强调|用户声明|用户拒绝|用户纠正|用户说|本人|'
    r'\baddress\b|\bpassword\b|\baccount\b|\bsalary\b|\bincome\b|'
    r'\bwife\b|\bhusband\b|\bfamily\b|\bdaughter\b|\bson\b'
)
PLURAL = re.compile(r'我们|\bour\b|\bus\b')
SINGULAR = re.compile(r'我(?!们|自己)|我的|我自己|\bmy\b|\bmine\b|\bme\b')
HOME_WORD = re.compile(r'\bhome\b')
HOME_PERSONAL = re.compile(r'\bmy home\b|\bhome address\b|家里')


def runtime_dir() -> Path:
    root = os.environ.get("ASTOR_DIR") or os.environ.get("ASTOR_DIR_NAME")
    if root:
        return Path(root)
    return Path.home() / ".astor"


def collect(runtime: Path):
    rows = []
    for db in sorted(runtime.glob("**/*.db")):
        try:
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            if not con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                               "AND name='memory_canonical'").fetchone():
                continue
            cols = {r[1] for r in con.execute("PRAGMA table_info(memory_canonical)")}
            if not {"content", "visibility"} <= cols:
                continue
            for kind, vis, content in con.execute(
                    "SELECT kind, visibility, content FROM memory_canonical"):
                if content:
                    rows.append((kind or "", vis or "", content))
        except sqlite3.Error:
            pass
        finally:
            con.close()
    return rows


def fp_effective_C(text: str) -> bool:
    """Would the first-person signal still block under rule C?"""
    if not has_first_person(text):
        return False
    if SINGULAR.search(text):
        return True
    if HOME_PERSONAL.search(text):
        return True
    if HOME_WORD.search(text):
        return False
    if PLURAL.search(text):
        return bool(MARKER.search(text))
    return True


def main() -> int:
    runtime = runtime_dir()
    if not runtime.exists():
        print(f"runtime not found: {runtime}")
        return 1
    rows = collect(runtime)
    shareable = [(k, c) for k, v, c in rows if k in AUTO_COMMONS_KINDS]
    personal = [c for _k, v, c in rows if v == "personal"]

    blocked_alone = []
    branch_counts = Counter()
    for _, content in shareable:
        if has_pii(content) or has_emotion(content) or has_geographic(content):
            continue
        if not has_first_person(content):
            continue
        blocked_alone.append(content)
        for name, rx in BRANCHES:
            if rx.search(content):
                branch_counts[name] += 1

    fp_only_personal = [c for c in personal if has_first_person(c)
                        and not (has_pii(c) or has_emotion(c)
                                 or has_geographic(c))]
    released_C = [c for c in fp_only_personal if not fp_effective_C(c)]

    print(f"runtime            : {runtime}")
    print(f"facts scanned      : {len(rows)}")
    print(f"shareable-kind     : {len(shareable)}")
    print(f"personal          : {len(personal)}")
    print()
    print(f"shareable facts blocked by first_person alone : {len(blocked_alone)}")
    print(f"personal facts blocked by first_person alone  : {len(fp_only_personal)}")
    print(f"rule C would release from the personal tier   : {len(released_C)}")
    print()
    print("branch distribution (a fact can hit several):")
    for name, _ in BRANCHES:
        n = branch_counts.get(name, 0)
        pct = (n / len(blocked_alone) * 100) if blocked_alone else 0.0
        print(f"  {name:<24}{n:>5}  {pct:5.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
