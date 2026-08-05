"""Regression cases for the tag-repair gates.

The artist-mismatch rule proposes rewriting a file's artist tag to its folder name. Placement
deliberately files a collaboration under the LEAD artist while tagging it with the FULL credit, so
"A feat. B" sitting inside "A" is the system working — but the rule read it as a wrong tag and
would have stripped the featuring credit from every collaboration in the library. Reconciliation
keys on that exact credit string, so each rewrite would also have flipped the want to pending and
re-downloaded a file already held.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LIBRARY_DIR", "/music")
from buskarr import db, repair  # noqa: E402

bad = 0


def check(label, ok, detail=""):
    global bad
    if not ok:
        bad += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{('  — ' + detail) if detail else ''}")


def file_row(path, artist, title, lead, album_artist=None):
    return {"path": path, "artist": artist, "artist_lead": lead, "album": "Alb",
            # Defaults to the folder lead, which is what worker.tag writes for anything buskarr
            # placed. Leaving it NULL here would arm the albumartist rule on every fixture row and
            # obscure what the artist-mismatch cases below are actually asserting.
            "album_artist": lead if album_artist is None else album_artist,
            "title": title, "file_title": title, "tag_title": title, "track_no": 1,
            "year": None, "codec": "flac", "bitrate": 900000, "sample_rate": 44100,
            "bit_depth": 16, "duration": 100.0, "size": 1, "provider": None, "mtime": 1.0}


with tempfile.TemporaryDirectory() as d:
    conn = db.init(os.path.join(d, "t.db"))
    # Five solo files make "A" a consistently-used folder, which is what arms the mismatch rule.
    for i in range(5):
        db.upsert_file(conn, file_row(f"/music/A/Alb/{i:02d} - S{i}.flac", "A", f"S{i}", "A"))
    # The full collaboration credit of the folder's own lead: placement working as designed.
    db.upsert_file(conn, file_row("/music/A/Alb/06 - Collab.flac", "A feat. B", "Collab", "A"))
    # An "&" credit led by the folder artist: also theirs, also not a wrong tag.
    db.upsert_file(conn, file_row("/music/A/Alb/07 - Duo.flac", "A & His Orchestra", "Duo", "A"))
    # A genuinely foreign tag in the folder: this one the rule exists to catch.
    db.upsert_file(conn, file_row("/music/A/Alb/08 - Stray.flac", "Somebody Else", "Stray", "A"))
    # Correct artist tag, empty albumartist: the shape 517 files in the real library were in, and
    # the one a scoped run has to still reach.
    db.upsert_file(conn, file_row("/music/A/Alb/09 - NoAA.flac", "A", "NoAA", "A",
                                  album_artist=""))
    # A folder whose name has been through worker.safe() while the tags have not: "AC/DC" files
    # in the "AC_DC" directory. The five path-resolved rows arm the consistency gate; the tagged
    # collaboration must survive it, because safe(lead("AC/DC feat. B")) IS this folder.
    for i in range(5):
        db.upsert_file(conn, file_row(f"/music/AC_DC/Alb/{i:02d} - T{i}.flac",
                                      "AC_DC", f"T{i}", "AC_DC"))
    db.upsert_file(conn, file_row("/music/AC_DC/Alb/06 - Collab.flac",
                                  "AC/DC feat. B", "Collab", "AC_DC"))
    db.upsert_file(conn, file_row("/music/AC_DC/Alb/07 - Stray.flac",
                                  "Random Person", "Stray2", "AC_DC"))
    conn.commit()

    flagged = {r["path"]: (reason, proposed)
               for r, reason, proposed in repair.find_suspect(conn)}
    print("=== the folder's own lead credit is never a mismatch ===")
    check("'A feat. B' under folder A is left alone",
          "/music/A/Alb/06 - Collab.flac" not in flagged,
          str(flagged.get("/music/A/Alb/06 - Collab.flac", "")))
    check("'A & His Orchestra' under folder A is left alone",
          "/music/A/Alb/07 - Duo.flac" not in flagged,
          str(flagged.get("/music/A/Alb/07 - Duo.flac", "")))
    check("a genuinely foreign tag is still flagged",
          flagged.get("/music/A/Alb/08 - Stray.flac", ("", {}))[1].get("artist") == "A")
    check("the solo files are untouched",
          not any(f"/music/A/Alb/{i:02d} - S{i}.flac" in flagged for i in range(5)))
    print("=== sanitised folder names do not read as strangers ===")
    check("'AC/DC feat. B' under folder AC_DC is left alone",
          "/music/AC_DC/Alb/06 - Collab.flac" not in flagged,
          str(flagged.get("/music/AC_DC/Alb/06 - Collab.flac", "")))
    check("a foreign tag in the sanitised folder is still flagged",
          flagged.get("/music/AC_DC/Alb/07 - Stray.flac", ("", {}))[1].get("artist") == "AC_DC")

    print("=== a scoped run writes only the tags it was asked for ===")
    # The artist rule is far less sure of itself than the albumartist one — on a compilation it
    # proposes crediting a guest performer's track to the folder artist. Scoping is what lets the
    # safe rule be applied without the risky one riding along.
    written = []
    repair.write_tags = lambda path, **fields: written.append((path, fields)) or True
    repair.repair(conn, dry_run=False, fields=["album_artist"], log=lambda *a: None)
    check("no artist tag is written when only album_artist is requested",
          all("artist" not in fields for _, fields in written),
          str([f for _, f in written if "artist" in f][:2]))
    check("a file whose only defect is an empty album_artist is still reached",
          any(path == "/music/A/Alb/09 - NoAA.flac" and fields.get("album_artist") == "A"
              for path, fields in written))

print("=== container handlers ===")
# Opus is Ogg-framed but not Vorbis. Mapping it to OggVorbis made every .opus file in the library
# silently unrepairable — 18 of them, all of which survived a full repair run untouched.
import mutagen.oggopus  # noqa: E402
import mutagen.oggvorbis  # noqa: E402

check("opus files are opened with the Opus handler, not the Vorbis one",
      repair.OggOpus is mutagen.oggopus.OggOpus
      and repair.OggVorbis is mutagen.oggvorbis.OggVorbis)
with tempfile.TemporaryDirectory() as d:
    # An empty file is enough: the point is which handler is reached, and each raises its own
    # error type on malformed input.
    stub = os.path.join(d, "x.opus")
    open(stub, "wb").close()
    try:
        repair._open(stub)
        raised = None
    except Exception as exc:                       # noqa: BLE001 - the type is the assertion
        raised = type(exc).__module__
    check("a .opus path reaches mutagen.oggopus", raised == "mutagen.oggopus", str(raised))

print("=== albumartist normalisation ===")
# The directories this library actually has, so the rule can tell a spelling of the folder it is
# in from the name of a different act sitting inside it.
ARTIST_DIRS = {"Jonathan Coulton", "The Arrogant Worms", "Moon Hooch", "“Weird Al” Yankovic",
               "Tom Lehrer"}


def fix(existing, folder="“Weird Al” Yankovic"):
    return repair.album_artist_fix(existing, folder, ARTIST_DIRS)


check("an empty tag takes the folder spelling",
      fix("") == "“Weird Al” Yankovic" and fix(None) == "“Weird Al” Yankovic")
check("a tag already matching the folder is left alone",
      fix("“Weird Al” Yankovic") is None)
# The three spellings the library actually held, which showed as three artists in Jellyfin.
check("curly, straight and bare quote spellings all converge",
      fix('"Weird Al" Yankovic') == "“Weird Al” Yankovic"
      and fix("Weird Al Yankovic") == "“Weird Al” Yankovic")
check("a joined collaboration folds to its lead",
      fix('"Weird Al" Yankovic & Wendy Carlos') == "“Weird Al” Yankovic")
check("a featuring credit folds to its lead",
      repair.album_artist_fix("Jonathan Coulton feat. Ellen McLain",
                              "Jonathan Coulton", ARTIST_DIRS) == "Jonathan Coulton")
check("a leading article is accepted as the same act",
      repair.album_artist_fix("Arrogant Worms", "The Arrogant Worms",
                              ARTIST_DIRS) == "The Arrogant Worms")
# The whole point of the gate: a misfiled album must stay visible as one.
check("a tag naming a different act is left untouched",
      repair.album_artist_fix("Moon Hooch", "Jonathan Coulton", ARTIST_DIRS) is None
      and repair.album_artist_fix("Original Broadway Cast", "Tom Lehrer", ARTIST_DIRS) is None)
check("an act that is itself a directory is never folded by the article rule",
      repair.album_artist_fix("Moon Hooch", "The Moon Hooch", ARTIST_DIRS) is None)
# db.norm empties non-Latin names, so using it here would fold every mismatched tag into them.
check("a non-Latin folder does not swallow unrelated tags",
      repair.album_artist_fix("Someone Else", "坂本龍一", ARTIST_DIRS) is None)

print(f"\n{bad} failure(s)")
sys.exit(1 if bad else 0)

