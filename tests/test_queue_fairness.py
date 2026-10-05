"""Whose wants a cycle works first, and how soon the next cycle comes.

By age alone, one person's imported list of two thousand songs filled every
cycle for days and anybody else's ask waited behind all of it. People now take
turns: a cycle takes everyone's first due want, then everyone's second. Within
one person, anything asked for one at a time goes ahead of anything that came
in bulk, so an import does not delay its owner's next search either.

The wait between cycles shortens while new wants are still due, and only then.
A throttled cycle, a failed one, or a queue holding nothing but retries waits
the full interval.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from buskarr import db, worker  # noqa: E402

bad = 0


def check(label, ok, detail=""):
    global bad
    if not ok:
        bad += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{('  — ' + detail) if detail else ''}")


def want(conn, who, title, age_minutes, bulk=False, status=db.STATUS_PENDING,
         retry_after=None):
    wid, created = db.add_want(conn, f"Act {title}", title, "An Album", "1999", 100.0,
                               requested_by=who, artist_lead=f"Act {title}", bulk=bulk)
    assert created, title
    conn.execute("UPDATE wants SET requested_at=?, status=?, retry_after=? WHERE id=?",
                 (time.time() - age_minutes * 60, status, retry_after, wid))
    conn.commit()
    return wid


def order(conn, limit=50):
    return [r["title"] for r in worker.due_wants(conn, time.time(), limit)]


def fresh(path):
    conn = db.init(path)
    conn.execute("DELETE FROM wants")
    conn.commit()
    return conn


with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, "t.db")

    print("=== a big import does not hold up somebody else's ask ===")
    conn = fresh(path)
    for i in range(200):
        want(conn, "importer", f"Imported {i:03}", 600 - i, bulk=True)
    want(conn, "listener", "Asked Later", 1)
    first = order(conn, 2)
    check("the later ask is in the first two of the cycle", "Asked Later" in first, str(first))

    print("\n=== people take turns ===")
    conn = fresh(path)
    for i in range(3):
        want(conn, "a", f"A{i}", 100 - i)
        want(conn, "b", f"B{i}", 50 - i)
    got = order(conn)
    check("turn by turn, oldest first within a turn",
          got == ["A0", "B0", "A1", "B1", "A2", "B2"], str(got))

    print("\n=== one at a time goes ahead of bulk for the same person ===")
    conn = fresh(path)
    for i in range(5):
        want(conn, "importer", f"Bulk {i}", 300 - i, bulk=True)
    want(conn, "importer", "Searched For", 1)
    got = order(conn)
    check("the person's own search comes first", got[0] == "Searched For", str(got))

    print("\n=== and ahead of other people's bulk too ===")
    conn = fresh(path)
    want(conn, "a", "A Bulk", 100, bulk=True)
    want(conn, "b", "B Single", 1)
    got = order(conn)
    check("a single ask leads its turn", got == ["B Single", "A Bulk"], str(got))

    print("\n=== a person's old misses do not push their new ask down the line ===")
    conn = fresh(path)
    for i in range(10):
        want(conn, "a", f"A Miss {i}", 10000 - i, status=db.STATUS_UNAVAILABLE,
             retry_after=time.time() - 60)
    want(conn, "a", "A New", 1)
    for i in range(5):
        want(conn, "b", f"B {i}", 50 - i)
    got = order(conn)
    check("a's new ask is in the first turn", got.index("A New") < 2, str(got))
    check("retries still come after every fresh want",
          all(t.startswith("A Miss") for t in got[6:]) and len(got) == 16, str(got))

    print("\n=== the cap is still a cap ===")
    check("the limit is applied", len(order(conn, 3)) == 3)

    print("\n=== asking again one at a time clears the bulk mark ===")
    conn = fresh(path)
    wid = want(conn, "importer", "Twice", 100, bulk=True)
    again, created = db.add_want(conn, "Act Twice", "Twice", requested_by="importer")
    row = conn.execute("SELECT bulk FROM wants WHERE id=?", (wid,)).fetchone()
    check("the same want comes back", again == wid and not created)
    check("and it is no longer bulk", row["bulk"] == 0, str(dict(row)))
    db.add_want(conn, "Act Twice", "Twice", requested_by="importer", bulk=True)
    row = conn.execute("SELECT bulk FROM wants WHERE id=?", (wid,)).fetchone()
    check("a later bulk ask does not put it back", row["bulk"] == 0, str(dict(row)))

    print("\n=== jobs carry the mark to the wants they create ===")
    job = db.add_job(conn, "album", "123", "deezer", "the album X", "importer", bulk=True)
    row = conn.execute("SELECT bulk FROM jobs WHERE id=?", (job,)).fetchone()
    check("a bulk job is stored as bulk", row["bulk"] == 1)

    print("\n=== how long to wait after a cycle ===")
    conn = fresh(path)
    check("an empty queue waits the full interval",
          worker.next_wait(conn, False) == worker.INTERVAL)
    want(conn, "a", "Waiting", 1)
    check("new work still due shortens the wait",
          worker.next_wait(conn, False) == min(worker.BUSY_INTERVAL, worker.INTERVAL))
    check("unless the cycle was throttled or failed",
          worker.next_wait(conn, True) == worker.INTERVAL)
    conn.execute("UPDATE wants SET retry_after=?", (time.time() + 3600,))
    conn.commit()
    check("a want on its empty-search cooldown is not due",
          worker.next_wait(conn, False) == worker.INTERVAL)
    conn = fresh(path)
    want(conn, "a", "Old Miss", 1000, status=db.STATUS_UNAVAILABLE,
         retry_after=time.time() - 60)
    check("retries alone do not hurry the next cycle",
          worker.next_wait(conn, False) == worker.INTERVAL)

print(f"\n{'FAILED' if bad else 'all checks passed'} ({bad} failure(s))")
sys.exit(1 if bad else 0)
