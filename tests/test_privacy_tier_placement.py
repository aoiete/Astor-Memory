"""A fact marked personal must not be readable by a peer, and must not
sit in a tier a peer can read.

v1.17.15. The public store holds BOTH commons and personal rows --
`visibility` is what separates them. Four defects let personal rows
out, all measured on the live corpus:

  * the write path chose its tier before the facts were extracted, so a
    fact the classifier called `personal` still landed in the public
    store (509 of 732 public rows were personal; 479 existed nowhere
    else);
  * the open-search peer read took the whole public table with no
    visibility filter;
  * the friend-path peer read took any id the merge produced;
  * has_pii() had no platform names in its keyword table, so
    "telegram 555000111" was not flagged -- the id was found and then
    dropped for want of context, and the 6-character context window
    truncated "telegram" past its own word boundary.

These tests pin the behaviour, not the implementation.
"""
from __future__ import annotations

import re
import sqlite3
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astor_memory.nest.visibility_classifier import has_pii  # noqa: E402
from astor_memory.config import get_default_astor_dir


class TestPiiDetectionCatchesChatIds(unittest.TestCase):

    def test_telegram_chat_id_is_flagged(self):
        """The shape of the text that leaked into the public tier."""
        text = ("deliver=origin→telegram 555000111, 所以 9/3~10/21 "
                "每天 15:00 发的都是 \"Cron test OK\" 测试消息")
        hits = has_pii(text)
        names = {h["name"] for h in hits}
        self.assertIn("telegram_chat_id", names,
                      f"a telegram chat id must be flagged: {hits}")
        redacting = [h for h in hits if h["severity"] == "redact"]
        self.assertTrue(redacting, "a chat id must be severity=redact")

    def test_platform_names_in_context(self):
        for platform in ("telegram", "discord", "wechat"):
            with self.subTest(platform=platform):
                hits = has_pii(f"deliver to {platform} 555000111 today")
                self.assertIn("telegram_chat_id",
                              {h["name"] for h in hits})

    def test_a_bare_number_is_not_pii(self):
        """8-12 digit numbers are common; only context makes them PII."""
        self.assertEqual(has_pii("the run took 1790806090 ms"), [])

    def test_context_window_is_wide_enough(self):
        """A 6-char window cut 'telegram' to 'egram'."""
        text = "x " + ("padding words here " * 3) + "telegram 555000111"
        hits = has_pii(text)
        self.assertIn("telegram_chat_id", {h["name"] for h in hits})


class TestPeerReadFiltersPersonal(unittest.TestCase):

    def test_open_search_sql_filters_visibility(self):
        src = (ROOT / "astor_memory" / "_internal" /
               "peer_open_search.py").read_text(encoding="utf-8")
        self.assertIn("visibility", src,
                      "the peer read must filter on visibility")
        m = re.search(r"where\s*=\s*\(?\s*'COALESCE\(tombstoned",
                      src)
        self.assertIsNotNone(m, "the peer read SQL must be reachable")
        seg = src[m.start():m.start() + 400]
        self.assertIn("commons", seg,
                      "the peer read SQL must restrict to commons rows")

    def test_friend_path_filters_visibility(self):
        src = (ROOT / "astor_memory" / "server.py").read_text(encoding="utf-8")
        i = src.find("def peer_public_search")
        self.assertGreater(i, 0)
        seg = src[i:i + 9000]
        self.assertIn("visibility", seg,
                      "the friend-path peer read must filter on visibility")
        self.assertIn("'commons'", seg,
                      "the friend-path peer read must restrict to commons")

    def test_no_peer_sql_reads_the_whole_public_table(self):
        """The failure mode was a SELECT with no visibility predicate."""
        src = (ROOT / "astor_memory" / "_internal" /
               "peer_open_search.py").read_text(encoding="utf-8")
        for m in re.finditer(r"FROM memory_canonical", src):
            seg = src[max(0, m.start() - 600):m.start() + 300]
            self.assertIn("visibility", seg,
                          "every peer-side query must mention visibility")


