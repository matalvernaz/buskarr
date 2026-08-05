"""Fold collaboration folders into the lead artist's folder.

Placement names the artist directory from the lead credit, but files placed before that did so from
the whole credit string — so one band ended up with eight directories ("The Longest Johns",
"… & Lucy Humphris", "… & El Pony Pisador", "… feat. SKÁLD", "… ft. SKÁLD" and more), which reads as
eight artists to anyone browsing.

The rule is ``credit.credited_to``, the same test that decides what a discography add queues, so this
cannot fold anything that test would not call the same artist's work. Two consequences worth stating:

  * The lead artist must ALREADY have a folder. Nothing is invented, and "Simon & Garfunkel" only
    folds if a folder literally named "Simon" exists — which no real library has.
  * A folder where the artist is the GUEST ("Celtic Woman feat. The Longest Johns") does not fold,
    because it is not their release. It is Celtic Woman's, and it stays under Celtic Woman.

Files are moved, never deleted, and never onto an existing path. The ``albumartist`` tag is written
as the lead so a media server groups them too — folder layout alone does not.
"""
import os

from . import credit, db, repair, worker

LIBRARY = os.environ.get("LIBRARY_DIR", "/music")


def artist_dirs(root):
    """Real directories directly under the library root.

    ``islink`` is excluded rather than followed: ``os.path.isdir`` follows symlinks, and a symlinked
    artist entry would have this walk and move files that live outside the library entirely.
    """
    return sorted(d for d in os.listdir(root)
                  if not d.startswith(".")
                  and not os.path.islink(os.path.join(root, d))
                  and os.path.isdir(os.path.join(root, d)))


def plan(root):
    """[(folder, lead_folder)] for every artist directory that belongs inside another one.

    The longest qualifying parent wins: "The Longest Johns & Lucy Humphris" must fold into
    "The Longest Johns" and not into some shorter folder that also happens to pass the credit test.

    Deepest name first, and each source resolved transitively to its terminal parent. Applying the
    pairs alphabetically let "A feat. B" fold into "A" and be pruned before "A feat. B & C" was
    processed, which then recreated the intermediate directory and left files one hop short of the
    lead — needing a second run to converge, and misdirecting a refile in between.
    """
    dirs = artist_dirs(root)
    direct = {}
    for d in dirs:
        parents = [p for p in dirs if p != d and credit.credited_to(d, p)]
        if parents:
            direct[d] = max(parents, key=len)
    out = []
    for d in direct:
        lead, seen = direct[d], {d}
        while lead in direct and lead not in seen:
            seen.add(lead)
            lead = direct[lead]
        out.append((d, lead))
    return sorted(out, key=lambda pair: len(pair[0]), reverse=True)


def _prune(path, stop):
    """Remove ``path`` and every empty directory under it, deepest first. Never touches ``stop``.

    Depth-first is the whole point: after a fold the artist directory is empty of files but still
    holds its album directories, so removing it first fails with ENOTEMPTY and the entire skeleton
    survives — which is precisely what a first attempt left behind. A directory that still holds
    files is skipped, so this can never remove one with audio in it.
    """
    if os.path.realpath(path) == os.path.realpath(stop):
        return
    for dirpath, _, filenames in os.walk(path, topdown=False):
        if filenames:
            continue
        try:
            os.rmdir(dirpath)
        except OSError:
            pass


def _adopt_wants(conn, src_name, lead):
    """Point every want that filed under ``src_name`` at ``lead`` instead. Returns how many.

    Matched by recomputing the directory each want would use — ``safe(folder_artist(want))`` — not by
    comparing ``artist_lead`` to the folder name as a string. Directory names have been through
    ``worker.safe``, which rewrites "/" and "\\\\" to "_", strips trailing dots and spaces, and
    truncates at 180 bytes, while the column holds the raw credit. For any credit ``safe()`` alters —
    "AC/DC & Someone" becomes the folder "AC_DC & Someone" — a string comparison matched nothing, so
    the wants kept the old lead, refile moved the files straight back, and the next fold moved them
    again: an indefinite ping-pong between the two tools.
    """
    n = 0
    for w in conn.execute("SELECT id, artist, artist_lead FROM wants").fetchall():
        if worker.safe(worker.folder_artist(w)) == src_name:
            conn.execute("UPDATE wants SET artist_lead=? WHERE id=?", (lead, w["id"]))
            n += 1
    return n


def fold(conn, root=LIBRARY, dry_run=True, log=print):
    pairs = plan(root)
    if not pairs:
        log("no collaboration folders to fold")
        return {"folders": 0, "moved": 0, "collisions": 0}
    log(f"{len(pairs)} folder(s) to fold into a lead artist:")
    moved = collisions = 0
    for src_name, lead in pairs:
        src = os.path.join(root, src_name)
        log(f"  {src_name!r} -> {lead!r}")
        for dirpath, _, filenames in os.walk(src):
            rel_dir = os.path.relpath(dirpath, src)
            for fn in sorted(filenames):
                old = os.path.join(dirpath, fn)
                new = os.path.normpath(os.path.join(root, lead, rel_dir, fn))
                # Both ends re-checked immediately before the move. os.walk can descend a symlinked
                # album directory even when the artist entry itself is real, and nothing else here
                # constrains the destination the way worker.destination() asserts for placements.
                if not worker.within_library(old) or not worker.within_library(new):
                    log(f"      SKIP {fn[:52]}: resolves outside the library")
                    continue
                log(f"      {'would move' if dry_run else 'moving'} {rel_dir}/{fn[:52]}"
                    + ("  [name may be taken; will suffix]" if os.path.exists(new) else ""))
                moved += 1
                if dry_run:
                    continue
                os.makedirs(os.path.dirname(new), exist_ok=True)
                # Atomic claim rather than check-then-move: rename() replaces silently, and the
                # worker may place a colliding file in the window.
                final = worker.place(old, new)
                if final != new:
                    collisions += 1
                # The lead belongs in the tags too: a media server builds its artist list from tags,
                # not from directory names, so moving the file alone would not group anything.
                repair.write_tags(final, album_artist=lead)
                conn.execute("DELETE FROM files WHERE path=?", (old,))
                # Re-point the want as well. Leaving it stale meant a refile run before the next
                # rescan saw a file_path that no longer existed and skipped the track entirely.
                conn.execute("UPDATE wants SET file_path=? WHERE file_path=?", (final, old))
        if not dry_run:
            _adopt_wants(conn, src_name, lead)
            _prune(src, root)
            conn.commit()
    if not dry_run:
        db.log_event(conn, "fold-credits", None,
                     f"{moved} file(s) from {len(pairs)} collaboration folder(s)")
    log(f"\n{len(pairs)} folder(s), {moved} file(s) "
        f"{'would be folded' if dry_run else 'folded'}, {collisions} name collision(s)")
    if dry_run:
        log("dry run — nothing moved. Re-run with --apply, then rescan.")
    else:
        log("rescan now so the files table and every want point at the new paths.")
    return {"folders": len(pairs), "moved": moved, "collisions": collisions}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="Move collaboration folders into the lead artist's folder.")
    ap.add_argument("--apply", action="store_true", help="actually move (default is dry-run)")
    a = ap.parse_args()
    fold(db.init(), dry_run=not a.apply)
