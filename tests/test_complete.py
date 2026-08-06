"""Regression cases for album completion — one held song queueing the rest of its release.

What must hold: the chosen release actually contains the seed song (match.vet, not the add_want
key), compilations and other artists' releases are refused, a same-spelling release reuses the
seed's attribution while a different edition keeps its own, the seed track is never re-queued,
an outage is an error rather than "no album", and the worker never routes an unknown job kind
into an artist add.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from buskarr import bulk, catalog, db, worker  # noqa: E402

bad = 0


def check(label, ok, detail=""):
    global bad
    if not ok:
        bad += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{('  — ' + detail) if detail else ''}")


class FakeSource:
    """Catalogue stub: canned album-search and track-search results, call log for assertions."""

    def __init__(self, name, albums=None, tracks=None, raise_on=()):
        self.name, self.albums, self.tracks = name, albums or [], tracks or []
        self.raise_on = set(raise_on)
        self.queries = []

    def search_albums(self, q, limit=10):
        self.queries.append(("album", q))
        if "album" in self.raise_on:
            raise OSError("catalogue down")
        return self.albums

    def search_tracks(self, q, limit=20):
        self.queries.append(("track", q))
        if "track" in self.raise_on:
            raise OSError("catalogue down")
        return self.tracks


def install(monkey_sources, details):
    """Point bulk at fake sources and a dict-keyed album_detail."""
    catalog.SOURCES.update(monkey_sources)

    def fake_detail(ref, source="deezer"):
        d = details[(source, str(ref))]
        if isinstance(d, Exception):
            raise d
        return {k: (list(v) if isinstance(v, list) else v) for k, v in d.items()}
    bulk.album_detail, real = fake_detail, bulk.album_detail
    return real


def fresh_conn(d, seed_album=None, seed_year=None, seed_duration=200.0, title="Hook Song",
               artist="Test Act"):
    conn = db.init(os.path.join(d, "t.db"))
    wid, _ = db.add_want(conn, artist, title, seed_album, seed_year, seed_duration)
    conn.execute("UPDATE wants SET status=?, file_path=? WHERE id=?",
                 (db.STATUS_HAVE, "/music/x.flac", wid))
    conn.commit()
    return conn, wid


def tr(title, dur, artist="Test Act", no=1):
    return {"title": title, "artist": artist, "duration": dur, "track_no": no}


REAL_SOURCES = dict(catalog.SOURCES)
REAL_DETAIL = bulk.album_detail

print("=== Tier A: known album, exact spelling — attribution reused, seed not re-queued ===")
with tempfile.TemporaryDirectory() as d:
    conn, wid = fresh_conn(d, seed_album="Who I Am", seed_year="1994")
    deezer = FakeSource("deezer", albums=[
        {"source": "deezer", "ref": "77", "title": "who i am", "artist": "Test Act",
         "kind": "album", "tracks": 3}])
    install({"deezer": deezer, "itunes": FakeSource("itunes")}, {
        ("deezer", "77"): {"title": "who i am", "year": "1996", "artist": "Test Act",
                           "kind": "album",
                           "tracks": [tr("Hook Song", 201.0), tr("Second Song", 180.0, no=2),
                                      tr("Third Song", 190.0, no=3)]}})
    try:
        r = bulk.complete_album(conn, wid)
        check("outcome completed", r["outcome"] == "completed", str(r))
        check("two tracks queued, seed excluded", r["added"] == 2 and r["total"] == 2, str(r))
        rows = conn.execute("SELECT title, album, year, track_no FROM wants ORDER BY id").fetchall()
        check("no second want for the seed song", len(rows) == 3, str([r_["title"] for r_ in rows]))
        check("new wants carry the SEED's spelling and year",
              all(x["album"] == "Who I Am" and x["year"] == "1994" for x in rows[1:]),
              str([(x["album"], x["year"]) for x in rows]))
        # Positions are the release's own, NOT renumbered after the seed was popped — renumbering
        # is how "track 4" becomes a second "track 5" on disk.
        check("queued wants keep their listing positions (no renumber past the seed)",
              [x["track_no"] for x in rows[1:]] == [2, 3],
              str([(x["title"], x["track_no"]) for x in rows]))
        check("seed want learned its own position", rows[0]["track_no"] == 1, str(dict(rows[0])))
        check("edition_differs not set", not r["edition_differs"])
    finally:
        catalog.SOURCES.update(REAL_SOURCES)
        bulk.album_detail = REAL_DETAIL

print("\n=== Tier A: only a deluxe edition exists — its own title, split reported ===")
with tempfile.TemporaryDirectory() as d:
    conn, wid = fresh_conn(d, seed_album="Who I Am", seed_year="1994")
    deezer = FakeSource("deezer", albums=[
        {"source": "deezer", "ref": "78", "title": "Who I Am (Deluxe Edition)",
         "artist": "Test Act", "kind": "album", "tracks": 2}])
    install({"deezer": deezer, "itunes": FakeSource("itunes")}, {
        ("deezer", "78"): {"title": "Who I Am (Deluxe Edition)", "year": "2004",
                           "artist": "Test Act", "kind": "album",
                           "tracks": [tr("Hook Song", 200.0), tr("Bonus Song", 120.0, no=2)]}})
    try:
        r = bulk.complete_album(conn, wid)
        check("deluxe keeps its own title", r["album"] == "Who I Am (Deluxe Edition)", str(r))
        check("deluxe keeps its own year",
              conn.execute("SELECT year FROM wants WHERE title='Bonus Song'").fetchone()[0]
              == "2004")
        check("edition difference reported", r["edition_differs"] is True)
        check("summary mentions the edition",
              "different release" in worker.job_summary("complete", r))
        # The seed is attributed to "Who I Am"; its position on the DELUXE is a different
        # release's fact and must not be stamped onto it.
        check("seed gains no track number from a different edition",
              conn.execute("SELECT track_no FROM wants WHERE id=?", (wid,)).fetchone()[0] is None)
    finally:
        catalog.SOURCES.update(REAL_SOURCES)
        bulk.album_detail = REAL_DETAIL

print("\n=== Tier A: album known only from the file's tags ===")
with tempfile.TemporaryDirectory() as d:
    conn, wid = fresh_conn(d, seed_album=None)
    conn.execute("INSERT INTO files (path, artist, title, norm_artist, norm_title, album, year, "
                 "duration) VALUES (?,?,?,?,?,?,?,?)",
                 ("/music/x.flac", "Test Act", "Hook Song", db.norm("Test Act"),
                  db.norm("Hook Song"), "Tag Album", "1990", 200.0))
    conn.commit()
    deezer = FakeSource("deezer", albums=[
        {"source": "deezer", "ref": "79", "title": "Tag Album", "artist": "Test Act",
         "kind": "album", "tracks": 2}])
    install({"deezer": deezer, "itunes": FakeSource("itunes")}, {
        ("deezer", "79"): {"title": "Tag Album", "year": "1990", "artist": "Test Act",
                           "kind": "album",
                           "tracks": [tr("Hook Song", 200.0), tr("Deep Cut", 240.0, no=2)]}})
    try:
        r = bulk.complete_album(conn, wid)
        check("file-tag album drove Tier A",
              r["outcome"] == "completed" and deezer.queries[0] == ("album", "Test Act Tag Album"),
              str(deezer.queries))
        check("seed want enriched from the resolution", r["enriched"] == 1)
        check("summary says to run refile",
              "refile" in worker.job_summary("complete", r))
    finally:
        catalog.SOURCES.update(REAL_SOURCES)
        bulk.album_detail = REAL_DETAIL

print("\n=== Tier B: no album anywhere — track search; fuzzy hit refused, album beats single ===")
with tempfile.TemporaryDirectory() as d:
    conn, wid = fresh_conn(d)
    deezer = FakeSource("deezer", tracks=[
        # Wrong song (duration far off) whose album must NOT be completed.
        {"source": "deezer", "artist": "Test Act", "title": "Hook Song", "album": "Wrong Home",
         "album_ref": "600", "year": None, "duration": 340.0},
        # The song as a single, and the song on its real album — the album must win.
        {"source": "deezer", "artist": "Test Act", "title": "Hook Song", "album": "Hook Song",
         "album_ref": "601", "year": None, "duration": 200.0},
        {"source": "deezer", "artist": "Test Act", "title": "Hook Song", "album": "Real Home",
         "album_ref": "602", "year": None, "duration": 200.0},
        # A compilation carrying the song — refused by kind.
        {"source": "deezer", "artist": "Test Act", "title": "Hook Song", "album": "Greatest Hits",
         "album_ref": "603", "year": None, "duration": 200.0}])
    install({"deezer": deezer, "itunes": FakeSource("itunes")}, {
        ("deezer", "601"): {"title": "Hook Song", "year": "2001", "artist": "Test Act",
                            "kind": "single", "tracks": [tr("Hook Song", 200.0)]},
        ("deezer", "602"): {"title": "Real Home", "year": "2002", "artist": "Test Act",
                            "kind": "album",
                            "tracks": [tr("Hook Song", 200.0), tr("Album Cut", 190.0, no=2)]},
        ("deezer", "603"): {"title": "Greatest Hits", "year": "2010", "artist": "Test Act",
                            "kind": "compilation",
                            "tracks": [tr("Hook Song", 200.0), tr("Other Hit", 210.0, no=2)]}})
    try:
        r = bulk.complete_album(conn, wid)
        check("the real album won over single and compilation",
              r["outcome"] == "completed" and r["album"] == "Real Home", str(r))
        # Ref 600 has no detail entry: fetching it would KeyError into r["errors"], so a clean
        # error list proves the wrong-duration hit's album was never even fetched.
        check("wrong-duration hit's album never fetched",
              not any("600" in e for e in r.get("errors", [])), str(r.get("errors")))
        check("only the missing track queued", r["added"] == 1 and r["total"] == 1, str(r))
    finally:
        catalog.SOURCES.update(REAL_SOURCES)
        bulk.album_detail = REAL_DETAIL

print("\n=== Gates: tribute act refused; remastered listing title still shields the seed ===")
with tempfile.TemporaryDirectory() as d:
    conn, wid = fresh_conn(d, seed_album="Night Opera")
    deezer = FakeSource("deezer", albums=[
        {"source": "deezer", "ref": "80", "title": "Night Opera", "artist": "Test Act Tribute Band",
         "kind": "album", "tracks": 9}])
    install({"deezer": deezer, "itunes": FakeSource("itunes")}, {
        ("deezer", "80"): {"title": "Night Opera", "year": "2015",
                           "artist": "Test Act Tribute Band", "kind": "album",
                           "tracks": [tr("Hook Song", 200.0, artist="Test Act Tribute Band")]}})
    try:
        r = bulk.complete_album(conn, wid)
        check("tribute act's album refused at search time", r["outcome"] == "none", str(r))
    finally:
        catalog.SOURCES.update(REAL_SOURCES)
        bulk.album_detail = REAL_DETAIL

with tempfile.TemporaryDirectory() as d:
    conn, wid = fresh_conn(d, seed_album="Night Opera")
    deezer = FakeSource("deezer", albums=[
        {"source": "deezer", "ref": "81", "title": "Night Opera", "artist": "Test Act",
         "kind": "album", "tracks": 2}])
    install({"deezer": deezer, "itunes": FakeSource("itunes")}, {
        ("deezer", "81"): {"title": "Night Opera", "year": "1998", "artist": "Test Act",
                           "kind": "album",
                           "tracks": [tr("Hook Song (Remastered)", 201.0),
                                      tr("Closer", 230.0, no=2)]}})
    try:
        r = bulk.complete_album(conn, wid)
        n = conn.execute("SELECT COUNT(*) FROM wants").fetchone()[0]
        check("'(Remastered)' listing spelling did not re-queue the seed",
              r["added"] == 1 and n == 2, f"{r} wants={n}")
    finally:
        catalog.SOURCES.update(REAL_SOURCES)
        bulk.album_detail = REAL_DETAIL

print("\n=== Twin gate: a respelled listing track claims the held want, not a new download ===")
with tempfile.TemporaryDirectory() as d:
    conn, wid = fresh_conn(d, seed_album="Commodore Test", seed_year="2021")
    # Held sibling under the dash spelling, credited to the duo — the listing spells it with
    # parentheses and credits the lead alone, which is exactly the pair that re-downloaded a
    # whole held album.
    sid, _ = db.add_want(conn, "Test Act & Guest", "Second Song - Chiptune", "Commodore Test",
                         "2021", 150.0)
    conn.execute("UPDATE wants SET status=?, provider='tidal', file_path='/music/y.flac' "
                 "WHERE id=?", (db.STATUS_HAVE, sid))
    conn.commit()
    deezer = FakeSource("deezer", albums=[
        {"source": "deezer", "ref": "90", "title": "Commodore Test", "artist": "Test Act",
         "kind": "album", "tracks": 3}])
    install({"deezer": deezer, "itunes": FakeSource("itunes")}, {
        ("deezer", "90"): {"title": "Commodore Test", "year": "2021", "artist": "Test Act",
                           "kind": "album",
                           "tracks": [tr("Hook Song", 200.0),
                                      tr("Second Song (Chiptune)", 151.0, no=2),
                                      tr("Different Length (Chiptune)", 60.0, no=3)]}})
    try:
        r = bulk.complete_album(conn, wid)
        check("respelled track claimed the held want — no new want row",
              db.find_want(conn, "Test Act", "Second Song (Chiptune)") is None
              and conn.execute("SELECT COUNT(*) FROM wants").fetchone()[0] == 3,
              str(r))
        check("held want enriched with the listing position",
              conn.execute("SELECT track_no FROM wants WHERE id=?", (sid,)).fetchone()[0] == 2)
        check("twin counted as already held", r["already"] == 1, str(r))
        check("a duration-disagreeing title still queues",
              r["added"] == 1
              and db.find_want(conn, "Test Act", "Different Length (Chiptune)") is not None,
              str(r))
    finally:
        catalog.SOURCES.update(REAL_SOURCES)
        bulk.album_detail = REAL_DETAIL

print("\n=== Outcomes: single, already complete, outage vs genuine absence ===")
with tempfile.TemporaryDirectory() as d:
    conn, wid = fresh_conn(d)
    deezer = FakeSource("deezer", tracks=[
        {"source": "deezer", "artist": "Test Act", "title": "Hook Song", "album": "Hook Song",
         "album_ref": "610", "year": None, "duration": 200.0}])
    install({"deezer": deezer, "itunes": FakeSource("itunes")}, {
        ("deezer", "610"): {"title": "Hook Song", "year": "2001", "artist": "Test Act",
                            "kind": "single", "tracks": [tr("Hook Song", 200.0)]}})
    try:
        r = bulk.complete_album(conn, wid)
        check("one-track release reports single", r["outcome"] == "single", str(r))
        check("single summary renders", "single" in worker.job_summary("complete", r))
    finally:
        catalog.SOURCES.update(REAL_SOURCES)
        bulk.album_detail = REAL_DETAIL

with tempfile.TemporaryDirectory() as d:
    conn, wid = fresh_conn(d, seed_album="Full House")
    db.add_want(conn, "Test Act", "Second Song", "Full House", "2001", 180.0)
    deezer = FakeSource("deezer", albums=[
        {"source": "deezer", "ref": "82", "title": "Full House", "artist": "Test Act",
         "kind": "album", "tracks": 2}])
    install({"deezer": deezer, "itunes": FakeSource("itunes")}, {
        ("deezer", "82"): {"title": "Full House", "year": "2001", "artist": "Test Act",
                           "kind": "album",
                           "tracks": [tr("Hook Song", 200.0), tr("Second Song", 180.0, no=2)]}})
    try:
        r = bulk.complete_album(conn, wid)
        check("nothing new to queue reports already",
              r["outcome"] == "already" and r["added"] == 0, str(r))
        check("already summary renders", "already" in worker.job_summary("complete", r))
        check("an already-wanted track was enriched with its position",
              conn.execute("SELECT track_no FROM wants WHERE title='Second Song'")
              .fetchone()[0] == 2)
    finally:
        catalog.SOURCES.update(REAL_SOURCES)
        bulk.album_detail = REAL_DETAIL

with tempfile.TemporaryDirectory() as d:
    conn, wid = fresh_conn(d)
    install({"deezer": FakeSource("deezer", raise_on=("album", "track")),
             "itunes": FakeSource("itunes", raise_on=("album", "track"))}, {})
    try:
        try:
            bulk.complete_album(conn, wid)
            check("total outage raises", False)
        except RuntimeError as e:
            check("total outage raises", "no catalogue could be reached" in str(e))
    finally:
        catalog.SOURCES.update(REAL_SOURCES)
        bulk.album_detail = REAL_DETAIL

with tempfile.TemporaryDirectory() as d:
    conn, wid = fresh_conn(d)
    install({"deezer": FakeSource("deezer", raise_on=("album", "track")),
             "itunes": FakeSource("itunes", tracks=[])}, {})
    try:
        r = bulk.complete_album(conn, wid)
        check("one source down, other empty → none (not an error)",
              r["outcome"] == "none", str(r))
        check("none summary renders", "no album" in worker.job_summary("complete", r))
    finally:
        catalog.SOURCES.update(REAL_SOURCES)
        bulk.album_detail = REAL_DETAIL

with tempfile.TemporaryDirectory() as d:
    conn = db.init(os.path.join(d, "t.db"))
    r = bulk.complete_album(conn, 424242)
    check("missing want reports missing", r["outcome"] == "missing", str(r))
    check("missing summary renders", "no longer exists" in worker.job_summary("complete", r))

print("\n=== Crash mid-add leaves neither wants nor enrichment ===")
with tempfile.TemporaryDirectory() as d:
    conn, wid = fresh_conn(d, seed_album=None)
    deezer = FakeSource("deezer", tracks=[
        {"source": "deezer", "artist": "Test Act", "title": "Hook Song", "album": "Boom",
         "album_ref": "611", "year": None, "duration": 200.0}])
    install({"deezer": deezer, "itunes": FakeSource("itunes")}, {
        ("deezer", "611"): {"title": "Boom", "year": "2001", "artist": "Test Act",
                            "kind": "album",
                            "tracks": [tr("Hook Song", 200.0), tr("Bang", 190.0, no=2)]}})
    real_add = db.add_want

    def exploding_add(*a, **kw):
        raise sqlite_boom
    sqlite_boom = RuntimeError("disk full")
    db.add_want = exploding_add
    try:
        try:
            bulk.complete_album(conn, wid)
            check("add failure propagates", False)
        except RuntimeError:
            check("add failure propagates", True)
        row = conn.execute("SELECT album FROM wants WHERE id=?", (wid,)).fetchone()
        n = conn.execute("SELECT COUNT(*) FROM wants").fetchone()[0]
        check("rollback left no partial state", row["album"] is None and n == 1,
              f"album={row['album']} wants={n}")
    finally:
        db.add_want = real_add
        catalog.SOURCES.update(REAL_SOURCES)
        bulk.album_detail = REAL_DETAIL

print("\n=== Worker dispatch: complete routed, unknown kind refused, nudge after adds ===")
with tempfile.TemporaryDirectory() as d:
    conn = db.init(os.path.join(d, "t.db"))
    calls = []
    real_complete, real_artist = bulk.complete_album, bulk.add_artist
    bulk.complete_album = lambda c, ref, by=None: (calls.append(("complete", ref)) or
                                                   {"outcome": "completed", "album": "A",
                                                    "resolved": "A", "year": "2001",
                                                    "source": "deezer", "added": 3, "already": 0,
                                                    "existing": 0, "total": 3, "batch": "b1",
                                                    "edition_differs": False, "enriched": 0})
    bulk.add_artist = lambda *a, **kw: calls.append(("artist", a)) or {}
    real_nudge = worker.NUDGE
    worker.NUDGE = os.path.join(d, "nudge")
    try:
        db.add_job(conn, "complete", "17", "catalogues", "test")
        db.add_job(conn, "bogus", "99", "deezer", "test")
        worker.run_jobs(conn)
        jobs = {j["kind"]: j for j in
                conn.execute("SELECT kind, status, detail FROM jobs").fetchall()}
        check("complete dispatched with the want id as int",
              calls and calls[0] == ("complete", 17), str(calls))
        check("complete job finished with a resolution summary",
              jobs["complete"]["status"] == db.JOB_DONE
              and "resolved to" in jobs["complete"]["detail"], str(dict(jobs["complete"])))
        check("unknown kind errored, never ran add_artist",
              jobs["bogus"]["status"] == db.JOB_ERROR
              and all(k != "artist" for k, _ in calls), str(calls))
        check("worker re-nudged acquisition after queueing", os.path.exists(worker.NUDGE))
    finally:
        bulk.complete_album, bulk.add_artist = real_complete, real_artist
        worker.NUDGE = real_nudge

print(f"\n{bad} failure(s)")
sys.exit(1 if bad else 0)