class TestWritePathRoutesPersonalToPrivate(unittest.TestCase):

    def test_write_path_has_a_private_target(self):
        src = (ROOT / "astor_memory" / "server.py").read_text(encoding="utf-8")
        self.assertIn("_private_target", src,
                      "the write path must build a private-tier target")
        self.assertIn("_f_tier", src,
                      "the write path must choose the tier per fact")

    def test_personal_facts_do_not_use_the_public_bus(self):
        src = (ROOT / "astor_memory" / "server.py").read_text(encoding="utf-8")
        i = src.find("def _private_target")
        self.assertGreater(i, 0)
        seg = src[i:i + 1500]
        self.assertIn("'private'", seg,
                      "the private target must be the private tier")
        # v1.17.15: the private target opens the bucket through
        # open_own_private, not astor_bus directly. The ACL is bound
        # with the caller's user_id, and admin cannot open
        # private_<admin> -- so the route binds as the owner to take
        # the bus. See astor_memory/_internal/acl.py.
        self.assertIn("open_own_private", seg,
                      "the private target must open its own bus")

    def test_personal_routing_is_not_applied_to_private_writes(self):
        """A write already targeting private must not re-route."""
        src = (ROOT / "astor_memory" / "server.py").read_text(encoding="utf-8")
        i = src.find("_f_bus, _f_event, _f_lex = bus, event_id, _lex")
        self.assertGreater(i, 0)
        seg = src[i:i + 500]
        self.assertIn("tier != 'private'", seg,
                      "the routing must skip writes already in private")


