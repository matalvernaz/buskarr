"""A cycle works each want as it stands when its turn comes, not as it stood when the cycle began.

2026-10-10: a cycle of 37 wants ran for 51 minutes. A want marked had at 11:41, the song being
already held under another credit, was reached at 12:04 from the copy taken at 11:14, queued at a
Soulseek peer and set back to searching. Another, moved to its artist's existing directory at
11:37, was filed under its old one at 11:52.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from buskarr import db, worker  # noqa: E402

bad = 0


def check(label, ok, detail=""):
    global bad
    if not ok:
        bad += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{('  — ' + detail) if detail else ''}")


with tempfile.TemporaryDirectory() as d:
    conn = db.init(os.path.join(d, "t.db"))
    first, _ = db.add_want(conn, "An Act", "First", None, None, 100.0, "someone")
    held, _ = db.add_want(conn, "An Act", "Held Meanwhile", None, None, 110.0, "someone")
    moved, _ = db.add_want(conn, "An Act", "Moved Meanwhile", None, None, 120.0, "someone")
    seen = []

    def attempt(conn, want, provs):
        seen.append((want["id"], want["artist_lead"]))
        if want["id"] == first:
            # What a person, or a repair, does while the cycle is busy with this one.
            conn.execute("UPDATE wants SET status=?, file_path=? WHERE id=?",
                         (db.STATUS_HAVE, "/music/An Act/Held Meanwhile.flac", held))
            conn.execute("UPDATE wants SET artist_lead=? WHERE id=?", ("Another Act", moved))
            conn.commit()
        return "queued"

    worker.attempt = attempt
    worker.run_jobs = lambda conn: None
    worker.run_harvest = lambda conn: None
    worker.run_grabs = lambda conn: None
    worker.providers.enabled = lambda: [{"name": "fake", "available": True, "healthy": True,
                                         "detail": ""}]
    worker.ready = lambda provs: provs
    worker.log = lambda *a: None

    print("=== a cycle reads each want again when its turn comes ===")
    worker.cycle(conn)
    ids = [i for i, _ in seen]
    check("a want marked had during the cycle is not fetched", held not in ids, str(seen))
    check("and stays had", conn.execute("SELECT status FROM wants WHERE id=?",
                                        (held,)).fetchone()[0] == db.STATUS_HAVE)
    check("a want moved during the cycle is worked with its new directory",
          (moved, "Another Act") in seen, str(seen))
    check("the rest of the cycle still runs", first in ids, str(seen))

print(f"\n{bad} failure(s)")
sys.exit(1 if bad else 0)
