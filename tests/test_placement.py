"""Regression cases for file placement, database bootstrap, and album-year resolution.

Every case here is a defect that reached production on 2026-08-04 and was found by an external
review panel, then confirmed against the source:

  1. ``migrate()`` runs before the schema script and backfilled unconditionally, so a fresh
     ``/state`` volume could not boot at all — the disaster-recovery path was broken.
  2. Every "check exists then ``shutil.move``" was a check-then-act race, and ``rename(2)`` REPLACES.
     The one thing this project promises never to do is destroy audio it did not just create.
  3. ``refile`` compared a collision-suffixed file against the unsuffixed canonical path, so each run
     bumped it (2) -> (3) -> (4), contradicting its own idempotence docstring.
  4. ``album_year`` used ``setdefault``, so a first edition with no date poisoned the map permanently
     and the album split across directories anyway.
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


print("=== 1. a brand-new database can initialise ===")
with tempfile.TemporaryDirectory() as d:
    try:
        conn = db.init(os.path.join(d, "fresh.db"))
        n = conn.execute("SELECT COUNT(*) FROM wants").fetchone()[0]
        check("db.init() on an empty volume", True, f"wants table present, {n} rows")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(wants)")}
        check("wants.track_no present after init", "track_no" in cols)
        # And it must still be idempotent on a second open.
        conn2 = db.init(os.path.join(d, "fresh.db"))
        check("second init on the same file", True)
        conn.close(); conn2.close()
    except Exception as e:
        check("db.init() on an empty volume", False, f"{type(e).__name__}: {e}")

print("\n=== 2. place() never replaces existing audio ===")
with tempfile.TemporaryDirectory() as d:
    victim = os.path.join(d, "01 - Song.flac")
    with open(victim, "w") as fh:
        fh.write("ORIGINAL AUDIO")
    src = os.path.join(d, "staged.flac")
    with open(src, "w") as fh:
        fh.write("NEW AUDIO")
    final = worker.place(src, victim)
    check("existing file survived", open(victim).read() == "ORIGINAL AUDIO",
          f"contains {open(victim).read()!r}")
    check("new file placed alongside", final != victim and open(final).read() == "NEW AUDIO",
          os.path.basename(final))
    check("source consumed", not os.path.exists(src))

print("\n=== 3. refile does not churn a suffixed file ===")
canonical = "/music/A/Singles/01 - Song.flac"
from buskarr import refile  # noqa: E402
check("canonical path is settled", refile._settled(canonical, canonical))
check("suffixed sibling is settled", refile._settled("/music/A/Singles/01 - Song (2).flac",
                                                     canonical))
check("a genuinely wrong folder is NOT settled",
      not refile._settled("/music/A feat. B/Singles/01 - Song.flac", canonical))
check("a different song is NOT settled",
      not refile._settled("/music/A/Singles/01 - Other.flac", canonical))

with tempfile.TemporaryDirectory() as d:
    # Lyrics are written next to the audio by an external tool and are in no database, so a move
    # that leaves them behind orphans them where the audio used to be.
    src_dir, dst_dir = os.path.join(d, "old"), os.path.join(d, "new")
    os.makedirs(src_dir); os.makedirs(dst_dir)
    old_audio, new_audio = os.path.join(src_dir, "01 - S.flac"), os.path.join(dst_dir, "04 - S.flac")
    for p in (old_audio, os.path.join(src_dir, "01 - S.lrc"), os.path.join(src_dir, "01 - S.txt")):
        open(p, "w").write("x")
    open(new_audio, "w").write("x")           # audio already moved; sidecars follow it
    n = refile._move_sidecars(old_audio, new_audio, print)
    check("both sidecars followed the audio", n == 2
          and os.path.exists(os.path.join(dst_dir, "04 - S.lrc"))
          and os.path.exists(os.path.join(dst_dir, "04 - S.txt")), str(os.listdir(dst_dir)))
    check("no sidecar left behind", os.listdir(src_dir) == ["01 - S.flac"],
          str(os.listdir(src_dir)))
    check("a second call is a no-op", refile._move_sidecars(old_audio, new_audio, print) == 0)

print("\n=== 4. add_artist album keying: years per release, spelling survives a re-add ===")
from buskarr import catalog  # noqa: E402


class FakeArtistSource:
    """Catalogue stub for add_artist: canned (wanted, tracks, meta) triple."""

    name = "fake-mb"

    def __init__(self, tracks):
        self.tracks = tracks

    def artist_catalogue(self, ref):
        return "Test Act", self.tracks, {"complete": True}


def trk(title, release, year, dur=100.0, track_no=None):
    return {"title": title, "artist": "Test Act", "album": release, "year": year,
            "duration": dur, "release": release, "release_title": release,
            "release_type": "album", "release_secondary": [], "track_no": track_no}


with tempfile.TemporaryDirectory() as d:
    conn = db.init(os.path.join(d, "t.db"))
    real_sources = dict(catalog.SOURCES)
    catalog.SOURCES["fake-mb"] = FakeArtistSource([
        trk("Song E", "Debut", None), trk("Song F", "Debut", "1990"),
        trk("Song G", "Debut", None), trk("Song H", "Debut", "2003"),
        trk("Song I", "Debut (Deluxe Edition)", "2005"),
    ])
    try:
        bulk.add_artist(conn, "x", source="fake-mb")
        got = {r["title"]: (r["album"], r["year"]) for r in
               conn.execute("SELECT title, album, year FROM wants")}
        check("first real year wins for the release",
              got["Song F"] == ("Debut", "1990") and got["Song G"] == ("Debut", "1990")
              and got["Song H"] == ("Debut", "1990"), str(got))
        check("an edition does NOT share the base album's year slot",
              got["Song I"] == ("Debut (Deluxe Edition)", "2005"), str(got["Song I"]))
        # Re-add after the source respelled and re-dated the release: the new track must join
        # the existing spelling and year, not open "DEBUT (1996)" beside "Debut (1990)".
        catalog.SOURCES["fake-mb"] = FakeArtistSource([trk("Song J", "DEBUT", "1996")])
        bulk.add_artist(conn, "x", source="fake-mb")
        j = conn.execute("SELECT album, year FROM wants WHERE title='Song J'").fetchone()
        check("re-add adopts the existing spelling and year",
              (j["album"], j["year"]) == ("Debut", "1990"), str(dict(j)))
    finally:
        catalog.SOURCES.clear(); catalog.SOURCES.update(real_sources)

print("\n=== 4b. a first-time add does not fetch album + deluxe + hits of one master ===")
with tempfile.TemporaryDirectory() as d:
    conn = db.init(os.path.join(d, "t.db"))
    real_sources = dict(catalog.SOURCES)
    # Nothing held: the on-disk repackaging guard cannot fire, which is exactly the case that
    # downloaded Wheeler Walker's catalogue three times over.
    catalog.SOURCES["fake-mb"] = FakeArtistSource([
        trk("Redneck Shit", "Redneck Shit", "2016", 144.0),
        trk("Family Tree", "Redneck Shit", "2016", 200.0),
        trk("Live Cut", "Redneck Shit", "2016", 150.0),
        trk("Redneck Shit (2016 Remaster)", "Redneck Shit (Deluxe)", "2020", 144.0),
        trk("Family Tree (Remastered 2020)", "Redneck Shit (Deluxe)", "2020", 200.0),
        # Same title, genuinely different take — must NOT collapse.
        trk("Live Cut (live)", "Redneck Shit (Deluxe)", "2020", 233.0),
    ])
    try:
        r = bulk.add_artist(conn, "x", source="fake-mb")
        titles = {x["title"] for x in conn.execute("SELECT title FROM wants")}
        check("remastered reissues of held masters not re-queued",
              "Redneck Shit (2016 Remaster)" not in titles
              and "Family Tree (Remastered 2020)" not in titles, str(sorted(titles)))
        check("a different-length take of the same title survives",
              "Live Cut (live)" in titles and "Live Cut" in titles, str(sorted(titles)))
        check("three originals plus the distinct take queued", r["added"] == 4, str(r))
    finally:
        catalog.SOURCES.clear(); catalog.SOURCES.update(real_sources)

print("\n=== 4a2. an artist add numbers its wants from the listing ===")
with tempfile.TemporaryDirectory() as d:
    conn = db.init(os.path.join(d, "t.db"))
    real_sources = dict(catalog.SOURCES)
    catalog.SOURCES["fake-mb"] = FakeArtistSource([
        trk("One", "Alb", "2001", 100.0, track_no=1),
        trk("Two", "Alb", "2001", 110.0, track_no=2),
        trk("Three", "Alb", "2001", 120.0, track_no=3),
        trk("Loose", None, None, 130.0),          # no release position available
    ])
    try:
        bulk.add_artist(conn, "x", source="fake-mb")
    finally:
        catalog.SOURCES.clear(); catalog.SOURCES.update(real_sources)
    got = {r["title"]: r["track_no"] for r in
           conn.execute("SELECT title, track_no FROM wants")}
    # This is the defect the whole track_no column exists for. add_album passed the position from
    # day one; add_artist never did, so every want from the main add path was NULL and placement
    # fell back to the provider's own tag — six songs numbered 01 in one directory.
    check("every listing track carries its position",
          (got["One"], got["Two"], got["Three"]) == (1, 2, 3), str(got))
    check("a track with no listing position stays NULL, not 1",
          got["Loose"] is None, str(got))
    # Filenames must then be distinct, which is the symptom Matt actually sees.
    rows = conn.execute("SELECT * FROM wants WHERE album='Alb' ORDER BY track_no").fetchall()
    names = [os.path.basename(worker.destination(r, ".flac")) for r in rows]
    check("the placed filenames are distinct and contiguous",
          names == ["01 - One.flac", "02 - Two.flac", "03 - Three.flac"], str(names))

print("\n=== 4a3. two releases sharing a title cannot both number one directory ===")
with tempfile.TemporaryDirectory() as d:
    conn = db.init(os.path.join(d, "t.db"))
    real_sources = dict(catalog.SOURCES)
    # MusicBrainz lists a dozen editions of one album and Deezer carries the single and the album
    # under one name; each numbers its own tracklist from 1, and all of them file into ONE
    # directory. Verified live: MB returns 'Voyage' as [1,1,2,2,3,3,...].
    album = [trk("A", "Voyage", "2024", 100.0, track_no=1),
             trk("B", "Voyage", "2024", 110.0, track_no=2),
             trk("C", "Voyage", "2024", 120.0, track_no=3)]
    other_edition = [trk("A", "Voyage", "2024", 100.0, track_no=1),      # same song, deduped
                     trk("Bonus", "Voyage", "2024", 140.0, track_no=2)]  # new, position collides
    for t in album:
        t["release"] = "rel-1"
    for t in other_edition:
        t["release"] = "rel-2"
    catalog.SOURCES["fake-mb"] = FakeArtistSource(album + other_edition)
    try:
        bulk.add_artist(conn, "x", source="fake-mb")
    finally:
        catalog.SOURCES.clear(); catalog.SOURCES.update(real_sources)
    got = {r["title"]: r["track_no"] for r in
           conn.execute("SELECT title, track_no FROM wants")}
    check("the claiming release numbers the directory", (got["A"], got["B"], got["C"]) == (1, 2, 3),
          str(got))
    check("a bonus track from another edition is NOT given a colliding 2",
          got["Bonus"] is None, str(got))
    rows = conn.execute("SELECT * FROM wants").fetchall()
    names = sorted(os.path.basename(worker.destination(r, ".flac")) for r in rows)
    check("no two placed filenames share a number",
          len({n.split(" - ")[0] for n in names}) == len(names), str(names))

print("\n=== 4b2. what counts as one recording relabelled ===")
# The rule is asymmetric on purpose. A missed twin costs a duplicate download, which the audio
# sweep finds later; a wrong twin silently refuses something Matt asked for. So only PACKAGING
# words collapse, and any word that could name a different performance keeps the two apart even
# when the running times match — a remix can land a second from its original.
TWIN_CASES = [
    ("Song A", "Song A (2010 Remaster)", 200, 200, True, "remaster"),
    ("Chattahoochee (extended mix 2)", "Chattahoochee [extended mix 2] [*]", 246, 246, True,
     "bracket style only"),
    ("Second Song - Chiptune", "Second Song (Chiptune)", 150, 151, True,
     "dash suffix vs parenthetical"),
    ("Song A", "Song A (Radio Edit)", 172, 173, True, "radio edit is packaging"),
    ("Song A", "Song A (Explicit)", 200, 200, True, "explicit label"),
    ("Pants Drunk", "Pants Drunk (Electric Timebomb remix)", 228, 230, False, "a remix"),
    ("F'n It Up", "F'n It Up (Home Demo)", 219, 222, False, "a demo"),
    ("Money N Bitches", "Money N Bitches (Live 2023)", 209, 208, False, "a live take"),
    ("Anne Louise", "Anne Louise (2023 re-recorded version)", 149, 149, False, "a re-recording"),
    ("Wellerman", "Wellerman (Community version)", 142, 142, False, "a community version"),
    ("Song A", "Song A (Acoustic)", 200, 200, False, "acoustic"),
    ("Song A", "Song A (Karaoke Mix)", 200, 200, False, "karaoke"),
    ("Song A", "Song A (Live at Wembley)", 200, 265, False, "lengths disagree"),
    ("Intro", "Outro", 30, 30, False, "different songs, same length"),
]
for a, b, da, dbb, expect, why in TWIN_CASES:
    got = db._same_recording(a, b, da, dbb)
    check(f"{'twin' if expect else 'distinct'}: {why}", got == expect,
          f"got twin={got} for {a!r} vs {b!r}")

print("\n=== 4c. add_want's twin gate: edition noise never becomes a second want ===")
with tempfile.TemporaryDirectory() as d:
    conn = db.init(os.path.join(d, "t.db"))
    real_sources = dict(catalog.SOURCES)
    catalog.SOURCES["fake-mb"] = FakeArtistSource([
        trk("Song A", "First Album", "2001"), trk("Intro", "First Album", "2001", 30.0)])
    try:
        bulk.add_artist(conn, "x", source="fake-mb")
    finally:
        catalog.SOURCES.clear(); catalog.SOURCES.update(real_sources)


    def n_wants():
        return conn.execute("SELECT COUNT(*) FROM wants").fetchone()[0]

    # A compilation added on its own, months later. The old gate was release-scoped and could not
    # see that this remaster is the master already wanted from the original album.
    base, real_detail = n_wants(), bulk.album_detail
    bulk.album_detail = lambda ref, source="deezer": {
        "title": "Greatest Hits", "year": "2010", "artist": "Test Act", "kind": "album",
        "tracks": [{"title": "Song A (2010 Remaster)", "artist": "Test Act",
                    "duration": 100.0, "track_no": 1},
                   {"title": "Brand New Song", "artist": "Test Act",
                    "duration": 175.0, "track_no": 2}]}
    try:
        r = bulk.add_album(conn, "99", source="deezer")
    finally:
        bulk.album_detail = real_detail
    check("a later compilation does not re-queue a held master",
          r["added"] == 1 and n_wants() == base + 1, str(r))
    check("its genuinely new track still queues",
          db.find_want(conn, "Test Act", "Brand New Song") is not None)

    before = n_wants()
    wid, created = db.add_want(conn, "Test Act", "Song A (2016 Remaster)", None, None, 100.0)
    check("the manual Add form returns the existing want, not a new one",
          not created and n_wants() == before and wid == db.find_want(conn, "Test Act", "Song A"),
          f"created={created} wid={wid}")

    # Everything below must STILL be addable: the gate keys on duration agreement, so a real
    # alternate take survives, and allow_dup remains the deliberate override.
    before = n_wants()
    checks = [
        ("a different-LENGTH take of the same song",
         db.add_want(conn, "Test Act", "Song A (Live at Wembley)", None, None, 265.0)[1]),
        ("allow_dup still forces a second copy",
         db.add_want(conn, "Test Act", "Song A (Acoustic)", None, None, 100.0,
                     allow_dup=True)[1]),
        ("the same title by a DIFFERENT artist",
         db.add_want(conn, "Other Act", "Song A (2010 Remaster)", None, None, 100.0)[1]),
        ("a different song that happens to share a duration",
         db.add_want(conn, "Test Act", "Outro", None, None, 30.0)[1]),
        ("a want with no duration at all",
         db.add_want(conn, "Test Act", "Song B", None, None, None)[1]),
    ]
    for label, ok in checks:
        check(label + " is still added", ok)
    check("all five landed", n_wants() == before + 5, f"{n_wants() - before}")

print("\n=== 5. track number: the want's listing position beats the source file's tag ===")
w = {"artist": "A", "title": "Song", "album": "Alb", "year": "2003", "track_no": 4}
check("want track_no wins over the source tag",
      worker.destination(w, ".flac", 5).endswith("/04 - Song.flac"),
      worker.destination(w, ".flac", 5))
w_less = {"artist": "A", "title": "Song", "album": "Alb", "year": "2003"}
check("no want number falls back to the source tag",
      worker.destination(w_less, ".flac", 5).endswith("/05 - Song.flac"))
check("NULL want number falls back too",
      worker.destination(dict(w, track_no=None), ".flac", 7).endswith("/07 - Song.flac"))
check("neither number falls back to 01",
      worker.destination(w_less, ".flac", None).endswith("/01 - Song.flac"))
check("want_track and destination agree (tag uses the same helper)",
      worker.want_track(w, 5) == 4 and worker.want_track(w_less, 5) == 5)

print("\n=== 6. release ordering still albums-first ===")
buckets = {
    "single": [{"release_type": "Single", "release_secondary": [], "year": "1989"}],
    "album": [{"release_type": "Album", "release_secondary": [], "year": "1990"}],
    "unreleased": [{"release_type": "unreleased", "release_secondary": [], "year": "1995"}],
    "comp": [{"release_type": "Album", "release_secondary": ["Compilation"], "year": "2004"}],
}
order = [k for k, _ in sorted(buckets.items(), key=lambda kv: bulk.release_order(kv[1]))]
check("album < single < compilation < unreleased",
      order == ["album", "single", "comp", "unreleased"], str(order))

print(f"\n{bad} failure(s)")
raise SystemExit(1 if bad else 0)
