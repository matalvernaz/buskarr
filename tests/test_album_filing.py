"""Songs added one at a time file into the album their artist already has, under the artist's real name.

A list imported song by song (2026-10-05..10, about a thousand songs through nextup) arrived with
each song's catalogue album label, so one record became several directories: "Take Me To The Alley"
beside "Take Me to the Alley", four for "Hello, I Must Be Going", "Greatest Hits" beside "Greatest
Hits (Deluxe)". And every albumartist tag carried the directory's sanitised name, so Jellyfin listed
"AC_DC" and "Wheeler Walker Jr" beside the "AC/DC" and "Wheeler Walker Jr." in the credits.

Locked down here: what counts as the same album (``db.album_fold``), which existing album a song
joins (``db.canonical_album``), that only the song-by-song paths do it, the one display spelling per
directory (``lead_display``) and the tag written from it, the repair rule that used to write the
directory name, and ``albums``, which brings the already-split albums together in two phases.
"""
import base64
import json
import os
import shutil
import sys
import tempfile

LIB = tempfile.mkdtemp(prefix="buskarr-albums-lib-")
os.environ["LIBRARY_DIR"] = LIB
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mutagen.flac import FLAC  # noqa: E402

from buskarr import albums, db, repair, scan, worker  # noqa: E402

FLAC_SILENCE = base64.b64decode(
    "ZkxhQwAAACICQAJAAAANAAANAfQA8AAAAZBrQxvy2nyTErPlwh5n9lkbBAAALAwAAABMYXZmNjEuNy4xMDMB"
    "AAAAFAAAAGVuY29kZXI9TGF2ZjYxLjcuMTAzgQAAAP/4dAgAAY8kAAAAfV8=")

# CI has no ffprobe; these tests only need the probe not to fail.
scan.probe = lambda path: {}

bad = 0


def check(label, ok, detail=""):
    global bad
    if not ok:
        bad += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{('  — ' + detail) if detail else ''}")


def fresh():
    d = tempfile.mkdtemp(prefix="buskarr-albums-db-")
    return db.init(os.path.join(d, "t.db")), d


def held(conn, artist, title, album, year=None, batch=None, provider="soulseek", lead=None):
    """A want that is already on disk, with a real FLAC where destination() puts it."""
    wid, _ = db.add_want(conn, artist, title, album, year, 200.0, "tester", batch=batch,
                         artist_lead=lead, allow_dup=True)
    w = conn.execute("SELECT * FROM wants WHERE id=?", (wid,)).fetchone()
    path = worker.destination(w, ".flac")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(FLAC_SILENCE)
    worker.tag(path, w)
    conn.execute("UPDATE wants SET status=?, provider=?, file_path=? WHERE id=?",
                 (db.STATUS_HAVE, provider, path, wid))
    conn.commit()
    return wid, path


