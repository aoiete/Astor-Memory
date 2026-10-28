"""The shared tier must not carry the operator's machine.

A public-tier fact is readable by every peer in the federation. A
method belongs there -- how to fetch a page, which port a service
listens on -- but a path that only exists on this host is private data
in a shared place, and a peer searching the tier learns it.

The rules are structural. A rule that named one operator's working
directory would be the same private data in a different file, and it
would not generalise to the next operator.
"""
from __future__ import annotations

import json

from astor_memory.nest.public_scrub import (
    find_usernames,
    scrub_local_identifiers,
    scrub_values,
)


def _scrub(text: str) -> str:
    out, _ = scrub_local_identifiers(text)
    return out


def test_a_windows_path_loses_its_drive_and_its_top_directory():
    out = _scrub(r"the script is at D:\work\project\run.py")
    assert out == r"the script is at <root>\project\run.py"
    assert "D:" not in out


def test_a_user_directory_loses_the_directory_and_the_name():
    out = _scrub(r"C:\Users\someone\AppData\Local\app\cfg.ini")
    assert out == r"<home>\AppData\Local\app\cfg.ini"
    assert "someone" not in out


def test_a_posix_home_loses_the_name():
    out = _scrub("/home/someone/.config/app/settings")
    assert out == "<home>/.config/app/settings"
    assert "someone" not in out


def test_a_unc_path_loses_the_host():
    out = _scrub(r"\\HOSTNAME\share\file.txt")
    assert out == r"\\<host>\share\file.txt"
    assert "HOSTNAME" not in out


def test_a_private_address_is_replaced():
    out = _scrub("the peer is at 192.168.1.7:7803")
    assert "192.168.1.7" not in out
    assert "<private-host>" in out


def test_loopback_is_a_convention_and_stays():
    assert _scrub("http://127.0.0.1:7803/v1/health") == \
        "http://127.0.0.1:7803/v1/health"


def test_a_public_url_stays():
    url = "https://mp.weixin.qq.com/s/abcdef"
    assert _scrub(url) == url


def test_a_url_is_not_mistaken_for_a_drive_letter():
    # "https://" ends in "s://" -- matching that would rewrite every
    # URL in the corpus.
    out = _scrub("see https://example.com/path for details")
    assert "example.com" in out
    assert "<root>" not in out


def test_a_path_after_a_chinese_character_is_still_a_path():
    out = _scrub("无D:/work/users 或 C:/Users")
    assert "D:/work" not in out
    assert "C:/Users" not in out


def test_a_path_hiding_another_path_is_caught():
    # The second path is inside the segment the first match consumed.
    out = _scrub("x D:/work/users或C:/Users或localhost")
    assert "C:/Users" not in out


def test_the_user_name_is_learned_from_the_path():
    assert find_usernames(r"C:\Users\someone\AppData") == {"someone"}
    assert find_usernames("/home/someone/x") == {"someone"}
    assert find_usernames("no path here") == set()


def test_a_reserved_word_is_not_a_user_name():
    assert find_usernames(r"C:\Users\Users\x") == set()
    assert find_usernames(r"C:\Users\Public\x") == set()


def test_the_user_name_is_removed_from_the_parts_that_are_not_paths():
    """The name is in the keywords and the entities, not only in a path."""
    values = {
        "content": r"see C:\Users\someone\AppData",
        "keywords": json.dumps(["app", "someone"]),
        "entities": json.dumps([{"type": "person", "value": "someone"}]),
    }
    out, changes = scrub_values(values)
    assert "someone" not in out["content"]
    assert "someone" not in out["keywords"]
    assert "someone" not in out["entities"]
    assert changes


def test_a_json_value_stays_valid_json():
    values = {"metadata": json.dumps(
        {"note": r"path C:\Users\someone\AppData", "n": 3})}
    out, changes = scrub_values(values)
    assert changes
    parsed = json.loads(out["metadata"])
    assert parsed["n"] == 3
    assert "someone" not in parsed["note"]


def test_a_value_with_nothing_private_is_left_alone():
    values = {"content": "the port is 7803 and the method is curl",
              "metadata": json.dumps({"a": 1})}
    out, changes = scrub_values(values)
    assert not changes
    assert out == values


def test_a_json_escape_is_not_a_drive_letter():
    """A JSON escape followed by a colon is not a path.

    In a JSON document "\u5e8f" is one escaped character. A colon
    right after it -- "顺序:\u68c0" -- reads as the letter "f", a
    colon and a separator, and matching that cuts the escape in half.
    The document stops being JSON, and every reader of it fails.
    """
    raw = '{"__context__": "\\u987a\\u5e8f:\\u68c0\\u67e5\\u7a0b\\u5e8f"}'
    out, _ = scrub_local_identifiers(raw)
    assert out == raw, "a JSON escape was rewritten:\n%s" % out
    json.loads(out)


def test_a_json_document_stays_parseable_after_scrubbing():
    raw = json.dumps({"__context__": "see C:\\Users\\someone\\AppData",
                       "other": "plain text"})
    out, changes = scrub_values({"metadata": raw})
    assert changes
    parsed = json.loads(out["metadata"])
    assert "someone" not in parsed["__context__"]
    assert parsed["other"] == "plain text"


def test_an_msys_path_loses_the_drive_and_the_top_directory():
    out = _scrub("HERMES_PY=/d/work/project/.venv/Scripts/python")
    assert out == "HERMES_PY=<root>/project/.venv/Scripts/python"
    assert "/d/" not in out


