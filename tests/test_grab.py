"""Regression cases for the Prowlarr grab path.

The grab is loose on purpose — a release title can only be gated on artist and album evidence,
never duration — because nothing it fetches enters the library except through harvest's full
per-track vetting. What must hold here: only fitting releases are chosen (torrent, seeded,
album-sized, lossless preferred), a grab is recorded and never silently repeated, and dry-run
sends nothing.
"""
import os
import sys
import tempfile
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from buskarr import db, grab  # noqa: E402

bad = 0


def check(label, ok, detail=""):
    global bad
    if not ok:
        bad += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{('  — ' + detail) if detail else ''}")


def rel(title, protocol="torrent", seeders=5, size=500 * 1024 * 1024, guid="g1", indexer=7):
    return {"title": title, "protocol": protocol, "seeders": seeders, "size": size,
            "guid": guid, "indexerId": indexer, "indexer": "FakeIdx"}


print("=== pick_release gates and preferences ===")
# A real NZB carries no seeders at all. Faking one with seeders=0 would be a torrent that happens
# to be dead; the awkward truth being modelled is that the FIELD IS ABSENT, which is exactly what
# the old torrent-shaped gate rejected on every usenet release.
nzb = rel("AJ - Alb1 FLAC", protocol="usenet", guid="usenet")
del nzb["seeders"]
releases = [
    rel("AJ - Alb1 [MP3 320]", seeders=50, guid="mp3"),
    rel("AJ - Alb1 FLAC", seeders=3, guid="flac"),
    nzb,
    rel("AJ - Alb1 FLAC", seeders=0, guid="dead"),
    rel("AJ - Alb1 FLAC", size=10 * 1024 * 1024, guid="tiny"),
    rel("AJ - Alb1 FLAC", size=9 * 1024 * 1024 * 1024, guid="huge"),
    rel("Somebody Else - Alb1 FLAC", seeders=80, guid="wrongartist"),
    rel("AJ - Different Record FLAC", seeders=80, guid="wrongalbum"),
]
best = grab.pick_release(releases, "AJ", "Alb1")
check("a seederless NZB is eligible at all", best is not None)
check("usenet wins the tie — no ratio spent, no obligation left behind",
      best and best["guid"] == "usenet", str(best and best["guid"]))

torrent_only = grab.pick_release(releases, "AJ", "Alb1", protocols=("torrent",))
check("lossless beats a better-seeded MP3",
      torrent_only and torrent_only["guid"] == "flac", str(torrent_only and torrent_only["guid"]))
# The worker's automatic sweep passes exactly this, so usenet stays a deliberate act.
check("protocols=('torrent',) excludes usenet entirely",
      torrent_only and torrent_only["guid"] != "usenet")
usenet_only = grab.pick_release(releases, "AJ", "Alb1", protocols=("usenet",))
check("protocols=('usenet',) excludes torrents entirely",
      usenet_only and usenet_only["guid"] == "usenet", str(usenet_only and usenet_only["guid"]))

# The seeder gate must survive for torrents; dropping it wholesale would grab dead releases.
check("an unseeded TORRENT is still refused",
      grab.pick_release([r for r in releases if r["guid"] == "dead"], "AJ", "Alb1") is None)
# A concert DVD in the audio category passes artist, album and size, and contains no audio file at
# all. One was grabbed live before this gate existed.
print("  -- video releases in the audio category --")
for title, is_vid in (("CMT Invitation Only - Alan Jackson HD XviD", True),
                      ("Alan Jackson-Chattahoochee-Opry 100 Years-720p-x264-2025", True),
                      ("Some Band - Live At Wembley 1080p BluRay", True),
                      ("Alan Jackson-Live At Texas Stadium-02-CD-FLAC-2007-6DM", False),
                      ("Avicii - True [FLAC]", False),
                      ("Kaylee Rose-Kaylee Rose-EP-WEB-2012-KNOWN", False)):
    check(f"{'video' if is_vid else 'audio'}: {title[:52]}", grab.is_video(title) is is_vid)
check("a video release is refused even with perfect artist and album evidence",
      grab.pick_release([rel("AJ - Alb1 HD XviD", guid="vid")], "AJ", "Alb1") is None)

check("tiny, huge, wrong-artist and wrong-album refused on both protocols",
      grab.pick_release([r for r in releases
                         if r["guid"] in ("tiny", "huge", "wrongartist", "wrongalbum")],
                        "AJ", "Alb1") is None)

print("\n=== adopt_torrents flips only unmanaged torrents ===")
import json as _json  # noqa: E402

calls = []


def fake_qbit(path, data=None):
    if path.startswith("torrents/info"):
        return _json.dumps([{"hash": "aa", "auto_tmm": False},
                            {"hash": "bb", "auto_tmm": True},
                            {"hash": "cc", "auto_tmm": False}])
    calls.append((path, data))
    return ""


n = grab.adopt_torrents(qbit=fake_qbit, log=lambda m: None)
check("two strays adopted", n == 2, str(n))
check("one setAutoManagement call with both hashes",
      calls == [("torrents/setAutoManagement", {"hashes": "aa|cc", "enable": "true"})],
      str(calls))