try:
    print("=== the same record under packaging labels folds equal ===")
    same = [
        ("Take Me To The Alley", "Take Me to the Alley"),
        ("Greatest Hits (Deluxe)", "Greatest Hits"),
        ("Greatest Hits (Deluxe Edition)", "Greatest Hits"),
        ("Led Zeppelin IV (Remaster)", "Led Zeppelin IV (Deluxe Edition)"),
        ("Hello, I Must Be Going (2016 Remaster)", "Hello, I Must Be Going!"),
        ("Hello, I Must Be Going! (Remastered)", "Hello, I Must Be Going! (Deluxe Edition)"),
        ("...But Seriously (2016 Remaster)", "...But Seriously (Deluxe Edition)"),
        ("The Globe Sessions (Bonus Track Version)", "The Globe Sessions"),
        ("Superunknown (20th Anniversary)", "Superunknown (Super Deluxe)"),
        ("Some Girls (Deluxe Version)", "Some Girls"),
        ("ECHO (dj-Jo Remix) - Single", "ECHO (dj-Jo remix)"),
        ("There's A Riot Goin' On: The Coasters On Atco",
         "There’s a Riot Goin’ On: The Coasters On Atco"),
        ("Sunshine on Leith (2011 Remaster)", "Sunshine on Leith"),
        ("Collected (Collector's Edition)", "Collected"),
    ]
    for a, b in same:
        check(f"{a!r} == {b!r}", db.album_fold(a) == db.album_fold(b),
              f"{db.album_fold(a)!r} vs {db.album_fold(b)!r}")

    print("\n=== a different release does not ===")
    different = [
        ("Fearless (Taylor's Version)", "Fearless"),
        ("Red (Taylor’s version)", "Red"),
        ("Midnights (The Late Night Edition)", "Midnights"),
        ("1989 (Big Machine Radio release special)", "1989"),
        ("Fearless (platinum edition)", "Fearless"),
        ("discography (2015)", "discography (2016)"),
        ("discography (2015)", "Discography 2015-2016"),
        ("Unplugged (Live)", "Unplugged"),
        ("Anti-Hero (acoustic version)", "Anti-Hero (Kungs remix extended version)"),
        ("The Life of a Showgirl (Track by Track version)", "The Life of a Showgirl"),
    ]
    for a, b in different:
        check(f"{a!r} != {b!r}", db.album_fold(a) != db.album_fold(b),
              f"both {db.album_fold(a)!r}")
    check("a name made only of packaging still has a key",
          db.album_fold("Deluxe") == "deluxe" or db.album_fold("Deluxe") != "")

    print("\n=== canonical_album: which existing album a song joins ===")
    conn, d = fresh()
    for i in range(3):
        held(conn, "Phil Collins", f"Song {i}", "Hello, I Must Be Going (2016 Remaster)")
    held(conn, "Phil Collins", "Other", "Hello, I Must Be Going!")
    got = db.canonical_album(conn, "Phil Collins", "Hello, I Must Be Going! (Deluxe Edition)")
    check("no exact spelling: the album holding the most songs",
          got == ("Hello, I Must Be Going (2016 Remaster)", None), str(got))
    got = db.canonical_album(conn, "Phil Collins", "Hello, I Must Be Going!")
    check("an existing exact spelling is kept (an add may keep an edition apart on purpose)",
          got == ("Hello, I Must Be Going!", None), str(got))
    got = db.canonical_album(conn, "Phil Collins", "No Jacket Required", "1985")
    check("nothing alike: the catalogue's label stands", got == ("No Jacket Required", "1985"))
    got = db.canonical_album(conn, "Genesis", "Hello, I Must Be Going!")
    check("another artist's album is never joined", got == ("Hello, I Must Be Going!", None))
    held(conn, "Psychostick", "Beer", "Sandwich", "2009")
    got = db.canonical_album(conn, "Psychostick", "Sandwich", None)
    check("an undated label takes the dated album's year (one directory, not two)",
          got == ("Sandwich", "2009"), str(got))
    got = db.canonical_album(conn, "Psychostick", "Sandwich", "2016")
    check("two known years far apart are different releases", got == ("Sandwich", "2016"), str(got))
    got = db.canonical_album(conn, "Psychostick", "Sandwich", "2010")
    check("a year either side is the same release", got == ("Sandwich", "2009"), str(got))
    held(conn, "The Proclaimers", "Letter From America", "Sunshine on Leith", "1988")
    got = db.canonical_album(conn, "The Proclaimers", "Sunshine on Leith (2011 Remaster)", "2011")
    check("an edition's later year does not keep it out of the original",
          got == ("Sunshine on Leith", "1988"), str(got))
    held(conn, "Peter Gabriel", "Solsbury Hill", "Peter Gabriel", "1977")
    got = db.canonical_album(conn, "Peter Gabriel", "Peter Gabriel", "1980")
    check("but the same name years apart is another album (Peter Gabriel 1 and 3)",
          got == ("Peter Gabriel", "1980"), str(got))

    print("\n=== a search row takes album and year from one catalogue ===")
    from buskarr import catalog
    deezer = [{"source": "deezer", "artist": "Phil Collins", "title": "Thru These Walls",
               "duration": 302, "album": "Hello, I Must Be Going! (2016 Remaster)"}]
    itunes = [{"source": "itunes", "artist": "Phil Collins", "title": "Thru These Walls",
               "duration": 302, "album": "Hello, I Must Be Going!", "year": "1982"}]
    row = catalog.merge_tracks([deezer, itunes])[0]
    check("another catalogue's year for another edition is not borrowed",
          (row["album"], row.get("year")) == ("Hello, I Must Be Going! (2016 Remaster)", None),
          str((row["album"], row.get("year"))))
    itunes_same = [dict(itunes[0], album="hello, i must be going! (2016 remaster)", year="2016")]
    row = catalog.merge_tracks([deezer, itunes_same])[0]
    check("the same album's year is", row.get("year") == "2016", str(row.get("year")))
    bare = [dict(deezer[0], album="")]
    row = catalog.merge_tracks([bare, itunes])[0]
    check("a row with no album takes album and year together",
          (row["album"], row.get("year")) == ("Hello, I Must Be Going!", "1982"))

    print("\n=== only the song-by-song paths match ===")
    wid, _ = db.add_want(conn, "Phil Collins", "I Missed Again", "Hello, I Must Be Going (Deluxe)",
                         None, 220.0, "defender", match_album=True, track_no=4)
    w = conn.execute("SELECT * FROM wants WHERE id=?", (wid,)).fetchone()
    check("a song-by-song add joins the album already on disk",
          w["album"] == "Hello, I Must Be Going (2016 Remaster)", repr(w["album"]))
    check("its catalogue position, from another edition, is not carried over", w["track_no"] is None)
    wid, _ = db.add_want(conn, "Phil Collins", "Thru These Walls", "Hello, I Must Be Going (Deluxe)",
                         None, 300.0, "matt", track_no=2)
    w = conn.execute("SELECT * FROM wants WHERE id=?", (wid,)).fetchone()
    check("an album or artist add files as listed", w["album"] == "Hello, I Must Be Going (Deluxe)")
    held(conn, "Gregory Porter", "Holding On", "Take Me To The Alley")
    wid, _ = db.add_want(conn, "Gregory Porter", "Insanity", "Take Me to the Alley", None, 330.0,
                         "defender", match_album=True, track_no=3)
    w = conn.execute("SELECT * FROM wants WHERE id=?", (wid,)).fetchone()
    check("a capitalisation variant joins the existing spelling",
          w["album"] == "Take Me To The Alley", repr(w["album"]))
    check("and keeps its position, being the same edition", w["track_no"] == 3)
    conn.close()
    shutil.rmtree(d, ignore_errors=True)

    print("\n=== a duet added on its own files under its lead's directory ===")
    conn, d = fresh()
    held(conn, "Zedd", "Clarity", "Clarity", "2012")
    held(conn, "The Longest Johns", "Wellerman", "Smoke & Oakum", "2018")
    held(conn, "AC/DC", "Thunderstruck", "The Razors Edge", "1990")
    lead = lambda wid: conn.execute("SELECT artist_lead FROM wants WHERE id=?", (wid,)).fetchone()[0]
    wid, _ = db.add_want(conn, "Zedd & Alessia Cara", "Stay", "Stay", "2017", 210.0, "defender",
                         match_album=True)
    check("an & credit joins the lead's existing directory", lead(wid) == "Zedd", lead(wid))
    wid, _ = db.add_want(conn, "Simon & Garfunkel", "Mrs. Robinson", "Bookends", "1968", 244.0,
                         "defender", match_album=True)
    check("a band named with & stays itself when no lead directory exists",
          lead(wid) == "Simon & Garfunkel", lead(wid))
    wid, _ = db.add_want(conn, "Celtic Woman feat. The Longest Johns", "Song", "An Album", None,
                         200.0, "defender", match_album=True)
    check("a guest appearance stays with the act whose release it is",
          lead(wid) == "Celtic Woman", lead(wid))
    wid, _ = db.add_want(conn, "AC/DC & Someone", "Duet", "A Single", None, 200.0, "defender",
                         match_album=True)
    check("a lead whose directory name was sanitised is still found", lead(wid) == "AC_DC", lead(wid))
    wid, _ = db.add_want(conn, "Zedd & Foxes", "Clarity (Live)", "Live", None, 280.0, "matt",
                         artist_lead="Foxes")
    check("a lead the caller names is kept", lead(wid) == "Foxes", lead(wid))
    wid, _ = db.add_want(conn, "Zedd & Hayley Williams", "Stay the Night", "Clarity", "2012",
                         217.0, "matt")
    check("an album or artist add is unchanged", lead(wid) == "Zedd & Hayley Williams", lead(wid))
    conn.close()
    shutil.rmtree(d, ignore_errors=True)

    print("\n=== one display spelling per artist directory ===")
    check("AC/DC from its directory", db.lead_display_for("AC/DC", "AC_DC") == "AC/DC")
    check("the full stop the directory dropped",
          db.lead_display_for("Wheeler Walker Jr.", "Wheeler Walker Jr") == "Wheeler Walker Jr.")
    check("the lead of a featuring credit",
          db.lead_display_for("The Notorious B.I.G. feat. Mase", "The Notorious B.I.G")
          == "The Notorious B.I.G.")
    check("the lead of an & credit",
          db.lead_display_for("AC/DC & Someone", "AC_DC") == "AC/DC")
    check("a compilation keeps its directory name",
          db.lead_display_for("Grumpy Old Men", "Various Artists") == "Various Artists")
    conn, d = fresh()
    a, _ = db.add_want(conn, "AC/DC", "Thunderstruck", "The Razors Edge", "1990", 292.0, "t")
    row = conn.execute("SELECT artist_lead, lead_display FROM wants WHERE id=?", (a,)).fetchone()
    check("add_want stores the directory and the display spelling",
          (row["artist_lead"], row["lead_display"]) == ("AC_DC", "AC/DC"), str(tuple(row)))
    for i in range(3):
        db.add_want(conn, "Thomas Benjamin Wild Esq.", f"Song {i}", None, None, 100.0, "t")
    b, _ = db.add_want(conn, "Thomas Benjamin Wild Esq", "Another", None, None, 100.0, "t")
    row = conn.execute("SELECT artist_lead, lead_display FROM wants WHERE id=?", (b,)).fetchone()
    check("a later credit without the full stop takes the directory's agreed spelling",
          row["lead_display"] == "Thomas Benjamin Wild Esq.", str(tuple(row)))
    conn.execute("UPDATE wants SET lead_display=NULL")
    conn.commit()
    db.backfill_lead_display(conn)
    spellings = {r[0] for r in conn.execute(
        "SELECT lead_display FROM wants WHERE artist_lead='Thomas Benjamin Wild Esq'")}
    check("the backfill gives a mixed directory one spelling, the majority's",
          spellings == {"Thomas Benjamin Wild Esq."}, str(spellings))

    print("\n=== the albumartist tag says it ===")
    _, path = held(conn, "AC/DC", "Back in Black", "Back in Black", "1980")
    check("tag() writes the display spelling, not the directory's",
          FLAC(path).get("albumartist") == ["AC/DC"], str(FLAC(path).get("albumartist")))
    stale = dict(conn.execute("SELECT * FROM wants WHERE title='Back in Black'").fetchone())
    stale["lead_display"] = "Somebody Else"
    check("a display spelling that no longer names the directory is not trusted",
          worker.album_artist(stale) == "AC/DC", worker.album_artist(stale))
    conn.close()
    shutil.rmtree(d, ignore_errors=True)

    print("\n=== fold tags a folded duet with its lead's display spelling ===")
    from buskarr import fold
    conn, d = fresh()
    held(conn, "AC/DC", "Thunderstruck", "The Razors Edge", "1990")
    duet_dir = os.path.join(LIB, "AC_DC & Someone", "A Single")
    os.makedirs(duet_dir, exist_ok=True)
    duet = os.path.join(duet_dir, "Duet.flac")
    with open(duet, "wb") as fh:
        fh.write(FLAC_SILENCE)
    fold.fold(conn, root=LIB, dry_run=False, log=lambda *a: None)
    folded = os.path.join(LIB, "AC_DC", "A Single", "Duet.flac")
    check("the duet folder folded into the lead's", os.path.exists(folded) and not os.path.exists(duet))
    check("and its albumartist is the lead as spelled, not the directory",
          os.path.exists(folded) and FLAC(folded).get("albumartist") == ["AC/DC"],
          str(FLAC(folded).get("albumartist")) if os.path.exists(folded) else "missing")
    conn.close()
    shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(os.path.join(LIB, "AC_DC"), ignore_errors=True)

    print("\n=== the repair rule writes the display spelling ===")
    dirs = {"AC_DC", "Jonathan Coulton", "Moon Hooch"}
    check("AC_DC becomes AC/DC",
          repair.album_artist_fix("AC_DC", "AC_DC", dirs, "AC/DC") == "AC/DC")
    check("the dropped full stop comes back",
          repair.album_artist_fix("Wheeler Walker Jr", "Wheeler Walker Jr", dirs,
                                  "Wheeler Walker Jr.") == "Wheeler Walker Jr.")
    check("a tag already right is left alone",
          repair.album_artist_fix("AC/DC", "AC_DC", dirs, "AC/DC") is None)
    check("with no display known, a tag richer than the folder name is NOT flattened to it",
          repair.album_artist_fix("AC/DC", "AC_DC", dirs, None) is None
          and repair.album_artist_fix("M.I.A.", "M.I.A", dirs, None) is None)
    check("an empty tag is filled with the display spelling",
          repair.album_artist_fix("", "AC_DC", dirs, "AC/DC") == "AC/DC")
    check("a misfiled album naming another act is still left visible",
          repair.album_artist_fix("Moon Hooch", "Jonathan Coulton", dirs, None) is None)

    print("\n=== albums: bringing an already-split album together ===")
    conn, d = fresh()
    keep = [held(conn, "Fleetwood Mac", f"Hit {i}", "Greatest Hits")[1] for i in range(3)]
    wid, stray = held(conn, "Fleetwood Mac", "Dreams", "Greatest Hits (Deluxe Edition)")
    pend, _ = db.add_want(conn, "Fleetwood Mac", "Gypsy", "Greatest Hits (Deluxe)", None, 260.0,
                          "defender")
    for i in range(2):
        held(conn, "Taylor Swift", f"Bonus {i}", "Midnights (3am Edition)", "2022", batch="b1")
    held(conn, "Taylor Swift", "Anti-Hero", "Midnights (Deluxe)", "2022", batch="b1")
    held(conn, "Taylor Swift", "Lavender Haze", "Midnights", "2022", batch="b1")
    held(conn, "Psychostick", "Beer", "Sandwich", "2003")
    held(conn, "Psychostick", "Numbers", "Sandwich", "2009")
    held(conn, "The Proclaimers", "500 Miles", "Sunshine on Leith", "1988")
    held(conn, "The Proclaimers", "Sky Takes the Soul", "Sunshine on Leith (2011 Remaster)", "2011")
    groups, skipped = albums.plan(conn)
    leads = {g["lead"] for g in groups}
    check("the song-by-song split is planned", "Fleetwood Mac" in leads, str(leads))
    check("an artist add's editions are left apart", "Taylor Swift" not in leads)
    check("the same name years apart is reported, not merged",
          any(s[0] == "Psychostick" for s in skipped) and "Psychostick" not in leads)
    check("an edition with its own later year is still brought in", "The Proclaimers" in leads)
    g = next(g for g in groups if g["lead"] == "Fleetwood Mac")
    check("it settles on the spelling most songs already use",
          g["settled"] == ("Greatest Hits", None), str(g["settled"]))
    before = os.path.exists(stray)
    albums.move(conn, dry_run=True, log=lambda *a: None)
    check("a dry run moves nothing", before and os.path.exists(stray))

    result = albums.move(conn, dry_run=False, log=lambda *a: None, ledger_dir=d)
    moved = conn.execute("SELECT * FROM wants WHERE id=?", (wid,)).fetchone()
    target_dir = os.path.dirname(keep[0])
    check("the stray file moved into the kept album's directory",
          os.path.dirname(moved["file_path"]) == target_dir and not os.path.exists(stray),
          moved["file_path"])
    check("its want names the kept album", moved["album"] == "Greatest Hits")
    check("its tags are untouched by the move (Jellyfin re-links on them)",
          FLAC(moved["file_path"]).get("album") == ["Greatest Hits (Deluxe Edition)"],
          str(FLAC(moved["file_path"]).get("album")))
    p = conn.execute("SELECT album, file_path FROM wants WHERE id=?", (pend,)).fetchone()
    check("a want still pending is relabelled too, so it lands there", p["album"] == "Greatest Hits"
          and p["file_path"] is None)
    check("the emptied directory is gone", not os.path.isdir(os.path.dirname(stray)))
    entries = json.load(open(result["ledger"]))
    check("the ledger records the move",
          any(e["old_path"] == stray and e["path"] == moved["file_path"] for e in entries))
    again, _ = albums.plan(conn)
    check("a second plan finds nothing left to do for it",
          not any(g["lead"] == "Fleetwood Mac" for g in again))

    albums.retag(conn, result["ledger"], dry_run=False, log=lambda *a: None)
    tags = FLAC(moved["file_path"])
    check("retag writes the kept album", tags.get("album") == ["Greatest Hits"], str(tags.get("album")))
    check("and the display albumartist", tags.get("albumartist") == ["Fleetwood Mac"])
    check("and leaves the title alone", tags.get("title") == ["Dreams"])
    conn.close()
    shutil.rmtree(d, ignore_errors=True)
finally:
    shutil.rmtree(LIB, ignore_errors=True)

print(f"\n{bad} failure(s)")
sys.exit(1 if bad else 0)
