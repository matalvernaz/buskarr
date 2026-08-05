"""Regression cases for the upgrade sweep.

The sweep replaces audio, which makes its failure ordering the whole point: the new copy must be
in the library before the original leaves it, the original must be quarantined rather than
deleted, and a download that arrives worse than its advertisement — or the wrong length — must
be discarded without touching anything.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from buskarr import db, scan, upgrade, worker  # noqa: E402

bad = 0


def check(label, ok, detail=""):
    global bad
    if not ok:
        bad += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{('  — ' + detail) if detail else ''}")


class FakeProvider:
    """Claims FLAC; delivers whatever ``deliver`` says the probe should see."""
    name = "fake"

    def __init__(self, deliver):
        self.deliver = deliver

    def search(self, want):
        return [{"provider": self.name, "title": want["title"], "artist": want["artist"],
                 "duration": want["duration"], "codec": "flac", "bitrate": 1000000,
                 "sample_rate": 44100, "bit_depth": 16}]

    def fetch(self, cand, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        staged = os.path.join(dest_dir, "dl.flac")
        with open(staged, "w") as fh:
            fh.write("NEW AUDIO BYTES")
        return staged


def scenario(deliver):
    """Fresh library with one lossy file behind a satisfied want; returns the moving parts."""
    d = tempfile.TemporaryDirectory()
    lib = os.path.join(d.name, "lib")
    old = os.path.join(lib, "A", "Alb (1999)", "01 - Song.mp3")
    os.makedirs(os.path.dirname(old))
    with open(old, "w") as fh:
        fh.write("OLD LOSSY BYTES")
    conn = db.init(os.path.join(d.name, "t.db"))
    wid, _ = db.add_want(conn, "A", "Song", "Alb", "1999", 100.0, allow_dup=True)
    conn.execute("UPDATE wants SET status=?, file_path=?, provider='youtube' WHERE id=?",
                 (db.STATUS_HAVE, old, wid))
    db.upsert_file(conn, {"path": old, "artist": "A", "artist_lead": "A", "album": "Alb",
                          "title": "Song", "file_title": "Song", "tag_title": "Song",
                          "track_no": 1, "year": "1999", "codec": "mp3", "bitrate": 128000,
                          "sample_rate": 44100, "bit_depth": None, "duration": 100.0,
                          "size": 15, "provider": "youtube", "mtime": 1.0})
    conn.commit()
    saved = (worker.LIBRARY, worker.STAGING, upgrade.UPGRADE_DIR, scan.probe)
    worker.LIBRARY = lib
    worker.STAGING = os.path.join(d.name, "staging")
    upgrade.UPGRADE_DIR = os.path.join(lib, ".upgrade_originals")
    scan.probe = lambda path: dict(deliver)
    provs = [{"provider": FakeProvider(deliver), "name": "fake"}]
    return d, conn, wid, old, lib, provs, saved


def restore(saved):
    worker.LIBRARY, worker.STAGING, upgrade.UPGRADE_DIR, scan.probe = saved


GOOD = {"codec": "flac", "bitrate": 950000, "sample_rate": 44100, "bit_depth": 16,
        "duration": 100.5, "tag_track": 1, "tag_title": "Song", "tag_artist": "A"}

print("=== a genuine upgrade replaces, quarantines, and re-points ===")
d, conn, wid, old, lib, provs, saved = scenario(GOOD)
try:
    r = upgrade.sweep(conn, dry_run=False, provs=provs, log=lambda m: None)
    new = os.path.join(lib, "A", "Alb (1999)", "01 - Song.flac")
    q = os.path.join(upgrade.UPGRADE_DIR, "A", "Alb (1999)", "01 - Song.mp3")
    check("one upgrade performed", r["upgraded"] == 1, str(r))
    check("new copy at the canonical destination",
          os.path.exists(new) and open(new).read() == "NEW AUDIO BYTES")
    check("original moved to quarantine, byte-for-byte",
          os.path.exists(q) and open(q).read() == "OLD LOSSY BYTES")
    check("original gone from the library", not os.path.exists(old))
    w = conn.execute("SELECT provider, file_path, note FROM wants WHERE id=?", (wid,)).fetchone()
    check("want re-pointed at the new file", w["file_path"] == new, w["file_path"])
    check("provider and note record the upgrade",
          w["provider"] == "fake" and "upgraded" in (w["note"] or ""), str(dict(w)))
    check("old files row forgotten",
          conn.execute("SELECT 1 FROM files WHERE path=?", (old,)).fetchone() is None)
finally:
    restore(saved); d.cleanup()

print("\n=== a download that arrives WORSE than claimed is refused ===")
d, conn, wid, old, lib, provs, saved = scenario(
    dict(GOOD, codec="mp3", bitrate=96000, bit_depth=None))
try:
    r = upgrade.sweep(conn, dry_run=False, provs=provs, log=lambda m: None)
    check("nothing upgraded", r["upgraded"] == 0, str(r))
    check("original untouched", os.path.exists(old) and open(old).read() == "OLD LOSSY BYTES")
finally:
    restore(saved); d.cleanup()

print("\n=== a download at the wrong length is refused ===")
d, conn, wid, old, lib, provs, saved = scenario(dict(GOOD, duration=93.0))
try:
    r = upgrade.sweep(conn, dry_run=False, provs=provs, log=lambda m: None)
    check("nothing upgraded (±4%% gate)", r["upgraded"] == 0, str(r))
    check("original untouched", os.path.exists(old))
finally:
    restore(saved); d.cleanup()

print("\n=== dry run plans but touches nothing ===")
d, conn, wid, old, lib, provs, saved = scenario(GOOD)
try:
    r = upgrade.sweep(conn, dry_run=True, provs=provs, log=lambda m: None)
    check("one upgrade planned", r["upgraded"] == 1, str(r))
    check("original untouched", os.path.exists(old))
    check("nothing placed", not os.path.exists(os.path.join(lib, "A", "Alb (1999)",
                                                            "01 - Song.flac")))
finally:
    restore(saved); d.cleanup()

print(f"\n{bad} failure(s)")
sys.exit(1 if bad else 0)
