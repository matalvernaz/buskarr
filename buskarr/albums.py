"""Bring an album that arrived song by song back into one directory.

``db.canonical_album`` files each NEW song-by-song addition under the album its artist already has.
This repairs the ones that arrived before it: a list imported a song at a time, each with whatever
edition its catalogue listed, became "Take Me To The Alley" beside "Take Me to the Alley", or four
directories for "Hello, I Must Be Going". Each group of wants whose albums are the same record
(``db.album_fold``) settles on the spelling most of its songs already use, and the rest join it.

Two phases, because Jellyfin keys a song's favourite, play count and place on
"album artist - album - track - title", and re-links them to a moved file only while those still
match. Moving and retagging in one step would change the key on the way and lose them.

  move    (dry run unless --apply)   points the wants at the settled album and moves each file into
          that album's directory with its tags untouched. Writes a ledger of every move.
  retag   (--retag LEDGER [--apply]) once the media server has picked the moves up, writes the album,
          date and albumartist tags of the moved files, in place.

Only groups holding at least one song-by-song want (``batch IS NULL``) are touched. An artist or
album add files from one release's own listing and keeps a deluxe edition apart on purpose; those
directories stay as they are. A group whose known years are more than one apart is two releases
that share a name, and is reported rather than merged.
"""
import collections
import json
import os
import time

from . import db, refile, repair, scan, worker

LIBRARY = os.environ.get("LIBRARY_DIR", "/music")


def _choice_key(stats, album):
    """Rank the spellings within a group: most held songs, then most wants, then a plain name."""
    s = stats[album]
    plain = db.album_fold(album[0]) == " ".join(db._album_words(album[0]))
    return (s["held"], s["n"], plain, bool(db._year4(album[1])), -s["first"])


def plan(conn, artist=None):
    """Return (groups, skipped).

    ``groups`` is a list of dicts: lead, the settled (album, year), and the wants that move to it,
    each with its current and new path (None when nothing is on disk for it yet).
    """
    sql = "SELECT * FROM wants WHERE album IS NOT NULL AND album<>''"
    args = []
    if artist:
        sql += " AND artist_lead=?"
        args.append(artist)
    by_record = collections.defaultdict(list)
    for w in conn.execute(sql, args).fetchall():
        by_record[(w["artist_lead"], db.album_fold(w["album"]))].append(w)

    groups, skipped = [], []
    for (lead, key), members in sorted(by_record.items(), key=lambda kv: (kv[0][0] or "", kv[0][1])):
        spellings = {(w["album"], w["year"]) for w in members}
        if len(spellings) < 2 or not key:
            continue
        if not any(w["batch"] is None for w in members):
            continue                        # an artist or album add's editions, kept apart on purpose
        years = {int(y) for y in (db._year4(w["year"]) for w in members) if y}
        if years and max(years) - min(years) > 1:
            skipped.append((lead, key, sorted(spellings, key=str), "years disagree"))
            continue
        stats = {}
        for sp in spellings:
            rows = [w for w in members if (w["album"], w["year"]) == sp]
            stats[sp] = {
                "n": len(rows),
                "held": sum(1 for w in rows
                            if w["status"] == db.STATUS_HAVE and w["file_path"]),
                "first": min((w["requested_at"] or 0) for w in rows),
            }
        settled = max(spellings, key=lambda sp: _choice_key(stats, sp))
        moving = []
        for w in members:
            if (w["album"], w["year"]) == settled:
                continue
            old = w["file_path"]
            new = None
            if (old and w["status"] == db.STATUS_HAVE and w["provider"]
                    and os.path.exists(old) and worker.within_library(old)):
                ext = os.path.splitext(old)[1].lower()
                target = dict(w)
                target["album"], target["year"] = settled
                dest = worker.destination(target, ext, (scan.probe(old) or {}).get("tag_track"))
                if not refile._settled(old, dest):
                    new = dest
            moving.append({"id": w["id"], "title": w["title"], "album": w["album"],
                           "year": w["year"], "old": old if new else None, "new": new})
        groups.append({"lead": lead, "settled": settled, "stats": stats, "moving": moving})
    return groups, skipped


