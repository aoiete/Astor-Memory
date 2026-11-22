"""A chat id must not reach the commons tier.

The commons tier is what every user of an install can read, and what
the peer path hands to other nodes. A fact that names a person's chat
id is not shared knowledge -- it is an identifier, and it identifies
one person to everyone.

Two identifiers reached the public tier on this host, and both got past
the PII detector:

1. The operator's own Telegram chat id, written as
   ``telegram DM <chat_id>``. The detector matched a platform name
   followed by digits, and only allowed ``chat_id``/``user_id`` in
   between -- so the ROLE word ("DM") broke the match.

2. A WeChat user id, written bare: ``<wechat_id>-...``. The detector
   only knew the WeChat id in its full ``...@im.wechat`` form.

Both are now caught. This test pins them, and pins the neighbours that
must NOT be caught -- a Hermes session id that starts with a date
(``telegram DM 20261104_122221_61c9ac3a``) is a session, not a chat,
and redacting it would drop a dozen harmless facts over a word.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from astor_memory.nest.visibility_classifier import has_pii  # noqa: E402


# A 9-digit run, assembled at runtime so that no identifier-shaped
# literal sits in the source. The digits are arbitrary -- what the
# detector has to notice is the SHAPE: a platform, a role, the digits.
_CHAT_ID = "448" "000" "241"

# A WeChat user id: an "o", then letters and digits. Assembled the same
# way -- the shape is what the detector has to notice, and a literal
# one in the source would be an identifier sitting in a public repo.
_WECHAT_ID = "o9cq" "80632"

# A Discord id: a longer run of digits. Assembled the same way.
_DISCORD_ID = "151" "448084" "479266" "412"


def _redacted(text: str) -> list[str]:
    return [m["name"] for m in has_pii(text) if m["severity"] == "redact"]


class TestChatIdsDoNotReachCommons(unittest.TestCase):

    def test_a_telegram_chat_id_with_a_role_word_is_caught(self):
        """The shape that got past: platform, role, digits."""
        self.assertTrue(_redacted(f"session 20261104_122221_61c9ac3a "
                                  f"(telegram DM {_CHAT_ID}) ran compression"),
                        "the operator's telegram chat id was not detected")

    def test_a_telegram_chat_id_in_a_namespaced_form_is_caught(self):
        self.assertTrue(_redacted(
            f"gateway_routing entry for telegram:dm:{_CHAT_ID} carries "
            "last_prompt_tokens 359795"))

    def test_a_bare_wechat_user_id_is_caught(self):
        self.assertTrue(_redacted(
            f"read the WEIXIN_ADMIN_ID (admin's {_WECHAT_ID}-...) from .env"))

    def test_a_hermes_session_id_is_not_a_chat_id(self):
        """The neighbour that must stay clean.

        A Hermes session id starts with a date and has more after it.
        The digits of the date are not a chat id, and redacting the
        sentence would throw away a useful fact over a number.
        """
        self.assertEqual(
            _redacted("the compression-churn loop on telegram DM "
                      "20261104_122221_61c9ac3a has RESUMED"), [],
            "a Hermes session id was treated as a chat id")
        self.assertEqual(
            _redacted("the discord DM 20261029_200344_95e192ab is idle"), [],
            "a Hermes session id was treated as a chat id")

    def test_a_platform_name_alone_is_not_a_chat_id(self):
        """The neighbour that must stay clean (a filename)."""
        self.assertEqual(_redacted("wechat.html is a filename"), [])
        self.assertEqual(_redacted("telegram is where the bot runs"), [])
        self.assertEqual(_redacted("curl + UA method to fetch wechat articles"),
                         [])

    def test_ordinary_words_starting_with_o_are_not_wechat_ids(self):
        """The neighbour that must stay clean.

        "occurrences" and "occurrence" start with "o" and are long
        enough. A WeChat id carries digits; a word does not.
        """
        self.assertEqual(_redacted("occurrences and occurrence are words"), [])
        self.assertEqual(_redacted("openness and opacity"), [])

    def test_a_topic_is_not_pii(self):
        """The rule the existing tests already pin."""
        self.assertEqual(_redacted("our key rotation is automated now"), [])
        self.assertEqual(_redacted("we put the API key in .env"), [])

    # --- v1.17.84: the platform ABBREVIATIONS -------------------------
    #
    # The detector knew the full platform names. The operator writes
    # the short ones -- "DC" for Discord, "TG" for Telegram -- so three
    # facts in the public tier carried a real Discord id that the
    # detector read as ordinary prose.
    #
    # The abbreviation forms are now caught, including the shapes the
    # real facts used: "admin" between the abbreviation and the role,
    # an English role word followed by a Chinese one, and the digits
    # wrapped in parentheses.

    def test_a_discord_id_written_with_the_abbreviation_is_caught(self):
        self.assertTrue(_redacted(
            f"DC admin DM 通道({_DISCORD_ID})只负责 crypto 信号"),
            "the operator's discord id was not detected")

    def test_a_discord_id_written_as_dc_channel_is_caught(self):
        self.assertTrue(_redacted(
            f"DC 通道 {_DISCORD_ID} 只负责 crypto 信号"))

    def test_a_telegram_id_written_with_the_abbreviation_is_caught(self):
        self.assertTrue(_redacted(
            f"TG DM {_CHAT_ID} 只负责 crypto 信号"))

    def test_an_abbreviation_without_a_role_word_is_not_a_chat_id(self):
        """The neighbour that must stay clean.

        "DC" and "TG" are ordinary words in English and abbreviations
        in plenty of contexts. Without a role word naming a
        conversation there is no identifier to redact.
        """
        self.assertEqual(_redacted("DC power supply model 5000W"), [])
        self.assertEqual(_redacted("TG the joint probability distribution"), [])
        self.assertEqual(_redacted("the DC group of companies reported"), [])
        self.assertEqual(_redacted("TGIF revenue grew 12%"), [])

    def test_an_abbreviation_with_a_role_word_but_no_digits_is_clean(self):
        """The neighbour that must stay clean."""
        self.assertEqual(_redacted("DC admin panel is broken"), [])
        self.assertEqual(_redacted("tg admin dashboard needs a refresh"), [])
        self.assertEqual(_redacted("DC channel 5000 words a day"), [])


if __name__ == "__main__":
    unittest.main()
