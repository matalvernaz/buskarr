"""The worker's automatic indexer steps: when a grab is retired, and when harvest runs at all.

Both directions of the retire decision are expensive to get wrong, and neither is visible until
days later:

  * Retiring too early abandons a download that has not finished. Its wants stay UNAVAILABLE and
    the release is deleted with its files when the 7-day seed limit expires, so the grab is spent
    for nothing.
  * Never retiring means every grab ever made re-walks the whole download tree on every cycle,
    forever.

``harvest`` itself is faked here. What is under test is the bookkeeping around it, and a real
harvest would need a download tree on disk to say anything about that.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from buskarr import db, worker  # noqa: E402
from buskarr import harvest as harvest_mod  # noqa: E402

bad = 0


def check(label, ok, detail=""):
    global bad
    if not ok:
        bad += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{('  — ' + detail) if detail else ''}")


def add_grab(conn, lead, album, age_days=0.0):
    conn.execute(
        "INSERT INTO grabs (album_key, artist, artist_lead, album, release_title, protocol,"
        " grabbed_at) VALUES (?,?,?,?,?,?,?)",
        (f"{lead}|{album}", lead, lead, album, f"{lead} - {album} FLAC", "torrent",
         time.time() - age_days * 86400))
    conn.commit()
    return conn.execute("SELECT id FROM grabs ORDER BY id DESC LIMIT 1").fetchone()["id"]


def want(conn, lead, album, title, status):
    wid, _ = db.add_want(conn, lead, title, album, "1999", 100.0, artist_lead=lead)
    conn.execute("UPDATE wants SET status=? WHERE id=?", (status, wid))
    conn.commit()


def harvested(conn, gid):
    return conn.execute("SELECT harvested_at FROM grabs WHERE id=?", (gid,)).fetchone()[0]


calls = []


def fake_harvest(conn, dry_run=True, limit=0, log=None):
    calls.append(dry_run)


with tempfile.TemporaryDirectory() as d:
    conn = db.init(os.path.join(d, "t.db"))
    harvest_mod.harvest = fake_harvest

    print("=== nothing outstanding: harvest must not run at all ===")
    calls.clear()
    check("no grabs means no work", worker.run_harvest(conn) == 0)
    check("harvest was never called", calls == [], str(calls))

    print("\n=== a grab whose album is still short is kept for the next cycle ===")
    calls.clear()
    short = add_grab(conn, "AJ", "Alb1")
    want(conn, "AJ", "Alb1", "Track 1", db.STATUS_UNAVAILABLE)
    want(conn, "AJ", "Alb1", "Track 2", db.STATUS_HAVE)
    check("harvest ran", worker.run_harvest(conn) == 0 and calls == [False], str(calls))
    check("not retired while a want is still unavailable", harvested(conn, short) is None)

    print("\n=== a grab whose album is complete is retired ===")
    calls.clear()
    done = add_grab(conn, "BJ", "Alb2")
    want(conn, "BJ", "Alb2", "Track 1", db.STATUS_HAVE)
    retired = worker.run_harvest(conn)
    check("one grab retired", retired == 1, str(retired))
    check("the completed grab is stamped", harvested(conn, done) is not None)
    check("the short one is still waiting", harvested(conn, short) is None)

    print("\n=== a want that merely LEFT unavailable is not proof of delivery ===")
    # Retry re-pends an unavailable want, and the stale-queue reaper moves SEARCHING back to
    # pending. Reading "no unavailable wants" as success stamps the grab, so nothing harvests the
    # download when it finishes and the 7-day cleanup deletes it. Only HAVE means delivered.
    calls.clear()
    repended = add_grab(conn, "DJ", "Alb4")
    want(conn, "DJ", "Alb4", "Track 1", db.STATUS_PENDING)
    want(conn, "DJ", "Alb4", "Track 2", db.STATUS_SEARCHING)
    worker.run_harvest(conn)
    check("a re-pended want does not retire the grab", harvested(conn, repended) is None)

    print("\n=== a grab from before the artist_lead column is not retired by the count ===")
    # Pre-migration rows carry NULL, while their wants carry a real lead, so `artist_lead IS NULL`
    # matches nothing and the grab would read as fully delivered on the first cycle after upgrade.
    calls.clear()
    legacy = add_grab(conn, "EJ", "Alb5")
    conn.execute("UPDATE grabs SET artist_lead=NULL WHERE id=?", (legacy,))
    conn.commit()
    want(conn, "EJ", "Alb5", "Track 1", db.STATUS_UNAVAILABLE)
    worker.run_harvest(conn)
    check("a NULL-lead grab is left to the window, not retired", harvested(conn, legacy) is None)

    print("\n=== a failure inside harvest must not leave a write lock open ===")
    calls.clear()

    def exploding_harvest(conn_, dry_run=True, limit=0, log=None):
        conn_.execute("UPDATE grabs SET release_title='half-written' WHERE id=?", (legacy,))
        raise RuntimeError("harvest blew up mid-write")

    harvest_mod.harvest = exploding_harvest
    try:
        worker.run_harvest(conn)
        check("the exception is contained", True)
        check("no transaction is left open across the sleep", conn.in_transaction is False,
              f"in_transaction={conn.in_transaction}")
        check("the half-written change was rolled back",
              conn.execute("SELECT release_title FROM grabs WHERE id=?",
                           (legacy,)).fetchone()[0] != "half-written")
    finally:
        harvest_mod.harvest = fake_harvest

    print("\n=== a grab past the window is given up on, without harvesting for it ===")
    calls.clear()
    stale = add_grab(conn, "CJ", "Alb3", age_days=worker.HARVEST_WINDOW / 86400 + 1)
    want(conn, "CJ", "Alb3", "Track 1", db.STATUS_UNAVAILABLE)
    worker.run_harvest(conn)
    check("expired grab is stamped so it stops being retried", harvested(conn, stale) is not None)

    print("\n=== the switch actually switches it off ===")
    calls.clear()
    old = worker.HARVEST_AUTO
    worker.HARVEST_AUTO = False
    try:
        check("HARVEST_AUTO=0 does nothing", worker.run_harvest(conn) == 0 and calls == [])
    finally:
        worker.HARVEST_AUTO = old

    print("\n=== automatic grabbing is torrent-only and capped ===")
    check("worker never grabs usenet unattended", "usenet" not in worker.GRAB_PROTOCOLS,
          str(worker.GRAB_PROTOCOLS))
    check("a per-cycle cap exists", worker.GRAB_PER_CYCLE > 0, str(worker.GRAB_PER_CYCLE))
    seen = []
    worker.grab.available = lambda: True
    worker.grab.sweep = lambda conn, **kw: seen.append(kw) or {"grabbed": 0, "already": 0}
    worker.run_grabs(conn)
    check("sweep is called with the cap and torrent-only",
          seen and seen[0]["limit"] == worker.GRAB_PER_CYCLE
          and seen[0]["protocols"] == worker.GRAB_PROTOCOLS
          and seen[0]["dry_run"] is False, str(seen))

print(f"\n{'FAILED' if bad else 'all checks passed'} ({bad} failure(s))")
sys.exit(1 if bad else 0)
