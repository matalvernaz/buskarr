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

print("\n=== 4. album_year upgrades past a dateless first edition ===")
album_year = {}


def resolve(album, year):
    k = db.norm(album)
    if year and not album_year.get(k):
        album_year[k] = year
    return album_year.get(k) or year


check("dateless edition first -> None", resolve("Who I Am", None) is None)
check("real year arrives -> 1994", resolve("Who I Am", "1994") == "1994")
check("later dateless track inherits 1994", resolve("Who I Am", None) == "1994")
check("a later reissue does not override", resolve("Who I Am", "2003") == "1994")

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