def move(conn, artist=None, dry_run=True, log=print, ledger_dir="/state"):
    groups, skipped = plan(conn, artist)
    if not groups:
        log("no album arrived split across directories")
    files = sum(1 for g in groups for m in g["moving"] if m["new"])
    for g in groups:
        album, year = g["settled"]
        log(f"\n{g['lead']} :: {album!r} ({year or 'no year'})")
        for sp, st in sorted(g["stats"].items(), key=lambda kv: -kv[1]["held"]):
            mark = "<- keeps" if sp == g["settled"] else ""
            log(f"    {st['held']:3d} held / {st['n']:3d} wanted  {sp[0]!r} ({sp[1] or '-'}) {mark}")
        for m in g["moving"]:
            if m["new"]:
                log(f"      {'would move' if dry_run else 'moving'} "
                    f"{os.path.relpath(m['old'], LIBRARY)}")
                log(f"          -> {os.path.relpath(m['new'], LIBRARY)}")
            else:
                log(f"      want {m['id']} {m['title']!r}: album label only (nothing on disk to move)")
    for lead, key, spellings, why in skipped:
        log(f"\nleft alone, {why}: {lead} :: {[s[0] for s in spellings]}")
    log(f"\n{len(groups)} album(s) to bring together, {files} file(s) to move")
    if dry_run:
        log("dry run, nothing changed. Re-run with --apply.")
        return {"albums": len(groups), "files": files, "ledger": None}

    entries = []
    for g in groups:
        album, year = g["settled"]
        for m in g["moving"]:
            entry = {"want": m["id"], "lead": g["lead"], "old_album": m["album"],
                     "old_year": m["year"], "album": album, "year": year,
                     "old_path": None, "path": None}
            if m["new"]:
                os.makedirs(os.path.dirname(m["new"]), exist_ok=True)
                # Tags untouched: see the module docstring. place() claims the name atomically and
                # suffixes on a collision rather than replacing anything.
                final = worker.place(m["old"], m["new"])
                refile._move_sidecars(m["old"], final, log)
                conn.execute("UPDATE wants SET file_path=? WHERE file_path=?", (final, m["old"]))
                conn.execute("DELETE FROM files WHERE path=?", (m["old"],))
                scan.index_file(conn, LIBRARY, final)
                refile._prune(os.path.dirname(m["old"]), LIBRARY)
                entry["old_path"], entry["path"] = m["old"], final
            conn.execute("UPDATE wants SET album=?, year=? WHERE id=?", (album, year, m["id"]))
            conn.commit()
            entries.append(entry)
    ledger = os.path.join(ledger_dir, f"albums-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(ledger, "w") as fh:
        json.dump(entries, fh, indent=1)
    db.log_event(conn, "albums", artist, f"{files} file(s) moved into {len(groups)} album(s)")
    log(f"ledger: {ledger}")
    log("Let the media server pick the moves up, then run --retag with this ledger.")
    return {"albums": len(groups), "files": files, "ledger": ledger}


def retag(conn, ledger, dry_run=True, log=print):
    """Write album, date and albumartist on the files a ``move`` placed. Nothing else is touched."""
    with open(ledger) as fh:
        entries = json.load(fh)
    done = failed = 0
    for e in entries:
        path = e.get("path")
        if not path:
            continue
        w = conn.execute("SELECT * FROM wants WHERE id=?", (e["want"],)).fetchone()
        if w is None or w["file_path"] != path or not os.path.exists(path):
            log(f"  skipped {path}: the want no longer names it")
            continue
        fields = {"album": w["album"], "album_artist": worker.album_artist(w)}
        if db._year4(w["year"]):
            fields["year"] = db._year4(w["year"])
        log(f"  {'would tag' if dry_run else 'tagging'} {os.path.relpath(path, LIBRARY)}  "
            + ", ".join(f"{k}={v!r}" for k, v in fields.items()))
        if dry_run:
            done += 1
            continue
        if repair.write_tags(path, **fields):
            scan.index_file(conn, LIBRARY, path)
            conn.commit()
            done += 1
        else:
            failed += 1
            log("      write failed; left as it was")
    log(f"{done} file(s) {'would be tagged' if dry_run else 'tagged'}"
        + (f", {failed} failed" if failed else ""))
    return {"tagged": done, "failed": failed}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Bring albums that arrived song by song together.")
    ap.add_argument("--apply", action="store_true", help="actually change things (default dry run)")
    ap.add_argument("--artist", default=None, help="limit to one lead artist directory")
    ap.add_argument("--retag", default=None, metavar="LEDGER",
                    help="second phase: tag the files a previous --apply moved")
    a = ap.parse_args()
    if a.retag:
        retag(db.init(), a.retag, dry_run=not a.apply)
    else:
        move(db.init(), artist=a.artist, dry_run=not a.apply)