class TestStoreIsTheOnePlaceThatRefuses(unittest.TestCase):
    """The guarantee has to be structural, not per-call-site discipline.

    Six call sites could reach the public tier without declaring a
    visibility. A check at each of them is a check the next caller
    forgets, so the check lives in the one funnel every write passes
    through: promote_candidate.
    """

    def _bus(self, tmpdir):
        import sqlite3
        from astor_memory._internal.acl import astor_init_acl
        from astor_memory._internal.acl_layout import Tier, get_db_path
        from astor_memory.bus.schema import astor_init_schema
        from astor_memory.bus.store import astor_bus
        p = get_db_path(Tier.PUBLIC.value, "bus", None)
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p), timeout=5)
        astor_init_schema(conn)
        conn.close()
        astor_init_acl(actor="admin:admin", role="admin", tier="public")
        return astor_bus(tier=Tier.PUBLIC.value, user_id=None)

    def _write(self, bus, content, kind, tier, visibility=None):
        ev = bus.append_event(namespace="t", agent_id="t", source="t",
                              action="write", content=content)
        cand = bus.insert_candidate(event_id=ev, namespace="t",
                                    content=content, kind=kind, confidence=0.9)
        kw = {} if visibility is None else {"visibility": visibility}
        return bus.promote_candidate(
            cand, promoted_by="test", user_id="admin", tier=tier, **kw)

    def test_personal_into_public_is_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            bus = self._bus(td)
        with self.assertRaises(ValueError) as ctx:
            self._write(bus, "the operator prefers TypeScript for backend",
                        "fact", "public")
        self.assertIn("visibility='commons'", str(ctx.exception))

    def test_a_pii_bearing_lesson_is_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            bus = self._bus(td)
        with self.assertRaises(ValueError):
            self._write(bus, "deliver=origin to telegram 555000111 every day",
                        "lesson", "public")

    def test_a_clean_lesson_is_published(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            bus = self._bus(td)
        fid = self._write(
            bus, "to repair a read cache, add every ranking parameter "
                 "to the cache key", "lesson", "public",
            visibility="commons")
        row = bus.conn.execute(
            "SELECT visibility, tier FROM memory_canonical WHERE id = ?",
            (fid,)).fetchone()
        self.assertEqual(row[0], "commons")
        self.assertEqual(row[1], "public")

    def test_a_private_write_is_untouched(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            bus = self._bus(td)
        fid = self._write(bus, "the operator prefers TypeScript",
                          "fact", "private")
        row = bus.conn.execute(
            "SELECT visibility, tier FROM memory_canonical WHERE id = ?",
            (fid,)).fetchone()
        self.assertEqual(row[0], "personal")
        self.assertEqual(row[1], "private")

    def test_publishers_declare_their_visibility(self):
        """Every source call site that can reach public says so."""
        import re
        root = Path(__file__).resolve().parent.parent / "astor_memory"
        offenders = []
        for f in root.rglob("*.py"):
            src = f.read_text(encoding="utf-8")
            for m in re.finditer(r"promote_candidate\(", src):
                start = m.end()
                depth, i = 1, start
                while i < len(src) and depth:
                    if src[i] == "(":
                        depth += 1
                    elif src[i] == ")":
                        depth -= 1
                    i += 1
                args = src[start:i - 1]
                if "visibility" in args:
                    continue
                if re.search(r"tier\s*=\s*('public'|\"public\"|Tier\.PUBLIC)",
                             args):
                    line = src[:m.start()].count("\n") + 1
                    offenders.append(f"{f.name}:{line}")
        self.assertEqual(
            offenders, [],
            "these call sites publish to the public tier without saying "
            f"so: {offenders}")


class TestPublicStoreHoldsOnlyShareableRows(unittest.TestCase):
    """The invariant, checked against the live store when it is there."""

    def test_public_store_has_no_personal_rows(self):
        runtime = get_default_astor_dir()
        db = runtime / "public" / "memory" / "astor_bus_public.db"
        if not db.exists() or not os.environ.get("LIVE_ASTOR"):
            # LIVE_ASTOR is how the suite says "the live deployment is
            # under test". Without it the resolver may point at a real
            # runtime by accident, and a test that writes to the shared
            # tier during the run would then be judged by it.
            self.skipTest("live runtime not present")
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            n = con.execute(
                "SELECT COUNT(*) FROM memory_canonical "
                "WHERE COALESCE(tombstoned,0)=0 AND visibility='personal'"
            ).fetchone()[0]
        finally:
            con.close()
        self.assertEqual(
            n, 0,
            f"{n} personal rows sit in the public store -- a peer can "
            f"read every one of them")

    def test_public_store_holds_only_shareable_kinds(self):
        """public is for lessons, methods, workflows, models and patterns."""
        from astor_memory.nest.visibility_classifier import AUTO_COMMONS_KINDS
        runtime = get_default_astor_dir()
        db = runtime / "public" / "memory" / "astor_bus_public.db"
        if not db.exists() or not os.environ.get("LIVE_ASTOR"):
            self.skipTest("live runtime not present")
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT DISTINCT kind FROM memory_canonical "
                "WHERE COALESCE(tombstoned,0)=0"
            ).fetchall()
        finally:
            con.close()
        bad = sorted({r[0] for r in rows} - set(AUTO_COMMONS_KINDS))
        self.assertEqual(
            bad, [],
            f"these kinds sit in the public tier but are not in "
            f"AUTO_COMMONS_KINDS: {bad}")


class TestKindWhitelistIsNotBypassed(unittest.TestCase):
    """An explicit `commons` hint must not promote an unshareable kind."""

    def test_hint_commons_cannot_promote_a_bare_fact(self):
        from astor_memory.nest.visibility_classifier import classify_visibility
        decision = classify_visibility(
            text="the operator prefers TypeScript for backend work",
            kind="fact", hint="commons", admin_allow_commons=True)
        self.assertEqual(decision["visibility"], "personal",
                         "a `fact` is not a lesson/method/workflow/model")

    def test_hint_commons_still_promotes_a_lesson(self):
        from astor_memory.nest.visibility_classifier import classify_visibility
        decision = classify_visibility(
            text="to repair a read cache, add every ranking parameter to the key",
            kind="lesson", hint="commons", admin_allow_commons=True)
        self.assertEqual(decision["visibility"], "commons")

    def test_auto_path_still_promotes_a_lesson(self):
        from astor_memory.nest.visibility_classifier import classify_visibility
        decision = classify_visibility(
            text="to repair a read cache, add every ranking parameter to the key",
            kind="lesson", hint="auto", admin_allow_commons=True)
        self.assertEqual(decision["visibility"], "commons")


if __name__ == "__main__":
    unittest.main()
