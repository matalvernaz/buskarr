"""Moving a file must re-point every want that named it, not just one.

Two wants legitimately share a path. `find_recording` satisfies an edition-noise variant
("Chattahoochee [extended mix 2] [*]") from the file a sibling want just placed, and that row
carries no provider — so `refile.plan` never selects it, and it can only ever be collateral of a
move made on somebody else's behalf. Re-pointing by want id left it naming a path that no longer
existed, which `refile` then skips silently on every later run.

`fold.py` learned this and carries the fix with its reasoning attached; `refile` and `dupes` did
not. Measured on the live library 2026-09-10: 3 files of 1619 were claimed by two wants each, and
one of them was a pending `refile` move away from stranding its sibling.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("LIBRARY_DIR", "/tmp/buskarr-test-lib")

bad = 0


def check(label, ok, detail=""):
    global bad
    if not ok:
        bad += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{('  — ' + detail) if detail else ''}")


def placed(conn, wid):
    return conn.execute("SELECT file_path FROM wants WHERE id=?", (wid,)).fetchone()["file_path"]


print("=== refile re-points every claimant of the file it moves ===")
with tempfile.TemporaryDirectory() as d:
    lib = os.path.join(d, "music")
    os.environ["LIBRARY_DIR"] = lib
    for mod in [m for m in list(sys.modules) if m.startswith("buskarr")]:
        del sys.modules[mod]
    from buskarr import db, refile, scan, worker  # noqa: E402

    conn = db.init(os.path.join(d, "t.db"))
    # The mover: a real download whose want has since gained an album, so refile has work to do.
    old = os.path.join(lib, "Alan Jackson", "Singles", "Chattahoochee.flac")
    os.makedirs(os.path.dirname(old), exist_ok=True)
    with open(old, "w") as fh:
        fh.write("AUDIO")
    mover, _ = db.add_want(conn, "Alan Jackson", "Chattahoochee", "Who I Am", "1994",
                           200.0, "tester", track_no=1)
    conn.execute("UPDATE wants SET status=?, provider=?, file_path=? WHERE id=?",
                 (db.STATUS_HAVE, "tidal", old, mover))
    # The passenger: the same recording under edition noise, satisfied from the mover's file.
    # No provider, exactly as add_want records a want resolved against a file already on disk.
    rider, _ = db.add_want(conn, "Alan Jackson", "Chattahoochee [extended mix 2] [*]",
                           "Original Album Classics", "2008", 200.0, "tester", allow_dup=True)
    conn.execute("UPDATE wants SET status=?, provider=NULL, file_path=? WHERE id=?",
                 (db.STATUS_HAVE, old, rider))
    conn.commit()

    scan.probe = lambda path: {}
    worker.tag = lambda path, want, track=None: True

    plan = refile.plan(conn)
    check("only the want with a provider is planned", [w["id"] for w, _, _ in plan] == [mover],
          str([w["id"] for w, _, _ in plan]))

    refile.refile(conn, dry_run=False, log=lambda *_: None)
    new = placed(conn, mover)
    check("the mover follows its file", new and new != old and os.path.exists(new), str(new))
    check("and so does the passenger", placed(conn, rider) == new,
          f"{placed(conn, rider)!r} != {new!r}")
    check("no want names a path that is gone",
          all(os.path.exists(r["file_path"])
              for r in conn.execute("SELECT file_path FROM wants WHERE file_path IS NOT NULL")))

    # The whole point of the stale row: refile skips a want whose file has vanished, so a stranded
    # passenger is never repaired by a later run either.
    refile.refile(conn, dry_run=True, log=lambda *_: None)
    check("a second run has nothing to do", not refile.plan(conn))
    conn.close()

print("\n=== quarantining a duplicate re-points every claimant too ===")
with tempfile.TemporaryDirectory() as d:
    lib = os.path.join(d, "music")
    os.environ["LIBRARY_DIR"] = lib
    for mod in [m for m in list(sys.modules) if m.startswith("buskarr")]:
        del sys.modules[mod]
    from buskarr import db, dupes, worker  # noqa: E402

    conn = db.init(os.path.join(d, "t.db"))
    keep = os.path.join(lib, "A", "First Album (2001)", "01 - Song.flac")
    drop = os.path.join(lib, "A", "Greatest Hits (2010)", "01 - Song.flac")
    for path in (keep, drop):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("AUDIO")
    one, _ = db.add_want(conn, "A", "Song", "Greatest Hits", "2010", 200.0, "tester")
    two, _ = db.add_want(conn, "A", "Song [remastered]", "Greatest Hits", "2010", 200.0,
                         "tester", allow_dup=True)
    for wid in (one, two):
        conn.execute("UPDATE wants SET status=?, file_path=? WHERE id=?",
                     (db.STATUS_HAVE, drop, wid))
    conn.commit()

    # dupes narrows on the `files` index, then decides on decoded audio. Both files here are
    # one recording, which is the only thing it acts on.
    for path in (keep, drop):
        conn.execute(
            "INSERT INTO files (path, artist, album, title, norm_artist, norm_title, "
            "duration, artist_lead) VALUES (?,?,?,?,?,?,?,?)",
            (path, "A", os.path.basename(os.path.dirname(path)), "Song",
             db.norm("A"), db.norm("Song"), 200.0, db.folder_key("A")))
    conn.commit()
    dupes.audio_hash = lambda path: "identical"
    dupes.LIBRARY = lib
    dupes.QUARANTINE = os.path.join(lib, ".quarantine")
    result = dupes.sweep(conn, dry_run=False, log=lambda *_: None)
    check("one file quarantined", result["quarantined"] == 1, str(result))
    check("both wants re-pointed, neither stranded",
          {placed(conn, one), placed(conn, two)} == {keep},
          str({placed(conn, one), placed(conn, two)}))
    check("and neither names the quarantine",
          not any(".quarantine" in (placed(conn, w) or "") for w in (one, two)))
    conn.close()

print(f"\n{bad} failure(s)")
raise SystemExit(1 if bad else 0)
