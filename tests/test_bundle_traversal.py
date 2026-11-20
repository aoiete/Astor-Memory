"""A bundle must not be able to write outside the restore root.

The manifest travels inside the bundle, so its `arcname` values are
untrusted input. Before v1.17.57 the restore did:

    target = astor_dir / f["arcname"]
    shutil.copy2(src, target)

so a bundle whose manifest claims "../../.ssh/authorized_keys" (or an
absolute path) wrote wherever the operator's account could reach. The
same name was used for the temp extraction path, so it escaped the
temp directory too.

Real tar files, real restore, real filesystem -- the test writes into
a temp dir and checks that nothing landed outside it.
"""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from astor_memory.bundle import BUNDLE_VERSION, import_bundle


def _malicious_bundle(tmp: Path, arcname: str, payload: bytes = b"pwned") -> Path:
    """A bundle whose tar member AND manifest claim an escaping path."""
    manifest = {
        "bundle_version": BUNDLE_VERSION,
        "created_at": "2026-01-01T00:00:00+00:00",
        "astor_version": "0.0.0",
        "tiers": ["public"],
        "users": [],
        "stores": ["bus"],
        "files": [{
            "arcname": arcname,
            "tier": "public",
            "user_id": None,
            "store": "bus",
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "rows": {},
        }],
        "total_bytes": len(payload),
    }
    mpath = tmp / "manifest.json"
    mpath.write_text(json.dumps(manifest), encoding="utf-8")

    bundle = tmp / "evil.tar.gz"
    with tarfile.open(bundle, "w:gz") as tf:
        tf.add(str(mpath), arcname="manifest.json")
        info = tarfile.TarInfo(name=arcname)
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    return bundle


class TestBundleRestoreStaysInsideTheRoot(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="astor_bundle_traversal_"))
        self.restore_root = self.tmp / "restore"
        self.restore_root.mkdir()

    def _restore(self, arcname):
        bundle = _malicious_bundle(self.tmp, arcname)
        return import_bundle(bundle, self.restore_root, dry_run=False)

    def test_a_relative_escape_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self._restore("../../escaped.txt")
        self.assertIn("escapes", str(ctx.exception))
        self.assertFalse((self.tmp / "escaped.txt").exists(),
                         "the payload landed outside the restore root")
        self.assertFalse((self.restore_root.parent / "escaped.txt").exists())

    def test_an_absolute_path_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            self._restore("/tmp/astor-bundle-absolute-escape.txt")
        self.assertIn("relative", str(ctx.exception))

    def test_the_dry_run_plan_also_refuses(self):
        bundle = _malicious_bundle(self.tmp, "../../escaped.txt")
        with self.assertRaises(ValueError):
            import_bundle(bundle, self.restore_root, dry_run=True)

    def test_a_normal_member_still_restores(self):
        """The guard must not break the ordinary path."""
        payload = b"a normal bus file"
        manifest = {
            "bundle_version": BUNDLE_VERSION,
            "created_at": "2026-01-01T00:00:00+00:00",
            "astor_version": "0.0.0",
            "tiers": ["public"], "users": [], "stores": ["bus"],
            "files": [{
                "arcname": "public/memory/astor_bus_public.db",
                "tier": "public", "user_id": None, "store": "bus",
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "rows": {},
            }],
            "total_bytes": len(payload),
        }
        mpath = self.tmp / "manifest.json"
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        bundle = self.tmp / "good.tar.gz"
        with tarfile.open(bundle, "w:gz") as tf:
            tf.add(str(mpath), arcname="manifest.json")
            info = tarfile.TarInfo(name="public/memory/astor_bus_public.db")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))

        out = import_bundle(bundle, self.restore_root, dry_run=False)
        self.assertEqual(out.get("restored"), 1, out)
        target = self.restore_root / "public" / "memory" / "astor_bus_public.db"
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
