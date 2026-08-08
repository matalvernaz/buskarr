"""Library scanner: build buskarr's picture of what is actually on disk.

Reads embedded tags where present and falls back to the path convention
``<Artist>/<Album>/NN - Title.ext``, because a real library always contains files whose tags are
missing or wrong. ffprobe supplies the quality facts that make upgrade decisions possible.

Deliberately never writes to the library — scanning is read-only.
"""
import json
import os
import re
import subprocess
import time

from . import db, match

AUDIO_EXT = (".flac", ".m4a", ".mp3", ".opus", ".ogg", ".wav", ".alac", ".aac")
# Directories buskarr maintains itself, or that hold copies rather than library content.
# Read from the same variable dedupe writes to, so the two cannot drift apart: a quarantine
# directory the scanner does not skip gets re-indexed as library content on the next scan.
QUARANTINE_DIR = os.environ.get("QUARANTINE_DIR", "/music/.buskarr_quarantine")
# Same variable buskarr.upgrade moves originals into — the two sides must read one name, or a
# renamed holding directory gets re-indexed as library content on the next scan.
UPGRADE_DIR = os.environ.get("UPGRADE_DIR", "/music/.upgrade_originals")
SKIP_DIRS = {".dedup_quarantine", ".lidarr-recycle", ".buskarr_staging",
             os.path.basename(QUARANTINE_DIR.rstrip("/")),
             os.path.basename(UPGRADE_DIR.rstrip("/"))}
TRACK_PREFIX = re.compile(r"^\s*(\d{1,3})\s*[-._]\s*")
PROBE_TIMEOUT = 60


def probe(path):
    """Return codec/bitrate/duration facts for one file, or None if unreadable.

    Bounded: this runs on freshly downloaded media, and a malformed file with no timeout would hang
    the acquisition worker indefinitely.
    """
    try:
        return _probe(path)
    except subprocess.TimeoutExpired:
        return None


def _probe(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=codec_name,sample_rate,bits_per_raw_sample,channels:"
         # Both albumartist spellings: Vorbis/FLAC write ALBUMARTIST, MP4 and ID3 come through
         # as album_artist. Asking for only one silently reads empty on half the library.
         "format=bit_rate,duration:"
         "format_tags=artist,album,title,date,track,album_artist,albumartist",
         "-of", "json", path],
        capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    if r.returncode != 0:
        return None
    try:
        d = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    st = (d.get("streams") or [{}])[0]
    fm = d.get("format") or {}
    tags = {k.lower(): v for k, v in (fm.get("tags") or {}).items()}

    def as_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    return {
        "codec": st.get("codec_name"),
        "sample_rate": as_int(st.get("sample_rate")),
        "bit_depth": as_int(st.get("bits_per_raw_sample")),
        "bitrate": as_int(fm.get("bit_rate")) or 0,
        "duration": float(fm.get("duration") or 0) or None,
        "tag_artist": tags.get("artist"),
        "tag_album_artist": tags.get("album_artist") or tags.get("albumartist"),
        "tag_album": tags.get("album"),
        "tag_title": tags.get("title"),
        "tag_year": (tags.get("date") or "")[:4] or None,
        "tag_track": as_int((tags.get("track") or "").split("/")[0]),
    }


def from_path(root, path):
    """Derive artist/album/title/track from the library path convention."""
    rel = os.path.relpath(path, root)
    parts = rel.split(os.sep)
    artist = parts[0] if parts else "Unknown Artist"
    album = parts[1] if len(parts) > 2 else None
    stem = os.path.splitext(os.path.basename(path))[0]
    track_no = None
    m = TRACK_PREFIX.match(stem)
    if m:
        track_no = int(m.group(1))
        stem = TRACK_PREFIX.sub("", stem)
    # Album folders are commonly "Title (1999)" — keep the year, drop it from the album name.
    year = None
    if album:
        ym = re.search(r"\((\d{4})\)\s*$", album)
        if ym:
            year = ym.group(1)
            album = album[:ym.start()].strip()
    return artist, album, stem.strip(), track_no, year


def file_row(root, path, stat, info):
    """Build the ``files`` row for one library file. ``info`` is ``probe()``'s result, or None.

    Shared by the walk and by ``index_file`` so a track recorded the moment it is placed is
    identical to the same track recorded by a full scan. Two constructions of this row would
    diverge, and the divergence would be invisible until something queried the difference.
    """
    p_artist, p_album, p_title, p_track, p_year = from_path(root, path)
    if not info:
        return {
            "path": path, "artist": p_artist, "artist_lead": p_artist,
            "album": p_album, "title": p_title,
            "file_title": p_title, "tag_title": None,
            "track_no": p_track, "year": p_year, "codec": "UNREADABLE", "bitrate": 0,
            "sample_rate": None, "bit_depth": None, "duration": None,
            "size": stat.st_size, "provider": None, "mtime": stat.st_mtime,
        }
    return {
        "path": path,
        # Tags win when present; the path convention is the fallback, not the reverse,
        # because tags survive renames and path parsing does not.
        "artist": info["tag_artist"] or p_artist,
        # The directory, always — it is the lead credit by construction, while the tag holds
        # the full collaboration credit. Grouping the library on the tag is what showed one
        # band as eight artists.
        "artist_lead": p_artist,
        # Recorded raw, never defaulted to the directory: an empty tag is the defect
        # repair looks for, and filling it in here would hide it.
        "album_artist": info["tag_album_artist"],
        "album": info["tag_album"] or p_album,
        # Resolved title still prefers the tag, but both are recorded so callers can
        # choose: dedup needs the filename, repair needs the disagreement.
        "title": info["tag_title"] or p_title,
        "file_title": p_title,
        "tag_title": info["tag_title"],
        "track_no": info["tag_track"] or p_track,
        "year": info["tag_year"] or p_year,
        "codec": info["codec"], "bitrate": info["bitrate"],
        "sample_rate": info["sample_rate"], "bit_depth": info["bit_depth"],
        "duration": info["duration"], "size": stat.st_size,
        "provider": None, "mtime": stat.st_mtime,
    }


