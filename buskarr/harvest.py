"""Harvest completed downloads into the library.

Soulseek and torrents are both asynchronous and both album-granular: you enqueue a release and a
directory of files appears later. This is the component that closes that loop — it walks download
directories, matches files against wanted tracks, and imports the ones that pass vetting.

It also mines what has already accumulated. As of writing there are ~694 completed download
directories holding ~106GB that were never imported, because Lidarr refuses a release unless 80% of
an album matches — so an 11-of-16-track download is discarded whole despite every one of those 11
files being perfectly good. Working per track instead of per album recovers them.

Copies, never moves: the download stays where the torrent client expects it, so seeding is
unaffected. Nothing here deletes anything, ever.
"""
import os
import time

from . import db, match, scan, worker

DOWNLOAD_DIRS = [d for d in os.environ.get(
    "DOWNLOAD_DIRS", "/downloads/slskd/complete,/downloads/complete").split(",") if d.strip()]
AUDIO_EXT = scan.AUDIO_EXT
# A download directory whose files are all already held is skipped without probing every file.
MAX_FILES_PER_DIR = int(os.environ.get("HARVEST_MAX_FILES_PER_DIR", "400"))


def log(msg):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} harvest: {msg}", flush=True)


def candidate_guesses(fn):
    """Plausible title readings of one filename, longest first.

    Downloads name files "NN - Title", "Artist - Title", and "Artist - Album - NN. Title". The
    old single split-once reading missed the third form entirely: the guess kept
    "Album - NN. Title", whose norm matches no want, so a wanted file was never even probed.
    Every " - " suffix is offered instead, each with any leading track number stripped — the
    bucket lookup decides which reading names a wanted song, and vet() stays the judge of
    whether the audio actually is that song.
    """
    stem = os.path.splitext(fn)[0]
    parts = [p.strip() for p in stem.split(" - ")]
    guesses = []
    for i in range(len(parts)):
        g = scan.TRACK_PREFIX.sub("", " - ".join(parts[i:])).strip()
        if g and g not in guesses:
            guesses.append(g)
    return guesses


