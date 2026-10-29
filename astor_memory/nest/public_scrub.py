"""Make a fact portable before it goes into the shared tier.

The public tier is the layer peers read. A method fact belongs there --
"download the page to a scratch file, then extract the js_content div"
is true on every machine. The path it happens to use on one machine is
not, and a peer that searches the public tier learns it.

So the shared tier keeps the method and loses the machine.

The rules are structural, not a list of this operator's paths. A rule
that named `D:/AI` would be the same private data in a different file,
and it would not generalise to the next operator.

  C:\\Users\\someone\\AppData  ->  <home>\\AppData
  D:\\work\\project\\file.py    ->  <root>\\project\\file.py
  /home/someone/x             ->  <home>/x
  \\\\HOST\\share              ->  \\\\<host>\\share
  192.168.1.7                 ->  <private-host>

What stays: loopback (127.0.0.1 is a convention, not a location),
public URLs, and the tail of a path, which is usually the part that
carries the knowledge -- the filename, the project name, the shape of
the directory.
"""
from __future__ import annotations

import re

# One path segment: no whitespace, no separator, no character Windows
# forbids in a name.
_SEG = r'[^\s\\/:*?"<>|]+'

# A Windows absolute path: a drive letter, then one or more segments.
# Matching segment by segment is what lets the rule drop the drive and
# the top-level directory, and the user directory and the name in it.
#
# The lookbehind excludes a preceding LETTER rather than any word
# character, because a word character includes CJK: "…无D:/AI/users…"
# is a path, and \b would not fire between a Chinese character and a
# drive letter. A letter is what has to be excluded -- "https://" ends
# in "s://", and matching that would rewrite every URL in the corpus.
#
# The second lookbehind excludes the tail of a JSON escape. In a
# JSON document "\u5e8f" is one escaped character, and a colon right
# after it -- "顺序:\u68c0" -- reads as the letter "f", a colon and a
# separator. Matching that cuts the escape in half and the document
# stops being JSON.
_WIN_PATH = re.compile(
    r'(?<![A-Za-z])(?<![\\]u[0-9a-fA-F]{3})([A-Za-z]):([\\/])('
    + _SEG + r'(?:[\\/]' + _SEG + r')*)')
# A POSIX absolute path with a home directory in it. The lookbehind
# keeps it from matching a "/users/<name>" that is the tail of a path
# the Windows rule already rewrote -- "<root>/users/admin" is one path,
# not a POSIX home directory.
_POSIX_HOME = re.compile(
    r'(?<![\w.>])/(?:home|Users|users)/([A-Za-z0-9_.-]+)((?:/' + _SEG + r')*)')
# The owner of a path, wherever the path lives. This is how the scrub
# learns a name it can then remove from the parts of a fact that are
# not paths.
_USER_IN_PATH = re.compile(
    r'(?:(?<![A-Za-z])[A-Za-z]:[\\/]Users[\\/]'
    r'|(?<![\w.>])/(?:home|Users|users)/)([A-Za-z0-9_.-]+)', re.IGNORECASE)
# A UNC path: \\host\share\...
_UNC = re.compile(r'\\\\([A-Za-z0-9_.-]+)((?:\\[^\s\\]*)+)')
# A bare home path: "~/file". The POSIX rule below needs a user
# segment ("/home/<name>/...") and the Windows rule needs a drive
# letter, so a tilde path passed straight through -- and that is the
# shape the v1.17.5 leak was written in ("~/.tmp_wechat.html"). The
# one-time cleanup rewrote the row that was already there; the door
# could not catch a new one.
_TILDE_HOME = re.compile(r'(?<![\w.>~])~((?:/' + _SEG + r')+)')

# An MSYS path: /c/Users/someone/AppData. The first segment is a
# single letter -- the drive -- and the rest is the same path the
# Windows rule handles. Without this, a shell command recorded in a
# lesson keeps the operator's home directory.
_MSYS_PATH = re.compile(
    r'(?<![\w.:/])/([A-Za-z])((?:/' + _SEG + r')+)')
# Private-range IPv4. Loopback and 0.0.0.0 are conventions, not locations.
_PRIVATE_IP = re.compile(
    r"\b(?:(?:192\.168|10)\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b")

