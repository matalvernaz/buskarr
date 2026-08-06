"""Move placed files to where the current placement rules say they belong.

``worker.destination`` is the single definition of where a file goes, but it only runs once, at
download time. When that definition changes — or when a want learns facts it did not have — every
file placed under the old answer stays where it was. Two such changes have happened:

  * the artist directory became the LEAD credit rather than the whole credit string, and
  * MusicBrainz artist adds gained album attribution, having previously reported none at all, which
    had filed entire discographies under "Artist/Singles/".

Rather than a migration per change, this recomputes ``destination()`` for every want that has a file
and moves the ones that disagree. Idempotent: a second run finds nothing.

Only files this tool can account for are touched — a want with a recorded path that exists. Anything
else is left alone, which is the same rule the rest of the project follows.
"""
import os
import re

from . import db, scan, worker

LIBRARY = os.environ.get("LIBRARY_DIR", "/music")

# Companion files that belong to one audio file and are named from it. Lyrics are written next to
# the audio by an external tool, so they are not in the files table and nothing else knows they
# exist — a move that leaves them behind orphans them in a directory the audio has left.
SIDECAR_EXT = (".lrc", ".txt")


def _move_sidecars(old, new, log):
    """Move ``old``'s same-stem companions alongside ``new``. Returns how many moved."""
    old_stem, new_stem = os.path.splitext(old)[0], os.path.splitext(new)[0]
    moved = 0
    for ext in SIDECAR_EXT:
        src = old_stem + ext
        if not os.path.exists(src):
            continue
        try:
            worker.place(src, new_stem + ext)
            moved += 1
        except OSError as e:
            # Never fail a refile over a lyrics file; the audio move is what matters.
            log(f"      sidecar {os.path.basename(src)} not moved ({type(e).__name__})")
    return moved


def _settled(path, canonical):
    """True when ``path`` is already the canonical name or an accepted suffixed form of it.

    Without this the module was not idempotent, contradicting its own docstring: once a collision
    parked a file at "01 - Song (2).flac", plan() still measured it against the unsuffixed canonical
    path, and each run bumped it to (3), (4), (5)… rewriting tags and database rows every time.
    """
    if os.path.realpath(path) == os.path.realpath(canonical):
        return True
    stem, ext = os.path.splitext(canonical)
    if os.path.dirname(os.path.realpath(path)) != os.path.dirname(os.path.realpath(canonical)):
        return False
    # Only the numeric suffixes worker.place() can generate. Accepting any parenthetical treated
    # "01 - Song (demo).flac" as a settled sibling of "01 - Song.flac", so a genuinely misfiled
    # demo would never be moved to where it belongs.
    m = re.fullmatch(re.escape(os.path.basename(stem)) + r" \((\d+)\)" + re.escape(ext),
                     os.path.basename(path))
    # >= 2 because place() starts suffixing at (2); "(1)" is not a name it can produce.
    return bool(m) and int(m.group(1)) >= 2


def _prune(path, stop):
    """Remove empty directories under and including ``path``, deepest first. Never touches ``stop``."""
    if os.path.realpath(path) == os.path.realpath(stop):
        return
    for dirpath, _, filenames in os.walk(path, topdown=False):
        if filenames:
            continue
        try:
            os.rmdir(dirpath)
        except OSError:
            pass


def plan(conn, artist=None):
    """[(want, current_path, wanted_path)] for every placed file sitting in the wrong place."""
    sql = ("SELECT * FROM wants WHERE file_path IS NOT NULL AND status=? "
           "AND provider IS NOT NULL")
    args = [db.STATUS_HAVE]
    if artist:
        sql += " AND artist_lead=?"
        args.append(artist)
    out = []
    for w in conn.execute(sql, args).fetchall():
        path = w["file_path"]
        if not os.path.exists(path) or not worker.within_library(path):
            continue
        ext = os.path.splitext(path)[1].lower()
        # The track number lives in the file's own tags, which is where destination() got it from
        # originally; re-deriving it keeps the filename and the tag agreeing.
        info = scan.probe(path) or {}
        dest = worker.destination(w, ext, info.get("tag_track"))
        if not _settled(path, dest):
            out.append((w, path, dest))
    return out


def refile(conn, artist=None, dry_run=True, log=print):
    moves = plan(conn, artist)
    if not moves:
        log("every placed file is already where the current rules put it")
        return {"moved": 0, "collisions": 0}
    log(f"{len(moves)} file(s) to move:")
    moved = collisions = sidecars = 0
    for w, old, new in moves:
        log(f"  {'would move' if dry_run else 'moving'} {os.path.relpath(old, LIBRARY)}")
        log(f"      -> {os.path.relpath(new, LIBRARY)}"
            + ("  [name may be taken; will suffix]" if os.path.exists(new) else ""))
        moved += 1
        if dry_run:
            continue
        os.makedirs(os.path.dirname(new), exist_ok=True)
        # Atomic claim, not check-then-move: rename() replaces silently, and the worker can place a
        # colliding file in the window between checking and moving.
        final = worker.place(old, new)
        if final != new:
            collisions += 1
        # The album tag has to follow the move, or the next scan reads the old one back off the file
        # and the library disagrees with its own directory layout.
        worker.tag(final, w, (scan.probe(final) or {}).get("tag_track"))
        sidecars += _move_sidecars(old, final, log)
        conn.execute("UPDATE wants SET file_path=? WHERE id=?", (final, w["id"]))
        conn.execute("DELETE FROM files WHERE path=?", (old,))
        conn.commit()
        _prune(os.path.dirname(old), LIBRARY)
    if not dry_run:
        db.log_event(conn, "refile", artist, f"{moved} file(s) moved to current placement rules")
    log(f"\n{moved} file(s) {'would be moved' if dry_run else 'moved'}, "
        f"{collisions} name collision(s)"
        + (f", {sidecars} sidecar(s) moved" if sidecars else ""))
    if dry_run:
        log("dry run — nothing moved. Re-run with --apply, then rescan.")
    else:
        log("rescan now so the files table matches the new paths.")
    return {"moved": moved, "collisions": collisions}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Re-file placed audio to the current layout rules.")
    ap.add_argument("--apply", action="store_true", help="actually move (default is dry-run)")
    ap.add_argument("--artist", default=None, help="limit to one lead artist")
    a = ap.parse_args()
    refile(db.init(), artist=a.artist, dry_run=not a.apply)
