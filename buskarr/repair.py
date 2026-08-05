"""Tag repair — make the embedded tags agree with the truth.

Large parts of this library came from rips where every file in an album carries one identical bogus
title, and sometimes a wrong artist. That is not cosmetic: Jellyfin reads the same tags, dedup
groups on them, and buskarr's own artist list fragments because of them (``Garfunkel And Oates``
with one track sitting beside ``Garfunkel and Oates`` with fifty).

The filenames are the reliable source. The library convention is
``<Artist>/<Album> (<Year>)/NN - Title.ext`` and it holds, so a disagreement between filename and
tag is almost always the tag being wrong.

Repair is gated, never blanket. A tag is only rewritten when there is positive evidence it is
wrong:

  duplicate-in-folder  several files in one album share an identical tag title — impossible for
                       distinct songs, so every tag in that folder is untrustworthy
  empty                the tag is missing entirely
  artist-mismatch      tag artist differs from the folder artist, the folder artist is used
                       consistently elsewhere in the library, AND the tag is not just the full
                       collaboration credit of the folder's own lead ("A feat. B" filed under "A"
                       is placement working as designed, not a wrong tag)

A tag that merely differs in punctuation or case from the filename is left alone: that is normal,
and rewriting it would churn thousands of files for nothing.
"""
import collections
import os
import re

from mutagen.flac import FLAC
from mutagen.mp4 import MP4
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3
from mutagen.oggvorbis import OggVorbis
from mutagen.oggopus import OggOpus

from . import credit, db, worker

REASON_DUPLICATE = "duplicate tag title within album"
REASON_EMPTY = "tag title empty"
REASON_ARTIST = "tag artist disagrees with folder"
REASON_ALBUM_ARTIST = "album artist missing or spelled differently"
LIBRARY = os.environ.get("LIBRARY_DIR", "/music")

_LEADING_ARTICLE = re.compile(r"^(?:the|a|an)\s+")


def album_artist_fix(existing, folder_artist, artist_dirs):
    """The spelling to write into the albumartist tag, or None to leave it alone.

    Jellyfin groups albums on this tag, not on the directory, so a tag that is missing or merely
    spelled differently from its own folder splits one act into several there while the library
    looks perfectly tidy on disk. Buskarr writes it correctly for everything it downloads
    (``worker.tag``); files it merely *adopted* never had it set at all.

    Gated on positive evidence that the tag names the artist whose folder the file is in. A tag
    naming somebody else is a misfiled album, and quietly overwriting it with the folder name
    would destroy the only remaining evidence of that.
    """
    existing = (existing or "").strip()
    if existing == folder_artist:
        return None
    if not existing:
        return folder_artist
    folder_key = credit._loose(folder_artist)
    if not folder_key:
        return None
    if credit._loose(existing) == folder_key:
        return folder_artist                                   # punctuation or case only
    if credit._loose(credit.lead_artist(existing)) == folder_key:
        return folder_artist                                   # "A feat. B" filed under A
    if credit.credited_to(existing, folder_artist):
        return folder_artist                                   # "A & B" filed under A
    # A leading article is the last difference accepted, and only when the tag is not itself an
    # artist directory here: "Arrogant Worms" inside "The Arrogant Worms" is one act, while
    # "Moon Hooch" inside "Jonathan Coulton" is a misfiled album that must stay visible.
    if existing in artist_dirs:
        return None
    if (_LEADING_ARTICLE.sub("", credit._loose(existing))
            == _LEADING_ARTICLE.sub("", folder_key)):
        return folder_artist
    return None