HOME = "<home>"
ROOT = "<root>"
HOST = "<host>"
PRIVATE_HOST = "<private-host>"
BIRTH_PILLARS = "<birth-pillars>"
BIRTH_DATE = "<birth-date>"

# A birth chart: four pillars, each a heavenly stem over an earthly
# branch -- "丙子/丙申/壬辰/乙巳". It is the most identifying thing a
# person can carry into a sentence, and it is the one the PII scanner
# does not know about: no digits, no @, no card number, just four
# characters. A method fact can quote one as an example, and the
# example is the part that has to go -- the method stays.
_GAN = "甲乙丙丁戊己庚辛壬癸"
_ZHI = "子丑寅卯辰巳午未申酉戌亥"
_PILLAR = "[%s][%s]" % (_GAN, _ZHI)
_BIRTH_PILLARS = re.compile(
    r"(?:%s)(?:[\s/／、,，]*(?:%s)){3}" % (_PILLAR, _PILLAR))
# A date that is attached to a birth: "born 1996-08-23", "生日 1996-08-23".
_BIRTH_DATE = re.compile(
    r"(?i)(生日|出生|生于|诞辰|birthday|born|birth\s*date)"
    r"(\s*[是为:：]?\s*)(\d{4}[-/]\d{1,2}[-/]\d{1,2})")

# Path segments that mean "a user directory" rather than "a project
# directory". The name after them is a person's name, so it goes.
_HOME_SEGMENTS = {"users", "home"}


def find_usernames(text: str) -> set[str]:
    """The user names that appear in a path in this text.

    A path names its owner -- "C:\\Users\\someone\\..." -- so a path is
    a place to learn who the operator is. The name then has to be
    scrubbed everywhere, not only inside the path: it also turns up in
    the keywords and in the extracted entities, where a path would not
    have caught it.
    """
    if not text:
        return set()
    out = set()
    for m in _USER_IN_PATH.finditer(text):
        name = m.group(1)
        # A single character or a reserved word is not a person's name.
        if len(name) < 3 or name.lower() in ("users", "user", "home",
                                             "public", "default", "all",
                                             "administrator", "temp"):
            continue
        out.add(name)
    return out