check("unreachable qbit degrades to zero, not an exception",
      grab.adopt_torrents(qbit=lambda *a, **k: (_ for _ in ()).throw(OSError()),
                          log=lambda m: None) == 0)

print("\n=== sweep: dry-run finds, apply grabs once, regrab overrides ===")
with tempfile.TemporaryDirectory() as d:
    conn = db.init(os.path.join(d, "t.db"))
    for i in range(3):
        wid, _ = db.add_want(conn, "AJ", f"Track {i}", "Alb1", "1999", 100.0 + i)
        conn.execute("UPDATE wants SET status=? WHERE id=?", (db.STATUS_UNAVAILABLE, wid))
    wid, _ = db.add_want(conn, "AJ", "Loose Song", None, None, 90.0)
    conn.execute("UPDATE wants SET status=? WHERE id=?", (db.STATUS_UNAVAILABLE, wid))
    conn.commit()

    posts = []

    def fake_api(path, body=None):
        if body is not None:
            posts.append(body)
            return None
        return [rel("AJ - Alb1 FLAC", guid="flac")]

    old_key = grab.PROWLARR_API_KEY
    grab.PROWLARR_API_KEY = "test"
    try:
        r = grab.sweep(conn, dry_run=True, api=fake_api, log=lambda m: None)
        check("dry-run identifies the release", r["grabbed"] == 1, str(r))
        check("dry-run sends nothing", not posts)
        check("dry-run records nothing",
              conn.execute("SELECT COUNT(*) FROM grabs").fetchone()[0] == 0)

        r = grab.sweep(conn, dry_run=False, api=fake_api, log=lambda m: None)
        check("apply grabs the release", r["grabbed"] == 1 and len(posts) == 1,
              f"{r} posts={posts}")
        check("grab recorded", conn.execute(
            "SELECT release_title FROM grabs").fetchone()[0] == "AJ - Alb1 FLAC")
        note = conn.execute("SELECT note FROM wants WHERE album='Alb1' LIMIT 1").fetchone()[0]
        check("wants note explains the wait", "grabbed via prowlarr" in (note or ""), str(note))

        r = grab.sweep(conn, dry_run=False, api=fake_api, log=lambda m: None)
        check("second apply skips the grabbed album", r["grabbed"] == 0 and len(posts) == 1,
              str(r))
        r = grab.sweep(conn, dry_run=False, regrab=True, api=fake_api, log=lambda m: None)
        check("--regrab grabs it again", r["grabbed"] == 1 and len(posts) == 2, str(r))
    finally:
        grab.PROWLARR_API_KEY = old_key

print("\n=== an album nothing fits is remembered, so it stops eating the cap ===")
with tempfile.TemporaryDirectory() as d:
    conn = db.init(os.path.join(d, "t.db"))
    wid, _ = db.add_want(conn, "AJ", "Track 1", "NoSuchAlbum", "1999", 100.0)
    conn.execute("UPDATE wants SET status=? WHERE id=?", (db.STATUS_UNAVAILABLE, wid))
    conn.commit()
    searches = []

    def barren_api(path, body=None):
        searches.append(path)
        return []                       # the indexers simply do not carry it

    old_key = grab.PROWLARR_API_KEY
    grab.PROWLARR_API_KEY = "test"
    try:
        grab.sweep(conn, dry_run=False, api=barren_api, log=lambda m: None)
        check("the miss is recorded",
              conn.execute("SELECT COUNT(*) FROM grab_attempts").fetchone()[0] == 1)
        n = len(searches)
        r = grab.sweep(conn, dry_run=False, api=barren_api, log=lambda m: None)
        # Without this the same unfindable album is the permanent head of a most-missing-first
        # queue: re-searched every cycle, and everything below it never examined at all.
        check("the next sweep does not search it again", len(searches) == n, str(searches))
        check("it is reported as cooling, not as nothing-to-do", r["albums"] == 0, str(r))
    finally:
        grab.PROWLARR_API_KEY = old_key

print("\n=== an ambiguous dispatch failure must not free the album for a second grab ===")
for label, exc, expect_row in (
        ("timeout keeps the record", TimeoutError("timed out"), 1),
        ("refused connection withdraws it",
         urllib.error.URLError(ConnectionRefusedError("refused")), 0)):
    with tempfile.TemporaryDirectory() as d:
        conn = db.init(os.path.join(d, "t.db"))
        wid, _ = db.add_want(conn, "AJ", "Track 1", "Alb1", "1999", 100.0)
        conn.execute("UPDATE wants SET status=? WHERE id=?", (db.STATUS_UNAVAILABLE, wid))
        conn.commit()

        def failing_api(path, body=None, _exc=exc):
            if body is not None:
                raise _exc
            return [rel("AJ - Alb1 FLAC", guid="flac")]

        old_key = grab.PROWLARR_API_KEY
        grab.PROWLARR_API_KEY = "test"
        try:
            grab.sweep(conn, dry_run=False, api=failing_api, log=lambda m: None)
            got = conn.execute("SELECT COUNT(*) FROM grabs").fetchone()[0]
            check(label, got == expect_row, f"rows={got}, wanted {expect_row}")
        finally:
            grab.PROWLARR_API_KEY = old_key

print(f"\n{bad} failure(s)")
sys.exit(1 if bad else 0)
