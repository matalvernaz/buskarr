"""Find and quarantine files whose AUDIO is identical to another copy in the library.

Why this exists alongside ``dedupe``. That module compares filename titles and durations, which
is the right tool for "the same song ripped twice at different quality" — but it cannot tell a
relabelled copy of one master from two genuinely different recordings that happen to share a
title and a length, so its output is candidates a human confirms.

This module asks the only question that has a definite answer: **is the decoded audio the same
bytes?** Nothing else works here, because ``worker.tag`` writes tags from the want, so two copies
of one recording have different file checksums while their audio is identical. On this library
that distinction found 55 redundant files that filename, title and ``dedupe`` checks all missed —
one provider had served a single recording to every edition-variant want ("(2013 remixed
version)", "(2023 re-recorded version)", "(Chiptune)", "(karaoke mix)") and each was placed under
its own name.

Its blind spot is the mirror image: the same performance RE-ENCODED (a FLAC against an MP3 of one
master) decodes to different bytes and is invisible here. That case is ``dedupe``'s and
``upgrade``'s. Run both.

Candidates are narrowed by ``db.edition_fold`` plus agreeing durations before anything is
decoded, because hashing the whole library would decode every file. A wrongly narrow candidate
set only means a duplicate goes unfound, never that a distinct recording is quarantined — the
hash is what decides, and it cannot be wrong about identity.

Files are MOVED to the quarantine directory, never deleted, so any mistake is reversible with mv.
"""
import collections
import os
import re
import subprocess

from . import db, worker

QUARANTINE = os.environ.get("QUARANTINE_DIR", "/music/.buskarr_quarantine")
LIBRARY = os.environ.get("LIBRARY_DIR", "/music")
AUDIO_HASH_TIMEOUT = 900

# Which copy survives when several are identical. Mirrors bulk.RELEASE_RANK's intent at the
# directory level, since a placed file knows only the album name it sits under: the original
# release keeps the recording, and a reissue or a hits collection yields to it.
COMPILATION_HINTS = ("greatest hits", "hits", "very best", "best of", "collection", "compilation",
                     "essential", "super hits", "anthology", "classics")
EDITION_HINTS = ("deluxe", "anniversary", "expanded", "remaster", "special edition", "edition")


def log(msg):
    print(msg, flush=True)


def audio_hash(path):
    """MD5 of the DECODED audio stream, or None if it cannot be read.

    ``-map 0:a:0`` so cover art cannot influence the result, and the container is bypassed
    entirely — a FLAC and its own retagged copy hash the same, which is the whole point.
    """
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-v", "quiet", "-i", path, "-map", "0:a:0", "-f", "md5",
             "-"], capture_output=True, text=True, timeout=AUDIO_HASH_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return None
    out = (r.stdout or "").strip()
    return out.replace("MD5=", "") if out.startswith("MD5=") else None


def _title_of(path):
    """Filename title — the NN prefix removed. The filename, never the tag: whole albums in this
    library are ripped carrying one bogus tag title."""
    return re.sub(r"^\d+\s*-\s*", "", os.path.splitext(os.path.basename(path))[0])


def _rank(path):
    """(release class, year) for survivor choice. Lower sorts first and is kept."""
    album = os.path.basename(os.path.dirname(path)).lower()
    year = re.search(r"\((\d{4})\)\s*$", album)
    bare = re.sub(r"\(\d{4}\)\s*$", "", album).strip()
    if any(h in bare for h in COMPILATION_HINTS):
        cls = 3
    elif any(h in bare for h in EDITION_HINTS):
        cls = 2
    elif bare == "singles":
        cls = 1
    else:
        cls = 0
    return cls, int(year.group(1)) if year else 9999


def _plainness(path):
    """Prefer the canonical name. Two identical copies in one directory differ only by an edition
    label, and without this the survivor was decided by where a space sorts against a full stop."""
    stem = os.path.splitext(os.path.basename(path))[0]
    return len(re.findall(r"\([^)]*\)|\[[^\]]*\]", stem)), len(stem)


def candidates(conn, artist=None):
    """[[rows]] — sets of files that MIGHT be one recording, cheap checks only.

    Grouped on the artist directory plus the edition-folded filename title, then split on
    duration so a title reused across genuinely different lengths does not form one bucket.
    """
    sql = "SELECT * FROM files WHERE duration IS NOT NULL"
    args = []
    if artist:
        sql += " AND artist_lead=?"
        args.append(artist)
    buckets = collections.defaultdict(list)
    for r in conn.execute(sql, args):
        buckets[(r["artist_lead"], db.edition_fold(_title_of(r["path"])))].append(dict(r))
    out = []
    for rows in buckets.values():
        if len(rows) < 2:
            continue
        rows.sort(key=lambda x: x["duration"])
        run = [rows[0]]
        for f in rows[1:]:
            # Anchored on the run's first file, not its neighbour: single-linkage clustering at a
            # 4s tolerance chained 100s, 104s and 108s into one group.
            if abs(f["duration"] - run[0]["duration"]) <= db.TWIN_DURATION:
                run.append(f)
            else:
                if len(run) > 1:
                    out.append(run)
                run = [f]
        if len(run) > 1:
            out.append(run)
    return out


