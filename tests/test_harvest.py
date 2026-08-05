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
    wid, _ = db.add_want(conn, "Arrogant Worms", "Song (live)", duration=100.0)

    # The download stub reports honest tags; the environment points every module at the tmp tree.
    old = (harvest.DOWNLOAD_DIRS, worker.LIBRARY, scan.probe, worker.tag)
    tagged = []
    harvest.DOWNLOAD_DIRS = [os.path.dirname(downloads)]
    worker.LIBRARY = lib
    scan.probe = lambda path: {
        "tag_title": "Song (live)", "tag_artist": "Arrogant Worms", "tag_album": None,
        "tag_year": None, "tag_track": 3, "duration": 100.0, "codec": "flac",
        "bitrate": 900000, "sample_rate": 44100, "bit_depth": 16}
    worker.tag = lambda path, want, track=None: tagged.append((path, track)) or True
    try:
        r = harvest.harvest(conn, dry_run=False, log=lambda m: None)
        check("the want was matched and imported", r["imported"] == 1, str(r))
        dest = os.path.join(lib, "Arrogant Worms", "Singles", "03 - Song (live).flac")
        check("placed at the tag's own track number", os.path.exists(dest), dest)
        check("tag written with the source track, not 1",
              tagged and tagged[0][1] == 3, str(tagged))
        row = conn.execute("SELECT status, provider FROM wants WHERE id=?", (wid,)).fetchone()
        check("want satisfied by harvest",
              row["status"] == db.STATUS_HAVE and row["provider"] == "harvest",
              f"{row['status']}/{row['provider']}")
        r2 = harvest.harvest(conn, dry_run=False, log=lambda m: None)
        check("second run imports nothing (idempotent)", r2["imported"] == 0, str(r2))
    finally:
        harvest.DOWNLOAD_DIRS, worker.LIBRARY, scan.probe, worker.tag = old

print(f"\n{bad} failure(s)")
sys.exit(1 if bad else 0)