def scrub_local_identifiers(text: str) -> tuple[str, list[dict]]:
    """Replace machine-specific identifiers in `text`.

    Returns (scrubbed_text, changes). `changes` is empty when the text
    was already portable.
    """
    if not text:
        return text, []
    changes: list[dict] = []

    def _sub_windows(m: re.Match) -> str:
        sep, segs = m.group(2), m.group(3)
        parts = [p for p in re.split(r'[\\/]', segs) if p]
        if not parts:
            return m.group(0)
        if parts[0].lower() in _HOME_SEGMENTS:
            # Drop the user directory AND the name in it.
            rest = parts[2:]
            out = HOME + (sep + sep.join(rest) if rest else "")
        else:
            # Drop the drive and the top-level directory.
            rest = parts[1:]
            out = ROOT + (sep + sep.join(rest) if rest else "")
        changes.append({
            "kind": "windows_path",
            "sample": m.group(0),
            "replacement": out,
        })
        return out

    def _sub_posix(m: re.Match) -> str:
        tail = m.group(2) or ""
        out = HOME + tail
        changes.append({
            "kind": "posix_home",
            "sample": m.group(0),
            "replacement": out,
        })
        return out

    def _sub_msys(m: re.Match) -> str:
        segs = m.group(2)
        parts = [p for p in segs.split('/') if p]
        if not parts:
            return m.group(0)
        if parts[0].lower() in _HOME_SEGMENTS:
            rest = parts[2:]
            out = HOME + ("/" + "/".join(rest) if rest else "")
        else:
            rest = parts[1:]
            out = ROOT + ("/" + "/".join(rest) if rest else "")
        changes.append({
            "kind": "msys_path",
            "sample": m.group(0),
            "replacement": out,
        })
        return out

    def _sub_tilde(m: re.Match) -> str:
        out = HOME + (m.group(1) or "")
        changes.append({
            "kind": "tilde_home",
            "sample": m.group(0),
            "replacement": out,
        })
        return out

    def _sub_unc(m: re.Match) -> str:
        out = "\\\\%s%s" % (HOST, m.group(2) or "")
        changes.append({
            "kind": "unc_host",
            "sample": m.group(0),
            "replacement": out,
        })
        return out

    def _sub_ip(m: re.Match) -> str:
        changes.append({
            "kind": "private_ip",
            "sample": m.group(0),
            "replacement": PRIVATE_HOST,
        })
        return PRIVATE_HOST

    def _sub_pillars(m: re.Match) -> str:
        # Four pillars that all carry the same heavenly stem are not a
        # birth chart. Qimen spells its "six officers" as 甲子、甲辰、
        # 甲申、甲午 -- every one of them an 甲 -- and that is a list
        # of stems, not a person's date. A birth chart has at least
        # two different stems in it, so that is what the rule asks for.
        stems = {m.group(0)[i] for i in range(0, len(m.group(0)), 3)
                 if i < len(m.group(0))}
        if len(stems) < 2:
            return m.group(0)
        changes.append({
            "kind": "birth_pillars",
            "sample": m.group(0),
            "replacement": BIRTH_PILLARS,
        })
        return BIRTH_PILLARS

    def _sub_birth_date(m: re.Match) -> str:
        # m.group(1) is the word, m.group(2) the separator between it
        # and the date, m.group(3) the date itself.
        out = m.group(1) + m.group(2) + BIRTH_DATE
        changes.append({
            "kind": "birth_date",
            "sample": m.group(0),
            "replacement": out,
        })
        return out

    # A path names its owner. The name then has to go from the rest of
    # the text too: "someone ran the script" identifies the same
    # person as "X:/Users/someone/run.py" does, and one path in a
    # paragraph is enough to connect the two. So the names are learned
    # from the text before the paths are rewritten -- after that there
    # is nothing left to learn them from.
    names = find_usernames(text)

    out = text
    # A path can hide another path: "D:/AI/users或C:/Users" is one
    # match, and the "C:/" inside it is only visible after the first
    # one is rewritten. Two passes catch that; a third catches the
    # third. The loop stops as soon as a pass changes nothing.
    for _ in range(3):
        nxt = _WIN_PATH.sub(_sub_windows, out)
        nxt = _MSYS_PATH.sub(_sub_msys, nxt)
        nxt = _POSIX_HOME.sub(_sub_posix, nxt)
        nxt = _UNC.sub(_sub_unc, nxt)
        nxt = _TILDE_HOME.sub(_sub_tilde, nxt)
        nxt = _PRIVATE_IP.sub(_sub_ip, nxt)
        if nxt == out:
            break
        out = nxt
    # Birth data is not a path, but it is the same kind of thing: a
    # fact about how to do something should not carry the birth chart
    # of the person it uses as an example.
    out = _BIRTH_PILLARS.sub(_sub_pillars, out)
    out = _BIRTH_DATE.sub(_sub_birth_date, out)
    for name in names:
        if name in out:
            out = out.replace(name, "<user>")
            changes.append({
                "kind": "username",
                "sample": name,
                "replacement": "<user>",
            })
    return out, changes


def scrub_values(values: dict) -> tuple[dict, list[dict]]:
    """Scrub every text value of a fact, and the operator's name with it.

    A fact is a row, and a peer reads the row. The path is in the
    content or the context, the name is in the keywords or the entities,
    and the two have to be removed together -- the name is only
    recognisable because a path carried it.

    A value that parses as JSON is scrubbed leaf by leaf and written
    back as JSON. Scrubbing the raw text of a JSON document would not
    find the paths -- a Windows path inside JSON has its separators
    escaped, so "C:\\Users\\x" is not the same bytes as "C:\\Users\\x"
    -- and writing a replacement into it would put a single backslash
    where the document needs two, which is not valid JSON.

    Returns (new_values, changes).
    """
    names: set[str] = set()
    for v in values.values():
        if isinstance(v, str):
            names |= find_usernames(v)
    out: dict = {}
    changes: list[dict] = []
    for key, v in values.items():
        if not isinstance(v, str) or not v:
            out[key] = v
            continue
        new_v, ch = _scrub_text(v, names)
        if ch:
            out[key] = new_v
            changes.extend(ch)
        else:
            new_v = v
            out[key] = v
        new_v, ch2 = rejoin_split_tokens(new_v)
        if ch2:
            out[key] = new_v
            changes.extend(ch2)
    return out, changes