def find(conn, artist=None, log=log):
    """[(hash, [paths])] for every set of files whose decoded audio is byte-identical."""
    cands = candidates(conn, artist)
    n_files = sum(len(c) for c in cands)
    log(f"{len(cands)} candidate group(s), {n_files} file(s) to decode")
    found, cache = [], {}
    for group in cands:
        by_hash = collections.defaultdict(list)
        for f in group:
            if not os.path.exists(f["path"]):
                continue
            h = cache.get(f["path"]) or audio_hash(f["path"])
            cache[f["path"]] = h
            if h:
                by_hash[h].append(f["path"])
        for h, paths in by_hash.items():
            if len(paths) > 1:
                found.append((h, paths))
    return found


def sweep(conn, dry_run=True, artist=None, drop_wants=False, log=log):
    """Quarantine every redundant copy. Returns a summary dict."""
    found = find(conn, artist, log)
    if not found:
        log("no two files in the library share the same decoded audio")
        return {"groups": 0, "quarantined": 0, "repointed": 0, "dropped": 0}
    redundant = sum(len(p) - 1 for _, p in found)
    log(f"\n{len(found)} group(s) of identical audio, {redundant} redundant file(s):")
    quarantined = repointed = dropped = 0
    for _, paths in found:
        paths = sorted(paths, key=lambda p: (_rank(p), _plainness(p), p))
        keep, losers = paths[0], paths[1:]
        log(f"  keep {os.path.relpath(keep, LIBRARY)}")
        keeper_want = conn.execute("SELECT id FROM wants WHERE file_path=?", (keep,)).fetchone()
        for lp in losers:
            w = conn.execute("SELECT id, title FROM wants WHERE file_path=?", (lp,)).fetchone()
            log(f"      {'would quarantine' if dry_run else 'quarantining'} "
                f"{os.path.relpath(lp, LIBRARY)}")
            if dry_run:
                continue
            dest = os.path.join(QUARANTINE, os.path.relpath(lp, LIBRARY))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            # Atomic claim, not check-then-move: the quarantine path is deterministic and a
            # previous run may already hold that name.
            final = worker.place(lp, dest)
            conn.execute("DELETE FROM files WHERE path=?", (lp,))
            db.log_event(conn, "quarantined", lp,
                         f"identical decoded audio to {keep} -> {final}", commit=False)
            quarantined += 1
            if w and drop_wants and keeper_want and w["id"] != keeper_want["id"]:
                # The version this want names does not exist separately anywhere buskarr can
                # reach, so leaving the row would re-fetch the same master forever.
                conn.execute("DELETE FROM wants WHERE id=?", (w["id"],))
                dropped += 1
            elif w:
                conn.execute("UPDATE wants SET file_path=?, note=? WHERE id=?",
                             (keep, "duplicate audio quarantined; sharing the kept copy",
                              w["id"]))
                repointed += 1
            conn.commit()
    if not dry_run:
        db.log_event(conn, "dupes", artist,
                     f"{quarantined} file(s) quarantined as identical audio")
    log(f"\n{len(found)} group(s), {quarantined if not dry_run else redundant} file(s) "
        f"{'would be quarantined' if dry_run else 'quarantined'}"
        + (f", {repointed} want(s) re-pointed, {dropped} dropped" if not dry_run else ""))
    if dry_run:
        log("dry run — nothing moved. Re-run with --apply.")
    else:
        log(f"originals are under {QUARANTINE} — restore any with mv. Rescan now.")
    return {"groups": len(found), "quarantined": quarantined, "repointed": repointed,
            "dropped": dropped}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="Quarantine files whose decoded audio duplicates another copy.")
    ap.add_argument("--apply", action="store_true", help="actually move (default is dry-run)")
    ap.add_argument("--artist", default=None, help="limit to one lead artist")
    ap.add_argument("--drop-wants", action="store_true",
                    help="delete the want rows behind quarantined copies instead of re-pointing "
                         "them at the kept file; use when the version they name does not exist")
    a = ap.parse_args()
    sweep(db.init(), dry_run=not a.apply, artist=a.artist, drop_wants=a.drop_wants)
