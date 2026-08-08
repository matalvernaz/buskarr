"""Regression cases for indexing a single file at placement time.

The ``files`` table was written by ``scan.scan`` and by nothing else, and nothing schedules a scan.
Every track the worker acquired was therefore absent from the Library page — and from every query
built on that table — until somebody pressed Rescan by hand. Reported as "greensleeves isn't even
showing up": the file was on disk, its want said ``have``, and the library did not know it existed.

The second case is the one that keeps the fix honest. A row written at placement time must be the
row a full scan would write, or the next scan quietly rewrites it and the two readings of the same
file disagree in between.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from buskarr import db, scan  # noqa: E402

bad = 0


def check(label, ok, detail=""):
    global bad
    if not ok:
        bad += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{('  — ' + detail) if detail else ''}")


PROBE = {
    "codec": "flac", "sample_rate": 44100, "bit_depth": 16, "bitrate": 900000,
    "duration": 218.0, "tag_artist": "Celtic Woman", "tag_album_artist": "Celtic Woman",
    "tag_album": "The Magic Of Christmas", "tag_title": "Greensleeves",
    "tag_year": "2019", "tag_track": 9,
}


def build(root):
    """One library file, laid out the way worker.destination() writes them."""
    path = os.path.join(root, "Celtic Woman", "The Magic Of Christmas (2019)",
                        "09 - Greensleeves.flac")
    os.makedirs(os.path.dirname(path))
    with open(path, "w") as fh:
        fh.write("AUDIO BYTES")
    return path


print("=== a placed file is in the library index without a rescan ===")
with tempfile.TemporaryDirectory() as d:
    lib = os.path.join(d, "music")
    path = build(lib)
    conn = db.init(os.path.join(d, "t.db"))
    old_probe = scan.probe
    scan.probe = lambda p: dict(PROBE)
    try:
        check("indexed", scan.index_file(conn, lib, path))
        row = conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
        check("row exists", row is not None)
        check("searchable by title", row and "greensleeves" in row["norm_title"],
              row and row["norm_title"])
        check("grouped under the artist directory, not the tag credit",
              row and row["artist_lead"] == "Celtic Woman", row and row["artist_lead"])
        check("lossless flag derived", row and row["lossless"] == 1)
        check("a vanished path is reported, not recorded",
              scan.index_file(conn, lib, os.path.join(lib, "Nobody", "Nothing.flac")) is False)
    finally:
        scan.probe = old_probe

print("\n=== the row matches what a full scan writes for the same file ===")
with tempfile.TemporaryDirectory() as d:
    lib = os.path.join(d, "music")
    path = build(lib)
    old_probe = scan.probe
    scan.probe = lambda p: dict(PROBE)
    try:
        placed = db.init(os.path.join(d, "placed.db"))
        scan.index_file(placed, lib, path)
        walked = db.init(os.path.join(d, "walked.db"))
        scan.scan(walked, lib, log=lambda m: None)
        a = dict(placed.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone())
        b = dict(walked.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone())
        # scanned_at is the one column that legitimately differs — it records the reading, not
        # the file.
        a.pop("scanned_at"), b.pop("scanned_at")
        check("identical rows", a == b,
              str({k: (a[k], b[k]) for k in a if a[k] != b[k]}))
    finally:
        scan.probe = old_probe

print("\n=== an unreadable file still lands in the index ===")
with tempfile.TemporaryDirectory() as d:
    lib = os.path.join(d, "music")
    path = build(lib)
    conn = db.init(os.path.join(d, "t.db"))
    old_probe = scan.probe
    scan.probe = lambda p: None
    try:
        scan.index_file(conn, lib, path)
        row = conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
        check("recorded from the path convention", row is not None and row["codec"] == "UNREADABLE",
              row and row["codec"])
        check("title read from the filename", row and row["title"] == "Greensleeves",
              row and row["title"])
        check("track number read from the filename prefix", row and row["track_no"] == 9,
              str(row and row["track_no"]))
    finally:
        scan.probe = old_probe

print(f"\n{bad} failure(s)")
sys.exit(1 if bad else 0)
