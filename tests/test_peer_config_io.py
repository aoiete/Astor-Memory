"""Tests for v1.15.29 (2026-09-28) - Ship M: full peer-config YAML export/import."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path


if __name__ == "__main__":
    unittest.main()


class _Tmp:
    def setUp_tmp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        os.environ["ASTOR_DIR"] = str(self.tmpdir)

    def tearDown_tmp(self):
        try:
            from astor_memory._internal.audit_logger import _reset_audit_conn
            _reset_audit_conn()
        except Exception: pass
        try:
            from astor_memory.nest import lex_index as _lex_mod
            with _lex_mod._LEX_SINGLETONS_LOCK:
                for lex in list(_lex_mod._LEX_SINGLETONS.values()):
                    try: lex.close()
                    except Exception: pass
                _lex_mod._LEX_SINGLETONS.clear()
        except Exception: pass
        try:
            from astor_memory.nest.vector_store import astor_reset_nest
            astor_reset_nest()
        except Exception: pass
        try:
            from astor_memory.bus.store import (
                astor_reset_bus, _BUS_SINGLETONS,
            )
            astor_reset_bus()
            if _BUS_SINGLETONS is not None:
                for _b in list(_BUS_SINGLETONS.values()):
                    try: _b.close()
                    except Exception: pass
                _BUS_SINGLETONS.clear()
        except Exception: pass
        try:
            from astor_memory._internal.peer_relationships import close_all_connections
            close_all_connections()
        except Exception: pass
        try:
            from astor_memory.forge import (_forge_conns, _forge_lock)
            with _forge_lock:
                for _fc in list(_forge_conns.values()):
                    try: _fc.close()
                    except Exception: pass
                _forge_conns.clear()
        except Exception: pass
        try: self._tmp.cleanup()
        except Exception: pass


class TestExportFullConfig(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()
        from astor_memory._internal.peer_identity import init_identity
        from astor_memory._internal.peer_relationships import (
            add_peer, set_topic, quarantine_peer,
        )
        init_identity(str(self.tmpdir))
        add_peer(
            "astor:" + "a" * 32, kind="friend", trust=80,
            alias="alice", endpoint="http://alice:7803",
            public_key="k", metadata={"allow_search": True},
        )
        add_peer(
            "astor:" + "b" * 32, kind="blacklist", trust=0,
            alias="bob",
        )
        set_topic("cooking", "astor:" + "a" * 32, weight=0.7)
        quarantine_peer("astor:" + "a" * 32, reason="test")

    def tearDown(self):
        self.tearDown_tmp()

    def test_export_returns_v1_1_bundle(self):
        from astor_memory._internal.peer_config_io import export_full_config
        bundle = export_full_config()
        self.assertEqual(bundle["version"], "1.1")
        self.assertEqual(bundle["section"], "ship_m_full_peer_config")
        self.assertIn("exported_at", bundle)
        self.assertIn("astor_version", bundle)

    def test_export_includes_all_peer_kinds(self):
        from astor_memory._internal.peer_config_io import export_full_config
        bundle = export_full_config()
        # alice is now quarantined (Ship L) so she appears under quarantine
        self.assertEqual(len(bundle["peers"]["friend"]), 0)
        self.assertEqual(len(bundle["peers"]["blacklist"]), 1)
        self.assertEqual(len(bundle["peers"]["quarantine"]), 1)

    def test_export_includes_topics(self):
        from astor_memory._internal.peer_config_io import export_full_config
        bundle = export_full_config()
        # alice (quarantined) has topic=cooking
        alice_pid = "astor:" + "a" * 32
        self.assertIn(alice_pid, bundle["topics"])
        topics = bundle["topics"][alice_pid]
        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0]["topic"], "cooking")
        self.assertEqual(float(topics[0]["weight"]), 0.7)

    def test_export_strips_public_key(self):
        from astor_memory._internal.peer_config_io import export_full_config
        bundle = export_full_config()
        for kind_peers in bundle["peers"].values():
            for p in kind_peers:
                self.assertNotIn("public_key", p)

    def test_export_includes_rate_limit_section(self):
        from astor_memory._internal.peer_config_io import export_full_config
        bundle = export_full_config()
        self.assertIn("rate_limits", bundle)
        self.assertEqual(len(bundle["rate_limits"]), 2)

    def test_export_summary_counts(self):
        from astor_memory._internal.peer_config_io import export_full_config
        bundle = export_full_config()
        s = bundle["summary"]
        self.assertEqual(s["total_peers"], 2)
        self.assertEqual(s["quarantine_count"], 1)
        self.assertEqual(s["blacklist_count"], 1)


class TestImportFullConfig(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()
        from astor_memory._internal.peer_identity import init_identity
        init_identity(str(self.tmpdir))

    def tearDown(self):
        self.tearDown_tmp()

    def test_import_adds_new_peers(self):
        from astor_memory._internal.peer_config_io import (
            export_full_config, import_full_config,
        )
        from astor_memory._internal.peer_relationships import add_peer, set_topic
        # Create source bundle
        add_peer(
            "astor:" + "a" * 32, kind="friend", trust=80,
            alias="alice", endpoint="http://alice:7803",
            public_key="k", metadata={"allow_search": True},
        )
        set_topic("cooking", "astor:" + "a" * 32, weight=0.5)
        bundle = export_full_config()
        # Wipe DB by re-creating in a new tmpdir
        old_dir = self.tmpdir
        new_dir = Path(tempfile.mkdtemp())
        os.environ["ASTOR_DIR"] = str(new_dir)
        # Import in fresh dir
        result = import_full_config(bundle, strategy="skip")
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["topics_restored"], 1)
        # Verify peer present
        from astor_memory._internal.peer_relationships import (
            get_peer, list_topics_for_peer,
        )
        p = get_peer("astor:" + "a" * 32)
        self.assertIsNotNone(p)
        self.assertEqual(p["kind"], "friend")
        self.assertEqual(p["alias"], "alice")
        # Topics restored
        topics = list_topics_for_peer("astor:" + "a" * 32)
        self.assertEqual(len(topics), 1)
        self.assertEqual(topics[0]["topic"], "cooking")
        # Restore env
        os.environ["ASTOR_DIR"] = str(old_dir)

    def test_import_strategy_skip_keeps_existing(self):
        from astor_memory._internal.peer_config_io import (
            export_full_config, import_full_config,
        )
        from astor_memory._internal.peer_relationships import add_peer
        # Add a peer with trust=80
        add_peer("astor:" + "a" * 32, kind="friend", trust=80,
                 alias="alice", endpoint="http://a:7803", public_key="k")
        # Build a bundle where the same peer has trust=50
        bundle = export_full_config()
        bundle["peers"]["friend"][0]["trust"] = 50
        result = import_full_config(bundle, strategy="skip")
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["added"], 0)
        from astor_memory._internal.peer_relationships import get_peer
        p = get_peer("astor:" + "a" * 32)
        self.assertEqual(int(p["trust"]), 80)  # unchanged

    def test_import_strategy_overwrite_replaces(self):
        from astor_memory._internal.peer_config_io import (
            export_full_config, import_full_config,
        )
        from astor_memory._internal.peer_relationships import add_peer
        add_peer("astor:" + "a" * 32, kind="friend", trust=80,
                 alias="alice", endpoint="http://a:7803", public_key="k")
        bundle = export_full_config()
        bundle["peers"]["friend"][0]["trust"] = 50
        bundle["peers"]["friend"][0]["alias"] = "alice_new"
        result = import_full_config(bundle, strategy="overwrite")
        self.assertEqual(result["overwritten"], 1)
        from astor_memory._internal.peer_relationships import get_peer
        p = get_peer("astor:" + "a" * 32)
        self.assertEqual(int(p["trust"]), 50)
        self.assertEqual(p["alias"], "alice_new")

    def test_import_v10_fallback(self):
        from astor_memory._internal.peer_config_io import import_full_config
        v10 = {
            "version": "1.0",
            "friends": [
                {"peer_id": "astor:" + "f" * 32, "kind": "friend",
                 "trust": 70, "alias": "old_friend"}
            ],
        }
        result = import_full_config(v10, strategy="skip")
        self.assertEqual(result["version"], "1.0")
        self.assertEqual(result["added"], 1)
        from astor_memory._internal.peer_relationships import get_peer
        p = get_peer("astor:" + "f" * 32)
        self.assertIsNotNone(p)
        self.assertEqual(p["alias"], "old_friend")

    def test_import_unsupported_version(self):
        from astor_memory._internal.peer_config_io import import_full_config
        bad = {"version": "2.0", "peers": {}}
        result = import_full_config(bad)
        self.assertIn("error", result)

    def test_import_restores_quarantine(self):
        from astor_memory._internal.peer_config_io import (
            export_full_config, import_full_config,
        )
        from astor_memory._internal.peer_relationships import (
            add_peer, quarantine_peer, get_peer,
        )
        add_peer("astor:" + "a" * 32, kind="friend", trust=80,
                 alias="alice", endpoint="http://a:7803", public_key="k")
        quarantine_peer("astor:" + "a" * 32, reason="abuse")
        bundle = export_full_config()
        # Wipe + restore
        new_dir = Path(tempfile.mkdtemp())
        old_dir = self.tmpdir
        os.environ["ASTOR_DIR"] = str(new_dir)
        result = import_full_config(bundle, strategy="skip")
        self.assertEqual(result["quarantined_restored"], 1)
        p = get_peer("astor:" + "a" * 32)
        self.assertEqual(p["kind"], "quarantine")
        self.assertEqual(int(p["trust"]), 0)
        os.environ["ASTOR_DIR"] = str(old_dir)


class TestCliPeerConfig(_Tmp, unittest.TestCase):
    def setUp(self):
        self.setUp_tmp()
        from astor_memory._internal.peer_identity import init_identity
        from astor_memory._internal.peer_relationships import add_peer
        init_identity(str(self.tmpdir))
        add_peer("astor:" + "a" * 32, kind="friend", trust=80,
                 alias="alice", endpoint="http://a:7803", public_key="k")

    def tearDown(self):
        self.tearDown_tmp()

    def test_export_import_roundtrip(self):
        import sys
        out_path = str(self.tmpdir / "test_bundle.yaml")
        old_argv = sys.argv
        from astor_memory.cli.main import main as _cli_main
        # Export
        sys.argv = ["am", "peer", "export-config", "--out", out_path]
        rc = _cli_main()
        self.assertEqual(rc, 0)
        # Verify file exists
        self.assertTrue(os.path.exists(out_path))
        # Wipe + restore
        new_dir = Path(tempfile.mkdtemp())
        os.environ["ASTOR_DIR"] = str(new_dir)
        sys.argv = ["am", "peer", "import-config", out_path]
        rc = _cli_main()
        self.assertEqual(rc, 0)
        sys.argv = old_argv