def _open(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".flac":
        return FLAC(path), "vorbis"
    if ext in (".m4a", ".mp4", ".aac", ".alac"):
        return MP4(path), "mp4"
    if ext == ".mp3":
        try:
            return EasyID3(path), "id3"
        except Exception:
            # Some rips have no ID3 header at all. Create and SAVE one before reopening —
            # add_tags() alone leaves nothing on disk, so the reopen fails identically.
            audio = MP3(path)
            audio.add_tags()
            audio.save()
            return EasyID3(path), "id3"
    if ext == ".opus":
        # Opus is Ogg-framed but is NOT Vorbis: OggVorbis rejects it with "no appropriate stream
        # found", so every .opus file in the library was silently unrepairable. Same Vorbis-comment
        # key space once opened, hence the shared kind.
        return OggOpus(path), "vorbis"
    if ext == ".ogg":
        return OggVorbis(path), "vorbis"
    return None, None


def write_tags(path, title=None, artist=None, album=None, year=None, track=None,
               album_artist=None):
    """Write the given fields, leaving everything else untouched. Returns True on success.

    Never raises: one malformed file among thousands must not abort a repair run.
    """
    try:
        handle, kind = _open(path)
    except Exception:
        return False
    if handle is None:
        return False
    try:
        if kind == "mp4":
            if title:
                handle["\xa9nam"] = [title]
            if artist:
                handle["\xa9ART"] = [artist]
            if album_artist:
                handle["aART"] = [album_artist]
            if album:
                handle["\xa9alb"] = [album]
            if year:
                handle["\xa9day"] = [str(year)]
            if track:
                handle["trkn"] = [(int(track), 0)]
        else:
            if title:
                handle["title"] = [title]
            if artist:
                handle["artist"] = [artist]
            if album_artist:
                handle["albumartist"] = [album_artist]
            if album:
                handle["album"] = [album]
            if year:
                handle["date"] = [str(year)]
            if track:
                handle["tracknumber"] = [str(track)]
        handle.save()
        return True
    except Exception:
        return False


def find_suspect(conn):
    """Return [(row, reason, proposed)] for files whose tags are demonstrably wrong."""
    rows = conn.execute("SELECT * FROM files WHERE codec != 'UNREADABLE'").fetchall()

    # Folders where one tag title repeats across distinct files: the tags there are unusable.
    per_folder = collections.defaultdict(list)
    for r in rows:
        per_folder[os.path.dirname(r["path"])].append(r)
    poisoned = set()
    for folder, items in per_folder.items():
        counts = collections.Counter((r["tag_title"] or "").strip().lower()
                                     for r in items if (r["tag_title"] or "").strip())
        bad = {t for t, n in counts.items() if n > 1}
        if bad:
            for r in items:
                if (r["tag_title"] or "").strip().lower() in bad:
                    poisoned.add(r["path"])

    # Folder artist used consistently across the library, for the artist-mismatch check.
    artist_folders = collections.Counter()
    for r in rows:
        artist_folders[r["artist"]] += 1

    # Every artist directory in the library, so the albumartist rule can tell a spelling of this
    # folder from the name of a genuinely different act sitting in it.
    artist_dirs = set()
    for r in rows:
        rel = os.path.relpath(r["path"], LIBRARY)
        parts = rel.split(os.sep)
        if len(parts) > 1 and not rel.startswith(".."):
            artist_dirs.add(parts[0])

    out = []
    for r in rows:
        file_title = (r["file_title"] or "").strip()
        tag_title = (r["tag_title"] or "").strip()
        # First component below the library root. A fixed index picked the ALBUM folder for the
        # documented /music/Artist/Album (Year)/NN - Title layout, and something else entirely for
        # any other LIBRARY_DIR depth — so --apply could rewrite artist tags to an album name.
        rel = os.path.relpath(r["path"], LIBRARY)
        parts = rel.split(os.sep)
        folder_artist = parts[0] if len(parts) > 1 and not rel.startswith("..") else None
        proposed = {}
        reason = None
        if r["path"] in poisoned and file_title and db.norm(file_title) != db.norm(tag_title):
            reason = REASON_DUPLICATE
            proposed["title"] = file_title
        elif not tag_title and file_title:
            reason = REASON_EMPTY
            proposed["title"] = file_title
        if folder_artist and r["tag_title"] is not None:
            tag_artist = (r["artist"] or "").strip()
            # credited_to guards the placement convention itself: a file the worker placed carries
            # the FULL credit in its artist tag while sitting in the LEAD's folder, so
            # "A feat. B" inside "A" is correct — and rewriting it to "A" would strip the featuring
            # credit from every collaboration in the library, then break want reconciliation, which
            # keys on that exact credit string.
            # Second exemption: the directory name has been through worker.safe(), the tag has
            # not, so credited_to can compare "AC/DC feat. B" against the folder "AC_DC" and see
            # a stranger. Running the tag's own lead through the same sanitiser asks the placement
            # convention's actual question — would THIS credit have produced THIS folder?
            if (db.norm(tag_artist) != db.norm(folder_artist)
                    and not credit.credited_to(tag_artist, folder_artist)
                    and worker.safe(credit.lead_artist(tag_artist)) != folder_artist
                    and artist_folders.get(folder_artist, 0) >= 5):
                reason = reason or REASON_ARTIST
                proposed["artist"] = folder_artist
        if folder_artist:
            fixed = album_artist_fix(r["album_artist"], folder_artist, artist_dirs)
            if fixed is not None:
                reason = reason or REASON_ALBUM_ARTIST
                proposed["album_artist"] = fixed
        if reason and proposed:
            out.append((r, reason, proposed))
    return out


def repair(conn, dry_run=True, limit=0, log=print, fields=None):
    """Repair suspect tags. ``fields`` restricts which tags may be written.

    The rules differ enormously in how sure they are. Filling an empty albumartist is safe; the
    artist-mismatch rule proposes rewriting a performer's name to the folder's, which on a
    compilation means crediting somebody else's track to the folder artist. Being able to run one
    rule without the other is what lets the safe ones be applied at all.
    """
    suspects = find_suspect(conn)
    if fields is not None:
        allowed = set(fields)
        suspects = [(r, reason, {k: v for k, v in proposed.items() if k in allowed})
                    for r, reason, proposed in suspects]
        suspects = [s for s in suspects if s[2]]
    log(f"{len(suspects)} file(s) with demonstrably wrong tags")
    by_reason = collections.Counter(reason for _, reason, _ in suspects)
    for reason, n in by_reason.most_common():
        log(f"  {n:>5}  {reason}")

    done = 0
    for r, reason, proposed in suspects:
        if limit and done >= limit:
            log(f"  limit {limit} reached")
            break
        shown = ", ".join(f"{k}={v!r}" for k, v in proposed.items())
        log(f"  {'would fix' if dry_run else 'fixing'} {os.path.basename(r['path'])[:52]}"
            f"  [{reason}]  -> {shown}")
        if not dry_run:
            if write_tags(r["path"], **proposed):
                done += 1
                # Reflect the change immediately so a later dedup sees repaired data.
                if "title" in proposed:
                    conn.execute("UPDATE files SET title=?, tag_title=?, norm_title=? "
                                 "WHERE path=?",
                                 (proposed["title"], proposed["title"],
                                  db.norm(proposed["title"]), r["path"]))
                if "artist" in proposed:
                    conn.execute("UPDATE files SET artist=?, norm_artist=? WHERE path=?",
                                 (proposed["artist"], db.norm(proposed["artist"]), r["path"]))
                if "album_artist" in proposed:
                    conn.execute("UPDATE files SET album_artist=? WHERE path=?",
                                 (proposed["album_artist"], r["path"]))
                conn.commit()
            else:
                log("      write failed; left untouched")
        else:
            done += 1
    if not dry_run:
        db.log_event(conn, "tag-repair", None, f"{done} file(s) repaired")
    log(f"done: {done} file(s) {'would be repaired' if dry_run else 'repaired'}")
    return {"suspect": len(suspects), "repaired": done}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Repair demonstrably wrong audio tags.")
    ap.add_argument("--apply", action="store_true", help="write tags (default is dry-run)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--fields", default=None,
                    help="comma-separated tags to write, e.g. album_artist,title "
                         "(default: every tag the rules propose)")
    a = ap.parse_args()
    repair(db.init(), dry_run=not a.apply, limit=a.limit,
           fields=[f.strip() for f in a.fields.split(",")] if a.fields else None)