def test_an_msys_user_directory_loses_the_name():
    out = _scrub("the log is at /c/Users/someone/AppData/logs/app.log")
    assert out == "the log is at <home>/AppData/logs/app.log"
    assert "someone" not in out


def test_a_url_path_is_not_an_msys_drive():
    out = _scrub("POST https://example.com/api/v1/read")
    assert "example.com/api/v1/read" in out
    assert "<root>" not in out


def test_a_single_letter_path_is_treated_as_a_drive():
    """A single-letter first segment IS the MSYS drive convention.

    There is no way to tell "/a/b/c" from "/d/work/x", and neither
    carries anything private -- the top directory goes, the structure
    stays. Losing the structure would lose the method.
    """
    out = _scrub("see /a/b/c for details")
    assert out == "see <root>/c for details"


# --- Birth data -------------------------------------------------------
# A birth chart is the most identifying thing a person carries into a
# sentence, and the PII scanner does not know about it: no digits, no
# @, no card number, just four characters.


def test_four_pillars_are_replaced():
    out, ch = scrub_local_identifiers("男 丙子/丙申/壬辰/乙巳 档案里")
    assert "<birth-pillars>" in out
    assert "丙子" not in out
    assert any(c["kind"] == "birth_pillars" for c in ch)


def test_a_birth_date_is_replaced():
    out, _ = scrub_local_identifiers("生日 1996-08-23")
    assert "<birth-date>" in out
    assert "1996-08-23" not in out


def test_the_word_next_to_the_date_stays():
    out, _ = scrub_local_identifiers("生日 1996-08-23")
    assert "生日" in out


def test_a_list_of_stems_is_not_a_birth_chart():
    # Qimen spells its "six officers" as 甲子、甲辰、甲申、甲午 --
    # every one of them an 甲. That is a list of stems in a classical
    # text, not somebody's date, and rewriting it would damage a fact
    # that has nothing to do with a person.
    text = "六儀者，甲子、甲辰、甲申、甲午是也"
    out, ch = scrub_local_identifiers(text)
    assert out == text
    assert ch == []


def test_two_pillars_are_not_a_birth_chart():
    text = "两柱: 丙子/壬辰"
    out, ch = scrub_local_identifiers(text)
    assert out == text
    assert ch == []


# --- The state-const block ---------------------------------------------
# A state-const token is a path, a version, a hash. The distiller used
# to rewrite its dots and dashes to full-width so the sentence split
# would not cut it in half -- and nothing restored them, so the shared
# tier carried a path that no longer resolved anywhere.


def test_the_token_keeps_its_ascii():
    from astor_memory.nest.distiller import distill
    src = ('[LESSON] grep -rn "def schedule" '
           '<home>/AppData/Local/hermes/hermes-agent/hermes_cli/ '
           '2>&1 | head -10')
    out, _removed, _report = distill(src)
    assert "<home>/AppData/Local/hermes/hermes-agent/hermes_cli/" in out
    assert "\uff0d" not in out
    assert "\uff0e" not in out


def test_the_placeholder_is_part_of_the_path():
    from astor_memory.nest.distiller import distill
    src = '[LESSON] grep -rn "def schedule" <home>/AppData/x/hermes_cli/'
    out, _removed, _report = distill(src)
    assert "<home>/AppData/x/hermes_cli/" in out


def test_a_split_path_is_put_back_together():
    """The sentence splitter used to cut a path in half, so a fact
    that said "maker_pocket.py" read "maker_pocket. py". The
    distiller is fixed, but the rows it already wrote stay broken --
    and the mining skips a fact it has already promoted, so they would
    never be rewritten. The scrub is what runs over the public tier,
    and it can put the pieces back together."""
    from astor_memory.nest.public_scrub import rejoin_split_tokens
    out, ch = rejoin_split_tokens("run maker_pocket. py then read server. py")
    assert "maker_pocket.py" in out, out
    assert "server.py" in out, out
    assert ch


def test_a_split_domain_is_put_back_together():
    from astor_memory.nest.public_scrub import rejoin_split_tokens
    out, _ch = rejoin_split_tokens("see mp. weixin. qq. com for details")
    assert "mp.weixin.qq.com" in out, out


def test_a_sentence_is_not_mistaken_for_a_split():
    """A period at the end of a sentence is not a broken path. The
    repair joins a word with the next one only when the next one is a
    file extension or the tail of a domain."""
    from astor_memory.nest.public_scrub import rejoin_split_tokens
    for s in ("the run finished. 5 items were left",
              "it happened. A 2011 answer",
              "no. 5 of them"):
        out, _ch = rejoin_split_tokens(s)
        assert out == s, (s, out)


def test_the_other_shapes_the_splitter_breaks():
    """The sentence splitter cut three more shapes, and each has a
    tell that separates it from a sentence that ends in a period:
    a binary extension, a dotted identifier with a leading
    underscore, and a hidden directory."""
    from astor_memory.nest.public_scrub import rejoin_split_tokens
    out, _ch = rejoin_split_tokens("run python. exe and hermes. exe")
    assert "python.exe" in out and "hermes.exe" in out, out
    out, _ch = rejoin_split_tokens("see bus. _connect and self. _conn")
    assert "bus._connect" in out and "self._conn" in out, out
    out, _ch = rejoin_split_tokens("use hermes-agent/. venv and repo/. git")
    assert ".venv" in out and ".git" in out, out


def test_a_sentence_with_ordinary_words_is_not_joined():
    from astor_memory.nest.public_scrub import rejoin_split_tokens
    for s in ("the run finished. snapshot was taken",
              "it failed. local retry then gave up"):
        out, _ch = rejoin_split_tokens(s)
        assert out == s, (s, out)