def index_file(conn, root, path):
    """Record one file in ``files`` without walking the library. Returns False if it is gone.

    The worker calls this the moment it places a download. Without it ``files`` is written only by
    a full scan, so a freshly acquired track — and its whole artist, if it is the first one — is
    absent from the Library page and from every query built on that table until somebody presses
    Rescan by hand.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return False
    db.upsert_file(conn, file_row(root, path, stat, probe(path)))
    conn.commit()
    return True


def scan(conn, root, progress_every=250, log=print):
    """Walk the library and refresh the files table. Returns counts."""
    seen, added, unreadable = 0, 0, 0
    started = time.time()
    known = {r["path"]: r["mtime"] for r in conn.execute("SELECT path, mtime FROM files")}
    live = set()

    # os.walk silently SKIPS a directory it cannot list. On an NFS library a transient error over
    # one artist directory would therefore mark its every file as "gone", delete their rows, and
    # reconcile_wants would queue the lot for re-download — the partial-outage version of the
    # empty-mount disaster guarded below. Record the errors and refuse the removal step instead.
    walk_errors = []

    for dirpath, dirnames, filenames in os.walk(root, onerror=walk_errors.append):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in sorted(filenames):
            if not fn.lower().endswith(AUDIO_EXT):
                continue
            full = os.path.join(dirpath, fn)
            live.add(full)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            seen += 1
            # Unchanged files are skipped: a rescan of a settled library should be cheap.
            if known.get(full) == stat.st_mtime:
                continue
            info = probe(full)
            if not info:
                unreadable += 1
            db.upsert_file(conn, file_row(root, full, stat, info))
            # Committed per file: the next iteration's ffprobe takes seconds over NFS, and
            # sqlite3's implicit transaction would otherwise hold the ONLY write lock across the
            # entire remaining walk — the worker and the web process both time out at 30 s long
            # before a post-import scan finishes.
            conn.commit()
            added += 1
            if added % progress_every == 0:
                log(f"  scanned {added} new/changed of {seen} seen...")
    conn.commit()

    # An empty walk over a library that had files is a vanished mount, not a deletion. Proceeding
    # would drop every `files` row, and reconcile_wants would then flip every satisfied want back to
    # pending — re-downloading the whole library.
    if known and not live:
        raise RuntimeError(
            f"{root} looks entirely empty but {len(known)} file(s) are on record — refusing to "
            "scan. Check the mount before re-running.")

    gone = [p for p in known if p not in live]
    if gone and walk_errors:
        # Part of the tree was unreadable, so "not seen" does not mean "not there". New and changed
        # files above are still recorded — only the removal step needs the walk to have been whole.
        log(f"  {len(walk_errors)} director(y/ies) could not be read "
            f"({type(walk_errors[0]).__name__}); keeping the {len(gone)} file record(s) that "
            "were not seen. Fix the mount and rescan.")
        gone = []
    for p in gone:
        conn.execute("DELETE FROM files WHERE path=?", (p,))
    conn.commit()

    # A want whose file reappeared (or vanished) should reflect reality after every scan.
    reconciled = reconcile_wants(conn)
    took = time.time() - started
    log(f"scan complete: {seen} files, {added} new/changed, {len(gone)} removed, "
        f"{unreadable} unreadable, {reconciled} want(s) reconciled, {took:.0f}s")
    return {"seen": seen, "updated": added, "removed": len(gone),
            "unreadable": unreadable, "reconciled": reconciled}


def reconcile_wants(conn):
    """Mark wants satisfied when a matching file exists, and un-satisfy them when it's gone."""
    changed = 0
    for w in conn.execute("SELECT * FROM wants").fetchall():
        tol = match.acceptable_delta(w["duration"])
        hit = db.find_file(conn, w["artist"], w["title"], w["duration"],
                           tolerance=tol if tol else 4.0)
        if hit and w["status"] != db.STATUS_HAVE:
            conn.execute("UPDATE wants SET status=?, file_path=? WHERE id=?",
                         (db.STATUS_HAVE, hit["path"], w["id"]))
            changed += 1
        elif not hit and w["status"] == db.STATUS_HAVE:
            if w["file_path"] and os.path.exists(w["file_path"]):
                # Placed while this scan was already past its directory: no files row yet, but
                # the audio is on disk. Un-satisfying here queues a re-download of a file that
                # exists; the next scan indexes it and the want reconciles normally.
                continue
            conn.execute("UPDATE wants SET status=?, file_path=NULL WHERE id=?",
                         (db.STATUS_PENDING, w["id"]))
            changed += 1
        elif hit and w["file_path"] and not _on_record(conn, w["file_path"]):
            # The file moved — a credit fold, or a manual tidy. The want is still satisfied, so its
            # status must not be disturbed, but the recorded path now points at nothing.
            # Conditioned on the OLD path being gone rather than on the two paths differing, or a
            # song held in several copies would rewrite this row on every scan.
            conn.execute("UPDATE wants SET file_path=? WHERE id=?", (hit["path"], w["id"]))
            changed += 1
    conn.commit()
    return changed


def _on_record(conn, path):
    return conn.execute("SELECT 1 FROM files WHERE path=?", (path,)).fetchone() is not None