def candidate_files(root):
    """Yield (path, [guessed titles]) for audio files under a download tree."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        count = 0
        for fn in sorted(filenames):
            if not fn.lower().endswith(AUDIO_EXT):
                continue
            count += 1
            if count > MAX_FILES_PER_DIR:
                break
            yield os.path.join(dirpath, fn), candidate_guesses(fn)


def _dest_belongs_to(conn, dest, want_id, source_duration=None):
    """Whether an existing library file at ``dest`` is this want's to finish.

    Two things have to hold. No other want may already have recorded that exact path
    as its own -- ``destination`` derives the name from artist, album and track, so a
    collision means two wants really are claiming one name and the older claim keeps
    it. And the file that is there has to be the recording this want is resuming.

    The second check is the one that is easy to leave out. A want killed before its
    status was written owns its file without saying so, so "nobody claims it" alone
    would let the next want that happens to compute the same name adopt somebody
    else's audio. Duration decides it: a resumed file is a copy of the download in
    hand and agrees, a different recording under the same name does not. A file that
    cannot be probed is not adopted, because the safe answer to "is this mine" is no.
    """
    row = conn.execute(
        "SELECT id FROM wants WHERE file_path=? AND id<>? LIMIT 1", (dest, want_id)
    ).fetchone()
    if row is not None:
        return False
    if source_duration is None:
        return True
    there = scan.probe(dest)
    if not there or not there.get("duration"):
        return False
    return match.duration_ok(source_duration, there["duration"])


def harvest(conn, dry_run=True, limit=0, log=log):
    """Match wanted tracks against completed downloads and import the BEST copy of each.

    Two-pass on purpose. A single streaming pass would import whichever copy it encountered first
    and then import every further copy of the same song — and duplicate directories are the norm
    here, with the same album downloaded three times at different bitrates.
    """
    wanted = conn.execute(
        "SELECT * FROM wants WHERE status IN (?,?,?)",
        (db.STATUS_PENDING, db.STATUS_SEARCHING, db.STATUS_UNAVAILABLE)).fetchall()
    if not wanted:
        log("nothing wanted; skipping")
        return {"considered": 0, "imported": 0}

    # Bucketed under db.norm, NOT the wants' own norm_title. That column holds the STRICT form
    # (parentheticals preserved), while the lookup below normalises the filename guess with
    # db.norm, which strips them — so a want for "Song (live)" lived in a bucket no filename key
    # could ever reach, and was simply unharvestable. vet()'s duration gate is what tells the
    # live take from the studio one, exactly as it does for every search provider.
    by_title = {}
    for w in wanted:
        by_title.setdefault(db.norm(w["title"]), []).append(w)
    log(f"{len(wanted)} wanted track(s) to look for across {len(DOWNLOAD_DIRS)} download dir(s)")

    # Pass one: gather every acceptable copy of every wanted track.
    found = {}                      # want id -> list of (score, path, info)
    considered = 0
    for root in DOWNLOAD_DIRS:
        if not os.path.isdir(root):
            log(f"  {root}: not present, skipping")
            continue
        for path, guesses in candidate_files(root):
            guess = next((g for g in guesses if by_title.get(db.norm(g))), None)
            if guess is None:
                continue
            hits = by_title[db.norm(guess)]
            considered += 1
            info = scan.probe(path)
            if not info:
                continue
            for w in hits:
                cand = {
                    "title": info["tag_title"] or guess,
                    # Tag artist if present, otherwise the download's own path. Never w["artist"]:
                    # that hands the gate the answer, so an untagged file matching only on filename
                    # would pass the artist check by construction.
                    "artist": info["tag_artist"] or os.path.relpath(path, start="/"),
                    "duration": info["duration"],
                    "codec": info["codec"], "bitrate": info["bitrate"],
                }
                ok, why = match.vet(dict(w), cand)
                if not ok:
                    log(f"    rejected {os.path.basename(path)[:46]} — {why}")
                    continue
                score = match.quality_score(info["codec"], info["bitrate"],
                                            info["sample_rate"], info["bit_depth"])
                found.setdefault(w["id"], []).append((score, path, info, w))
                break

    # Pass two: import only the best copy of each track.
    imported = 0
    for wid, copies in found.items():
        copies.sort(key=lambda c: -c[0])
        score, path, info, w = copies[0]
        others = len(copies) - 1
        ext = os.path.splitext(path)[1].lower()
        dest = worker.destination(w, ext, info.get("tag_track"))

        # A destination that already exists used to end this want here, for ever. The
        # steps after the copy -- tag, index, set the want to have -- are separate, so a
        # process killed between them left a complete playable file at the canonical
        # name with the want still pending, and every later harvest skipped it on this
        # check. Three of those states were reproduced. Finishing the remaining steps is
        # safe because each is idempotent, and it is the only path that recovers them.
        resuming = os.path.exists(dest)
        if resuming and not _dest_belongs_to(conn, dest, wid, info.get("duration")):
            log(f"    skipping {os.path.basename(dest)[:46]} — another want already holds that name")
            continue
        # Only meaningful for a name nothing has claimed: when resuming, the duplicate
        # this would find is the half-imported file itself.
        if not resuming and not w["allow_dup"] and db.find_exact(conn, w["artist"], w["title"], w["duration"]):
            continue
        extra = f" (best of {len(copies)} copies)" if others else ""
        verb = "would import" if dry_run else ("finishing" if resuming else "importing")
        log(f"    {verb} {w['artist']} - {w['title']}"
            f"  <- {os.path.basename(path)[:44]} "
            f"({info['codec']} {int((info['bitrate'] or 0) / 1000)}k){extra}")
        imported += 1
        if not dry_run:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if not resuming:
                # Copy, never move: the torrent client still needs the original in place to seed.
                # And claim the name atomically — the exists-check above is only a cheap skip, not a
                # guarantee. shutil.copy2 TRUNCATES an existing destination, so racing the worker's
                # placement of the same want would have destroyed whichever file landed first.
                try:
                    worker.copy_no_replace(path, dest)
                except FileExistsError:
                    imported -= 1
                    continue
            # Retagging on resume matters rather than being belt-and-braces: a kill after
            # the copy and before the tag leaves the source release's own album on the
            # file, not the one that was asked for.
            worker.tag(dest, w, info.get("tag_track"))
            scan.index_file(conn, worker.LIBRARY, dest)
            conn.execute(
                "UPDATE wants SET status=?, provider=?, file_path=?, strikes=0,"
                " retry_after=NULL, note=? WHERE id=?",
                (db.STATUS_HAVE, "harvest", dest,
                 f"{info['codec']} {int((info['bitrate'] or 0) / 1000)}k from completed download",
                 wid))
            conn.commit()
            db.log_event(conn, "harvested", f"{w['artist']} - {w['title']}",
                         os.path.basename(path))
        if limit and imported >= limit:
            log(f"  limit {limit} reached")
            break

    log(f"done: {considered} file(s) considered, {len(found)} track(s) matched, "
        f"{imported} {'importable' if dry_run else 'imported'}")
    return {"considered": considered, "matched": len(found), "imported": imported}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually import (default is dry-run)")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    harvest(db.init(), dry_run=not a.apply, limit=a.limit)