def _scrub_text(v: str, names: set[str]) -> tuple[str, list[dict]]:
    """Scrub one value, as JSON if it is JSON, as text if it is not."""
    import json as _json
    try:
        parsed = _json.loads(v)
    except Exception:
        parsed = None
    if isinstance(parsed, (dict, list)):
        new_parsed, ch = _scrub_json(parsed, names)
        if not ch:
            return v, []
        return _json.dumps(new_parsed, ensure_ascii=False), ch
    new_v, ch = scrub_local_identifiers(v)
    for name in names:
        if name in new_v:
            new_v = new_v.replace(name, "<user>")
            ch = ch + [{"kind": "username", "sample": name,
                        "replacement": "<user>"}]
    return new_v, ch


def _scrub_json(value, names: set[str]):
    if isinstance(value, str):
        return _scrub_text(value, names)
    if isinstance(value, list):
        out, changes = [], []
        for x in value:
            nx, c = _scrub_json(x, names)
            out.append(nx)
            changes.extend(c)
        return out, changes
    if isinstance(value, dict):
        out, changes = {}, []
        for k, x in value.items():
            nx, c = _scrub_json(x, names)
            out[k] = nx
            changes.extend(c)
        return out, changes
    return value, []


# v1.17.9: the sentence splitter used to cut a path in half, so a
# fact that says "maker_pocket.py" read "maker_pocket. py". The
# distiller is fixed, but the rows it already wrote stay broken --
# and the mining skips a fact it has already promoted, so they would
# never be rewritten. The scrub is what runs over the public tier,
# and it can put the pieces back together.
_SPLIT_FILE_RE = re.compile(
    r'\b([a-z][a-z0-9_\-]{1,})\. '
    r'(html?|py|js|json|md|sh|xml|csv|txt|log|ini|cfg|conf|css'
    r'|exe|dll|so|whl|jar|bin)\b')
# A dotted identifier: "bus. _connect" is one name, and the
# underscore is what tells it apart from a sentence that happens to
# end in a period.
_SPLIT_IDENT_RE = re.compile(r'\b([a-z][a-z0-9_]*)\. (_[a-z][a-z0-9_]*)\b')
# A hidden directory: ". venv" is ".venv". The names are ordinary
# English words -- "local", "data", "config" all are -- so the
# tell is the slash in front: a path component has one, and a
# sentence that ends in a period does not. Without the slash this
# joined "it failed. local retry" into "it failed.local retry".
_SPLIT_DOTDIR_RE = re.compile(
    r'(?<=/)\. (venv|git|config|local|data|cache|ssh|npm)\b')
# The whole domain, not the last three labels: "mp. weixin. qq. com"
# is one domain, and joining half of it leaves a label with a space
# after its dot.
_SPLIT_DOMAIN_RE = re.compile(
    r'\b([a-z][a-z0-9\-]{1,}(?:\. [a-z][a-z0-9\-]{1,})*)\. '
    r'(com|net|org|cn|io|dev|app|co|me|ai|info|biz)\b')


def rejoin_split_tokens(text: str) -> tuple[str, list[dict]]:
    """Put a path or a domain back together after the sentence
    splitter cut it. Returns (text, changes)."""
    if not text:
        return text, []
    changes: list[dict] = []

    def _file(m: re.Match) -> str:
        joined = '%s.%s' % (m.group(1), m.group(2))
        changes.append({"kind": "split_file", "sample": m.group(0),
                        "replacement": joined})
        return joined

    def _domain(m: re.Match) -> str:
        joined = (m.group(1) + '. ' + m.group(2)).replace('. ', '.')
        changes.append({"kind": "split_domain", "sample": m.group(0),
                        "replacement": joined})
        return joined

    def _ident(m: re.Match) -> str:
        joined = '%s.%s' % (m.group(1), m.group(2))
        changes.append({"kind": "split_identifier", "sample": m.group(0),
                        "replacement": joined})
        return joined

    def _dotdir(m: re.Match) -> str:
        joined = '.%s' % m.group(1)
        changes.append({"kind": "split_dotdir", "sample": m.group(0),
                        "replacement": joined})
        return joined

    out = _SPLIT_DOMAIN_RE.sub(
        _domain,
        _SPLIT_DOTDIR_RE.sub(
            _dotdir,
            _SPLIT_IDENT_RE.sub(
                _ident,
                _SPLIT_FILE_RE.sub(_file, text))))
    return out, changes
