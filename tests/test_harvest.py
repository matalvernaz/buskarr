"""Regression cases for harvest matching and placement.

Two defects live here. Wants were bucketed under their STRICT norm (parentheticals preserved) but
looked up by the filename's plain norm (parentheticals stripped), so a want for "Song (live)" sat
in a bucket no filename key could reach — permanently unharvestable. And the import used
``os.path.exists`` + ``shutil.copy2``, a check-then-act whose act TRUNCATES an existing file: the
one destructive shape this project promises not to contain.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from buskarr import db, harvest, scan, worker  # noqa: E402

bad = 0


def check(label, ok, detail=""):
    global bad
    if not ok:
        bad += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{('  — ' + detail) if detail else ''}")


print("=== copy_no_replace never truncates an existing file ===")
with tempfile.TemporaryDirectory() as d:
    victim = os.path.join(d, "01 - Song.flac")
    with open(victim, "w") as fh:
        fh.write("ORIGINAL AUDIO")
    src = os.path.join(d, "downloaded.flac")
    with open(src, "w") as fh:
        fh.write("NEW AUDIO")
    try:
        worker.copy_no_replace(src, victim)
        check("existing destination refused", False, "no FileExistsError raised")
    except FileExistsError:
        check("existing destination refused", True)
    check("existing file survived", open(victim).read() == "ORIGINAL AUDIO")
    check("source left in place (seeding must not be disturbed)", os.path.exists(src))
    fresh = os.path.join(d, "02 - Other.flac")
    worker.copy_no_replace(src, fresh)
    check("fresh name copied", open(fresh).read() == "NEW AUDIO")

print("\n=== filename readings cover multi-part torrent naming ===")
check("'Artist - Album - NN. Title' reaches the bare title",
      "Where Were You - Live" in harvest.candidate_guesses(
          "Alan Jackson - Drive - 13. Where Were You - Live.flac"),
      str(harvest.candidate_guesses("Alan Jackson - Drive - 13. Where Were You - Live.flac")))
check("'NN - Title' still strips the prefix",
      harvest.candidate_guesses("03 - Song (live).flac")[0] == "Song (live)")
check("'Artist - Title' still offers the tail",
      "Song" in harvest.candidate_guesses("Artist - Song.flac"))

print("\n=== a parenthetical want is reachable from a filename ===")
with tempfile.TemporaryDirectory() as d:
    lib = os.path.join(d, "lib")
    downloads = os.path.join(d, "downloads", "Some Album [FLAC]")
    os.makedirs(lib)
    os.makedirs(downloads)
    with open(os.path.join(downloads, "03 - Song (live).flac"), "w") as fh:
        fh.write("AUDIO BYTES")

    conn = db.init(os.path.join(d, "t.db"))
    # Attributed to an album, because a track number only means something on one. "Singles"
    # deliberately carries no position — every song in it is track 1 of its own single.
    wid, _ = db.add_want(conn, "Arrogant Worms", "Song (live)", "Live Bait", "2003",
                         duration=100.0)

    # The download stub reports honest tags; the environment points every module at the tmp tree.
    old = (harvest.DOWNLOAD_DIRS, worker.LIBRARY, scan.probe, worker.tag)
    tagged = []
    harvest.DOWNLOAD_DIRS = [os.path.dirname(downloads)]
    worker.LIBRARY = lib
    scan.probe = lambda path: {
        "tag_title": "Song (live)", "tag_artist": "Arrogant Worms", "tag_album": None,
        "tag_album_artist": "Arrogant Worms", "tag_year": None, "tag_track": 3,
        "duration": 100.0, "codec": "flac",
        "bitrate": 900000, "sample_rate": 44100, "bit_depth": 16}
    worker.tag = lambda path, want, track=None: tagged.append((path, track)) or True
    try:
        r = harvest.harvest(conn, dry_run=False, log=lambda m: None)
        check("the want was matched and imported", r["imported"] == 1, str(r))
        dest = os.path.join(lib, "Arrogant Worms", "Live Bait (2003)", "03 - Song (live).flac")
        check("placed at the tag's own track number", os.path.exists(dest), dest)
        check("tag written with the source track, not 1",
              tagged and tagged[0][1] == 3, str(tagged))
        row = conn.execute("SELECT status, provider FROM wants WHERE id=?", (wid,)).fetchone()
        check("want satisfied by harvest",
              row["status"] == db.STATUS_HAVE and row["provider"] == "harvest",
              f"{row['status']}/{row['provider']}")
        check("imported file is in the library index straight away",
              conn.execute("SELECT 1 FROM files WHERE path=?", (dest,)).fetchone() is not None)
        r2 = harvest.harvest(conn, dry_run=False, log=lambda m: None)
        check("second run imports nothing (idempotent)", r2["imported"] == 0, str(r2))
    finally:
        harvest.DOWNLOAD_DIRS, worker.LIBRARY, scan.probe, worker.tag = old

print("\n=== an interrupted harvest finishes instead of being skipped for ever ===")
# The copy, the tag, the index and the want's status are four separate steps. A process
# killed between any two of them left a complete playable file at the canonical name
# with the want still pending, and every later harvest hit `if os.path.exists(dest)`
# and gave up. Three of those states were reproduced by the audit; this covers them.
for label, pre_tag, pre_index in [
    ("killed after the copy, before tag/index/status", False, False),
    ("killed after tagging, before index/status", True, False),
    ("killed after indexing, before the want's status", True, True),
]:
    with tempfile.TemporaryDirectory() as d:
        lib = os.path.join(d, "lib")
        downloads = os.path.join(d, "downloads", "Some Album [FLAC]")
        os.makedirs(lib)
        os.makedirs(downloads)
        with open(os.path.join(downloads, "03 - Song.flac"), "w") as fh:
            fh.write("AUDIO BYTES")

        conn = db.init(os.path.join(d, "t.db"))
        wid, _ = db.add_want(conn, "Arrogant Worms", "Song", "Live Bait", "2003",
                             duration=100.0)

        old = (harvest.DOWNLOAD_DIRS, worker.LIBRARY, scan.probe, worker.tag)
        tagged = []
        harvest.DOWNLOAD_DIRS = [os.path.dirname(downloads)]
        worker.LIBRARY = lib
        scan.probe = lambda path: {
            "tag_title": "Song", "tag_artist": "Arrogant Worms", "tag_album": None,
            "tag_album_artist": "Arrogant Worms", "tag_year": None, "tag_track": 3,
            "duration": 100.0, "codec": "flac",
            "bitrate": 900000, "sample_rate": 44100, "bit_depth": 16}
        worker.tag = lambda path, want, track=None: tagged.append((path, track)) or True
        try:
            # Reproduce the interrupted state: the file is published, the rest is not done.
            dest = os.path.join(lib, "Arrogant Worms", "Live Bait (2003)", "03 - Song.flac")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w") as fh:
                fh.write("AUDIO BYTES")
            if pre_tag:
                tagged.append((dest, 3))
            if pre_index:
                scan.index_file(conn, lib, dest)

            r = harvest.harvest(conn, dry_run=False, log=lambda m: None)
            row = conn.execute("SELECT status, provider, file_path FROM wants WHERE id=?",
                               (wid,)).fetchone()
            check(f"{label}: the want ends up satisfied",
                  row["status"] == db.STATUS_HAVE, f"{label}: {row['status']}")
            check(f"{label}: the want points at the published file",
                  row["file_path"] == dest, str(row["file_path"]))
            check(f"{label}: the file is in the library index",
                  conn.execute("SELECT 1 FROM files WHERE path=?", (dest,)).fetchone() is not None)
            check(f"{label}: the file was tagged for the want that asked",
                  any(t[0] == dest for t in tagged), str(tagged))
            check(f"{label}: nothing was copied over the published bytes",
                  open(dest).read() == "AUDIO BYTES")
            r2 = harvest.harvest(conn, dry_run=False, log=lambda m: None)
            check(f"{label}: a further run does nothing", r2["imported"] == 0, str(r2))
        finally:
            harvest.DOWNLOAD_DIRS, worker.LIBRARY, scan.probe, worker.tag = old

print("\n=== a name another want already holds is still never taken ===")
with tempfile.TemporaryDirectory() as d:
    lib = os.path.join(d, "lib")
    downloads = os.path.join(d, "downloads", "Some Album [FLAC]")
    os.makedirs(lib)
    os.makedirs(downloads)
    with open(os.path.join(downloads, "03 - Song.flac"), "w") as fh:
        fh.write("AUDIO BYTES")

    conn = db.init(os.path.join(d, "t.db"))
    wid, _ = db.add_want(conn, "Arrogant Worms", "Song", "Live Bait", "2003", duration=100.0)
    other, _ = db.add_want(conn, "Arrogant Worms", "Something Else", "Live Bait", "2003",
                           duration=100.0)

    old = (harvest.DOWNLOAD_DIRS, worker.LIBRARY, scan.probe, worker.tag)
    harvest.DOWNLOAD_DIRS = [os.path.dirname(downloads)]
    worker.LIBRARY = lib
    scan.probe = lambda path: {
        "tag_title": "Song", "tag_artist": "Arrogant Worms", "tag_album": None,
        "tag_album_artist": "Arrogant Worms", "tag_year": None, "tag_track": 3,
        "duration": 100.0, "codec": "flac",
        "bitrate": 900000, "sample_rate": 44100, "bit_depth": 16}
    worker.tag = lambda path, want, track=None: True
    try:
        dest = os.path.join(lib, "Arrogant Worms", "Live Bait (2003)", "03 - Song.flac")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w") as fh:
            fh.write("SOMEBODY ELSE'S AUDIO")
        # The other want already recorded that exact path as its own.
        conn.execute("UPDATE wants SET file_path=?, status=? WHERE id=?",
                     (dest, db.STATUS_HAVE, other))
        conn.commit()

        harvest.harvest(conn, dry_run=False, log=lambda m: None)
        check("the other want's file was not overwritten",
              open(dest).read() == "SOMEBODY ELSE'S AUDIO")
        row = conn.execute("SELECT status FROM wants WHERE id=?", (wid,)).fetchone()
        check("and this want was not falsely satisfied",
              row["status"] != db.STATUS_HAVE, row["status"])
    finally:
        harvest.DOWNLOAD_DIRS, worker.LIBRARY, scan.probe, worker.tag = old

print(f"\n{bad} failure(s)")
sys.exit(1 if bad else 0)
