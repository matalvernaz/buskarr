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


def trk(title, release, year, dur=100.0):
    return {"title": title, "artist": "Test Act", "album": release, "year": year,
            "duration": dur, "release": release, "release_title": release,
            "release_type": "album", "release_secondary": []}


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
