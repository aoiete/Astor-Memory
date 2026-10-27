"""Round-trip check for astor_memory.bundle.

    python tests/test_bundle.py

The bundle packs the memory state into one file and puts it back. The
only thing that proves that works is doing it: pack a runtime, wipe it,
restore it, and compare the facts that came out.

Nothing here touches the real runtime. The test builds its own ASTOR_DIR
in a temporary directory with the same 9-db layout the real one uses.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

failures = []


def check(name, ok, detail=""):
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name, ("  -> " + detail) if detail and not ok else ""))
    if not ok:
        failures.append(name)
    return ok


BUS_SCHEMA = """
CREATE TABLE memory_canonical (
    id INTEGER PRIMARY KEY,
    content TEXT NOT NULL,
    namespace TEXT NOT NULL,
    tombstoned INTEGER DEFAULT 0
);
CREATE TABLE events (id INTEGER PRIMARY KEY, event_type TEXT, payload TEXT);
CREATE TABLE audit_log (id INTEGER PRIMARY KEY, actor TEXT, action TEXT);
CREATE TABLE memory_candidates (id INTEGER PRIMARY KEY, content TEXT);
CREATE TABLE schema_version (version INTEGER, applied_at TEXT);
"""


def make_runtime(root: Path) -> Path:
    """Build a runtime with one public scope and one private user."""
    adir = root / "astor"
    pub = adir / "public" / "memory"
    priv = adir / "users" / "alice" / "memory"
    for d in (pub, priv):
        d.mkdir(parents=True, exist_ok=True)

    for name, rows in (("astor_bus_public.db", 3), ("astor_bus_alice.db", 2)):
        db = (pub if "public" in name else priv) / name
        conn = sqlite3.connect(db)
        conn.executescript(BUS_SCHEMA)
        for i in range(rows):
            conn.execute(
                "INSERT INTO memory_canonical (content, namespace) VALUES (?, ?)",
                ("fact %d of %s" % (i, name), "ns"),
            )
        conn.commit()
        conn.close()

    # An empty nest and forge, so the bundle walks the same three stores.
    for store, tier, uid in (("nest", "public", None), ("forge", "public", None),
                             ("nest", "private", "alice"), ("forge", "private", "alice")):
        if tier == "private":
            path = priv / ("astor_%s_%s.db" % (store, uid))
        else:
            path = pub / ("astor_%s_%s.db" % (store, tier))
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE schema_version (version INTEGER)")
        conn.commit()
        conn.close()
    return adir


def facts(db: Path) -> list:
    if not db.exists():
        return []
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute(
            "SELECT content FROM memory_canonical ORDER BY id")]
    finally:
        conn.close()


def main():
    from astor_memory.bundle import export_bundle, import_bundle, inspect_bundle

    root = Path(tempfile.mkdtemp(prefix="astor-bundle-test-"))
    try:
        adir = make_runtime(root)
        pub_facts_before = facts(adir / "public" / "memory" / "astor_bus_public.db")
        priv_facts_before = facts(adir / "users" / "alice" / "memory" / "astor_bus_alice.db")
        check("the test runtime holds facts", len(pub_facts_before) == 3 and len(priv_facts_before) == 2)

        # --- export ---
        bundle = root / "bundle.tar.gz"
        rep = export_bundle(adir, bundle, tiers=["public", "private"], user_ids=["alice"])
        check("export writes a bundle", bundle.exists() and bundle.stat().st_size > 0)
        check("export reports the files it packed", len(rep["manifest"]["files"]) == 6,
              "%d files" % len(rep["manifest"]["files"]))
        names = [f["arcname"] for f in rep["manifest"]["files"]]
        check("the bundle holds the public bus",
              "public/memory/astor_bus_public.db" in names)
        check("the bundle holds the private bus",
              "users/alice/memory/astor_bus_alice.db" in names)
        check("the bundle leaves the lex index out",
              not any("lex" in n for n in names))

        # --- inspect ---
        man = inspect_bundle(bundle)
        check("the manifest is readable", man["bundle_version"] == 1)
        check("the manifest carries a checksum per file",
              all(len(f["sha256"]) == 64 for f in man["files"]))
        check("the manifest counts the rows",
              man["files"][0]["rows"].get("memory_canonical") in (2, 3))

        # --- dry run does not write ---
        rep2 = import_bundle(bundle, adir, dry_run=True)
        check("a dry run restores nothing", rep2["restored"] == 0)

        # --- wipe and restore ---
        for f in (adir / "public" / "memory" / "astor_bus_public.db",
                  adir / "users" / "alice" / "memory" / "astor_bus_alice.db"):
            f.unlink()
        check("the runtime is empty before the restore",
              facts(adir / "public" / "memory" / "astor_bus_public.db") == [])

        rep3 = import_bundle(bundle, adir)
        check("the restore puts the files back", rep3["restored"] == 6,
              "%d restored" % rep3["restored"])
        pub_facts_after = facts(adir / "public" / "memory" / "astor_bus_public.db")
        priv_facts_after = facts(adir / "users" / "alice" / "memory" / "astor_bus_alice.db")
        check("the public facts come back unchanged", pub_facts_after == pub_facts_before,
              "%s vs %s" % (pub_facts_after, pub_facts_before))
        check("the private facts come back unchanged", priv_facts_after == priv_facts_before)

        # --- a corrupted file inside the bundle is refused ---
        # Corrupt the payload, not the tail: gzip ignores trailing bytes,
        # so appending would not exercise the checksum at all.
        import io
        import tarfile
        bad = root / "bad.tar.gz"
        with tarfile.open(bundle, "r:gz") as src, tarfile.open(bad, "w:gz") as dst:
            for member in src.getmembers():
                data = src.extractfile(member).read() if member.isfile() else b""
                if member.isfile() and member.name.endswith("astor_bus_public.db"):
                    data = data[:-1] + bytes([data[-1] ^ 0xFF])  # flip one byte
                if member.isfile():
                    member.size = len(data)
                dst.addfile(member, io.BytesIO(data))
        try:
            import_bundle(bad, adir)
            check("a corrupted bundle is refused", False, "no error raised")
        except ValueError as e:
            check("a corrupted bundle is refused", "checksum" in str(e).lower(), str(e)[:90])
        except Exception as e:
            check("a corrupted bundle is refused", False, "wrong error: %r" % e)

        # --- a bundle of a tier that was not asked for stays out ---
        only_pub = root / "pub-only.tar.gz"
        export_bundle(adir, only_pub, tiers=["public"])
        man3 = inspect_bundle(only_pub)
        check("a public-only bundle leaves private data out",
              not any(f["tier"] == "private" for f in man3["files"]))

        return 1 if failures else 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    code = main()
    print()
    if failures:
        print("FAILED: %s" % ", ".join(failures))
    else:
        print("all checks passed")
    sys.exit(code)
