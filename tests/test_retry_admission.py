"""Which wants a cycle picks up, and in what order.

A want nothing could find is marked UNAVAILABLE with a `retry_after` and a note
promising another look in N days. Nothing kept that promise: the selector asked
only for PENDING and FAILED, so the deadline was written, passed, and never
read. Every unavailable want in the live database sat at strike 1 -- one
attempt, ever -- which is the signature of a retry that never ran.

The ordering half matters as much. Unavailable wants are the oldest rows by
`requested_at`, so admitting them without a rule would let a long backlog take
every slot of every cycle ahead of something asked for a minute ago.
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


def want(conn, title, status, age_days, retry_after=None, strikes=0):
    wid, _ = db.add_want(conn, "An Act", title, "An Album", "1999", 100.0,
                         artist_lead="An Act")
    conn.execute(
        "UPDATE wants SET status=?, requested_at=?, retry_after=?, strikes=? WHERE id=?",
        (status, time.time() - age_days * 86400, retry_after, strikes, wid))
    conn.commit()
    return wid


with tempfile.TemporaryDirectory() as d:
    conn = db.init(os.path.join(d, "t.db"))
    now = time.time()

    def titles():
        return [r["title"] for r in worker.due_wants(conn, now, 50)]

    print("=== a retry deadline that has passed is honoured ===")
    want(conn, "Due", db.STATUS_UNAVAILABLE, 30, retry_after=now - 3600, strikes=1)
    check("the want is looked at again", "Due" in titles(), str(titles()))

    print("\n=== but a deadline in the future is still a deadline ===")
    want(conn, "NotDue", db.STATUS_UNAVAILABLE, 30, retry_after=now + 86400, strikes=1)
    check("not before its cooldown ends", "NotDue" not in titles(), str(titles()))

    print("\n=== and a retry never takes a slot from a fresh request ===")
    want(conn, "AskedJustNow", db.STATUS_PENDING, 0)
    check("pending is worked first despite being newest",
          titles()[0] == "AskedJustNow", str(titles()))
    check("the retry still gets its turn behind it", "Due" in titles(), str(titles()))

    print("\n=== the states that always ran keep running ===")
    want(conn, "Failed", db.STATUS_FAILED, 1)
    want(conn, "Have", db.STATUS_HAVE, 1)
    check("failed is still admitted", "Failed" in titles(), str(titles()))
    check("a want already in the library is not", "Have" not in titles(), str(titles()))

    print("\n=== the cap is still a cap ===")
    check("the limit is applied", len(worker.due_wants(conn, now, 2)) == 2,
          str(worker.due_wants(conn, now, 2)))

print(f"\n{'FAILED' if bad else 'all checks passed'} ({bad} failure(s))")
sys.exit(1 if bad else 0)
