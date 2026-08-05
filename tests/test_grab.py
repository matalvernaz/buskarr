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
releases = [
    rel("AJ - Alb1 [MP3 320]", seeders=50, guid="mp3"),
    rel("AJ - Alb1 FLAC", seeders=3, guid="flac"),
    rel("AJ - Alb1 FLAC", protocol="usenet", seeders=999, guid="usenet"),
    rel("AJ - Alb1 FLAC", seeders=0, guid="dead"),
    rel("AJ - Alb1 FLAC", size=10 * 1024 * 1024, guid="tiny"),
    rel("AJ - Alb1 FLAC", size=9 * 1024 * 1024 * 1024, guid="huge"),
    rel("Somebody Else - Alb1 FLAC", seeders=80, guid="wrongartist"),
    rel("AJ - Different Record FLAC", seeders=80, guid="wrongalbum"),
]
best = grab.pick_release(releases, "AJ", "Alb1")
check("lossless beats a better-seeded MP3", best and best["guid"] == "flac",
      str(best and best["guid"]))
check("usenet, unseeded, tiny, huge, wrong-artist, wrong-album all refused",
      grab.pick_release([r for r in releases if r["guid"] != "flac" and r["guid"] != "mp3"],
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

print(f"\n{bad} failure(s)")
sys.exit(1 if bad else 0)
