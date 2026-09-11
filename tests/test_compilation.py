"""A various-artists compilation is one album, not one album per track.

Every track of a compilation is by a different act, and both the library directory and the
``albumartist`` tag come from the track's credit. So adding "Schoolhouse Rock! Rocks" (15 tracks,
15 performers) placed each song in its own artist folder under a same-named directory, and wrote a
different ``albumartist`` on each — which Jellyfin groups on, so the release read as fifteen
one-track albums and the compilation itself could not be found anywhere. Hit 2026-09-10 on a real
request that came in through nextup.

The release's own credit is the only evidence: a tracklist whose performers all differ describes a
split single and a duets record just as well, and both of those have a real act to file under.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LIBRARY_DIR", "/tmp/buskarr-test-lib")
from buskarr import bulk, db, worker  # noqa: E402

bad = 0


def check(label, ok, detail=""):
    global bad
    if not ok:
        bad += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{('  — ' + detail) if detail else ''}")


COMPILATION = {
    "title": "Schoolhouse Rock! Rocks", "year": "1996", "artist": "Various Artists",
    "kind": "album",
    "tracks": [
        {"title": "I'm Just a Bill", "artist": "Deluxx Folk Implosion",
         "duration": 206.0, "track_no": 2},
        {"title": "Three Is a Magic Number", "artist": "Blind Melon",
         "duration": 194.0, "track_no": 3},
        {"title": "No More Kings", "artist": "Pavement", "duration": 263.0, "track_no": 6},
    ],
}

SOLO = {
    "title": "Wowee Zowee", "year": "1995", "artist": "Pavement", "kind": "album",
    "tracks": [
        {"title": "Rattled by the Rush", "artist": "Pavement", "duration": 263.0, "track_no": 1},
        {"title": "Grounded", "artist": "Pavement feat. Somebody", "duration": 240.0,
         "track_no": 2},
    ],
}


def add(conn, detail, **kw):
    """Run one album add against a stubbed catalogue fetch."""
    real = bulk.album_detail
    bulk.album_detail = lambda album_id, source="deezer": detail
    try:
        return bulk.add_album(conn, "stub-id", "tester", **kw)
    finally:
        bulk.album_detail = real


print("=== 1. the credit that names nobody is recognised ===")
for spelling in ("Various Artists", "various artists", "  VA ", "V/A",
                 "Verschiedene Interpreten"):
    check(f"{spelling!r} is a compilation credit", bulk.various_artists(spelling))
for spelling in ("Pavement", "", None, "Various Cruelties", "The Various"):
    check(f"{spelling!r} is not", not bulk.various_artists(spelling))

print("\n=== 2. a compilation's tracks share one folder ===")
with tempfile.TemporaryDirectory() as d:
    conn = db.init(os.path.join(d, "t.db"))
    r = add(conn, COMPILATION)
    check("all three tracks queued", r["added"] == 3, str(r))
    rows = list(conn.execute(
        "SELECT * FROM wants ORDER BY track_no"))
    leads = {r["artist_lead"] for r in rows}
    check("one artist_lead across the release", len(leads) == 1, str(leads))
    check("and it is the compilation folder",
          leads == {db.folder_key(bulk.VARIOUS_ARTISTS)}, str(leads))
    check("the per-track credit is untouched",
          [r["artist"] for r in rows] == ["Deluxx Folk Implosion", "Blind Melon", "Pavement"],
          str([r["artist"] for r in rows]))

    print("\n=== 3. placement and tagging follow, which is the visible half ===")
    dests = [worker.destination(r, ".flac") for r in rows]
    folders = {p.rsplit("/", 3)[-3] for p in dests}
    check("one library directory, not three", len(folders) == 1, str(folders))
    check("named for the compilation",
          folders == {db.folder_key(bulk.VARIOUS_ARTISTS)}, str(folders))
    check("the album directory keeps its title and year",
          all("/Schoolhouse Rock! Rocks (1996)/" in p for p in dests), str(dests[:1]))
    check("track numbers still come from the release",
          [os.path.basename(p)[:2] for p in dests] == ["02", "03", "06"],
          str([os.path.basename(p) for p in dests]))
    # tag() writes albumartist from this same helper, so agreeing here is agreeing there.
    check("albumartist is the compilation, per track",
          {worker.folder_artist(r) for r in rows} == {bulk.VARIOUS_ARTISTS},
          str({worker.folder_artist(r) for r in rows}))
    conn.close()

print("\n=== 4. an ordinary album is unchanged ===")
with tempfile.TemporaryDirectory() as d:
    conn = db.init(os.path.join(d, "t.db"))
    r = add(conn, SOLO)
    check("both tracks queued", r["added"] == 2, str(r))
    rows = list(conn.execute("SELECT * FROM wants ORDER BY track_no"))
    check("filed under the act, not the compilation folder",
          {r["artist_lead"] for r in rows} == {db.folder_key("Pavement")},
          str({r["artist_lead"] for r in rows}))
    check("a featuring credit still folds to its lead",
          rows[1]["artist"] == "Pavement feat. Somebody"
          and rows[1]["artist_lead"] == db.folder_key("Pavement"))
    conn.close()

print("\n=== 5. a caller supplying its own listing must supply the credit ===")
with tempfile.TemporaryDirectory() as d:
    conn = db.init(os.path.join(d, "t.db"))
    add(conn, COMPILATION,
        listing=(COMPILATION["title"], COMPILATION["year"], COMPILATION["tracks"]),
        album_artist=COMPILATION["artist"])
    leads = {r["artist_lead"] for r in conn.execute("SELECT artist_lead FROM wants")}
    check("complete_album's path files the same way",
          leads == {db.folder_key(bulk.VARIOUS_ARTISTS)}, str(leads))
    conn.close()

print(f"\n{bad} failure(s)")
raise SystemExit(1 if bad else 0)
