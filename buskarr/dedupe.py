"""Deduplicate the library, keeping the best copy of each recording.

The matching rules here are the product of three failed attempts on this library, each of which
would have destroyed real music:

1. Grouping on **tag** titles collapsed different songs together, because whole albums are ripped
   with one identical bogus title. Group on the FILENAME title.
2. Stripping parentheticals collapsed distinct recordings — ``(reprise)``, ``(demo)``,
   ``(Live Bait)``, ``(Semi-Conducted)`` are different performances. Keep parentheticals.
3. Title agreement alone is insufficient. ``Rocks and Trees`` appears at 285s and at 119s; those
   cannot be one recording. **Durations must agree** for two files to be the same thing.

Files are MOVED to a quarantine directory, never deleted, so any mistake is reversible with mv.
"""
import collections
import os
import re
import unicodedata

from . import db, match, worker

QUARANTINE = os.environ.get("QUARANTINE_DIR", "/music/.buskarr_quarantine")
LIBRARY = os.environ.get("LIBRARY_DIR", "/music")
# Two files are the same recording only if their lengths agree this closely. Deliberately tight:
# it is the single signal that survived when title matching repeatedly did not.
DURATION_TOLERANCE = float(os.environ.get("DEDUPE_DURATION_TOLERANCE", "4.0"))


def strict_norm(s):
    """Normalise for dedup only — parentheticals are PRESERVED because they carry meaning."""
    s = unicodedata.normalize("NFKD", s or "").lower().replace("’", "'").replace("´", "'")
    return re.sub(r"[^a-z0-9()]+", " ", s).strip()


def groups(conn):
    """Yield (key, [rows]) for each set of files that are genuinely the same recording."""
    rows = conn.execute("SELECT * FROM files WHERE codec != 'UNREADABLE'").fetchall()
    buckets = collections.defaultdict(list)
    for r in rows:
        buckets[(r["norm_artist"], strict_norm(r["file_title"] or r["title"]))].append(r)

    for key, items in buckets.items():
        if len(items) < 2:
            continue
        timed = [x for x in items if x["duration"]]
        timed.sort(key=lambda x: x["duration"])
        run = []
        for x in timed:
            # Anchored to the FIRST member, not the previous one. Comparing to the previous item
            # lets a run walk: 100s/104s/108s all cluster at a 4s tolerance though the ends differ
            # by 8s, and distinct recordings get quarantined as duplicates of each other.
            if run and abs(x["duration"] - run[0]["duration"]) <= DURATION_TOLERANCE:
                run.append(x)
            else:
                if len(run) > 1:
                    yield key, list(run)
                run = [x]
        if len(run) > 1:
            yield key, list(run)


def rank(row):
    """Sort key: best copy first. Lossless and hi-res beat bitrate; bitrate breaks ties."""
    return (-match.quality_score(row["codec"], row["bitrate"],
                                 row["sample_rate"], row["bit_depth"]),
            -(row["bitrate"] or 0), -(row["size"] or 0))


def dedupe(conn, dry_run=True, limit=0, log=print):
    total_groups = kept = moved = 0
    freed = 0
    for key, items in groups(conn):
        items = sorted(items, key=rank)
        keep, losers = items[0], items[1:]
        total_groups += 1
        kept += 1
        log(f"  {key[1][:44]:<46} keep {keep['codec']}"
            f" {int((keep['bitrate'] or 0) / 1000)}k")
        for lo in losers:
            rel = os.path.relpath(lo["path"], LIBRARY)
            dest = os.path.join(QUARANTINE, rel)
            log(f"      {'would move' if dry_run else 'moving'} {lo['codec']}"
                f" {int((lo['bitrate'] or 0) / 1000)}k  {rel[:58]}")
            freed += lo["size"] or 0
            moved += 1
            if not dry_run:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                # Atomic claim, not check-then-move: the quarantine path is deterministic, and
                # shutil.move onto an existing name overwrites it — a second run over a reused
                # library path would have destroyed the copy the first run saved. place() suffixes
                # past whatever is already there. Nothing here may ever lose audio.
                worker.place(lo["path"], dest)
                conn.execute("DELETE FROM files WHERE path=?", (lo["path"],))
                conn.commit()
        if limit and total_groups >= limit:
            log(f"  limit {limit} groups reached")
            break

    if not dry_run:
        db.log_event(conn, "dedupe", None,
                     f"{moved} file(s) quarantined from {total_groups} group(s)")
    log(f"\n{total_groups} group(s), {moved} file(s) "
        f"{'would be quarantined' if dry_run else 'quarantined'}, "
        f"{freed / 1e9:.2f} GB")
    if dry_run:
        log("dry run — nothing moved. Re-run with --apply.")
    else:
        log(f"originals are under {QUARANTINE} — restore any with mv.")
    return {"groups": total_groups, "moved": moved, "bytes": freed}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Quarantine duplicate recordings, keeping the best.")
    ap.add_argument("--apply", action="store_true", help="actually move (default is dry-run)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N groups")
    a = ap.parse_args()
    dedupe(db.init(), dry_run=not a.apply, limit=a.limit)
