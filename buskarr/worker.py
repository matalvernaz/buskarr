"""The acquisition loop: turn pending wants into files on disk.

For each want, providers are tried in quality order. The first candidate that clears vetting is
fetched, tagged, and placed. Failures earn an escalating backoff so material nothing carries decays
from daily retries to monthly instead of burning a slot every cycle forever.

File placement lives here and nowhere else, and it never deletes anything it did not just write.
That constraint is deliberate: the one destructive bug worth fearing in a tool like this is an
operation whose blast radius is wider than its name suggests.
"""
import os
import shutil
import threading
import time

from mutagen.flac import FLAC
from mutagen.mp4 import MP4

from . import bulk, credit, db, match, providers

LIBRARY = os.environ.get("LIBRARY_DIR", "/music")
STAGING = os.environ.get("STAGING_DIR", "/state/staging")
INTERVAL = int(os.environ.get("INTERVAL", "1800"))
PER_CYCLE = int(os.environ.get("PER_CYCLE", "25"))
BASE_COOLDOWN = int(os.environ.get("BASE_COOLDOWN", str(24 * 3600)))
MAX_COOLDOWN = int(os.environ.get("MAX_COOLDOWN", str(30 * 24 * 3600)))
# A search returning nothing is ambiguous — a throttled session looks identical to a song nobody
# has — so it earns a short retry rather than a day's exile.
EMPTY_COOLDOWN = int(os.environ.get("EMPTY_COOLDOWN", "3600"))
# The web process asks for an early cycle by touching this file rather than running one itself:
# both processes share one SQLite file, and only this loop is allowed to fetch or place anything.
NUDGE = os.environ.get("NUDGE_FILE", "/state/nudge")
NUDGE_POLL = 5
# A Soulseek enqueue that has produced nothing after this long is treated as dead — the peer went
# offline, or the queue never drained. Without an age-out, a want in SEARCHING is selected by no
# query at all and is stuck forever; harvest still imports the file if it turns up late.
SEARCH_STALE = int(os.environ.get("SEARCH_STALE", str(48 * 3600)))


def log(msg):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}", flush=True)


def backoff(strikes):
    return min(BASE_COOLDOWN * 2 ** max(0, strikes - 1), MAX_COOLDOWN)


# ext4/most filesystems cap a single component at 255 bytes; leave room for a track prefix.
MAX_COMPONENT = 180
_SANITISE = {c: None for c in range(0x20)}
_SANITISE[0x7f] = None
# The full Windows-illegal set, not just the POSIX separators. The library is also shared over
# SMB, where a component containing : ? * " < > | is unreachable — and letting ":" through is what
# produced the colon/underscore split spellings of one album directory ("Genuine: …" in the
# database, "Genuine_ …" on disk), with refile happy to ping-pong between them.
for _c in '/\\:*?"<>|':
    _SANITISE[ord(_c)] = "_"


def safe(name):
    """Make one catalogue string safe to use as a single path component.

    Replacing "/" is not sufficient. An artist or album literally named ".." produced
    ``/music/../../NN - Title.flac``, which resolves outside the library root — so a catalogue value
    could direct a write anywhere the process can reach. Control characters and unbounded lengths
    also came straight through.
    """
    name = (name or "").translate(_SANITISE).strip(" .")
    if not name or name in {".", ".."}:
        return "Unknown"
    if len(name.encode("utf-8")) > MAX_COMPONENT:
        while len(name.encode("utf-8")) > MAX_COMPONENT and name:
            name = name[:-1]
        name = name.strip() or "Unknown"
    return name


def within_library(path):
    """True only if ``path`` really resolves inside LIBRARY. Last line of defence before a write."""
    root = os.path.realpath(LIBRARY)
    target = os.path.realpath(path)
    return target == root or target.startswith(root + os.sep)


def folder_artist(want):
    """The artist directory a want belongs in: the lead credit, not the whole credit string.

    Naming the directory from the full credit gave one band eight folders — "The Longest Johns",
    "… & Lucy Humphris", "… feat. SKÁLD", "… ft. SKÁLD" — which reads as eight artists to anyone
    browsing the library. ``dict()`` because this is called with both database rows and plain dicts,
    and a row has no ``.get``.
    """
    return credit.lead_artist(want["artist"], dict(want).get("artist_lead"))


def want_track(want, fallback=None):
    """The track number to name and tag a want's file with: the want's own, else ``fallback``.

    A want created from a resolved album listing knows its position on THAT release. The acquired
    file's tag carries its position on whatever release the provider happened to serve — a
    compilation or another edition often enough that one completed album got two "05" files and no
    "04". The listing wins; the source tag stays the fallback for wants that never had a listing.
    ``dict()`` because this is called with both database rows and plain dicts, and older callers'
    dicts have no ``track_no`` key at all.
    """
    return dict(want).get("track_no") or fallback


def destination(want, ext, track=None):
    """Library path for a want: ``<Lead artist>/<Album> (<Year>)/NN - Title.ext``.

    ``track`` comes from the acquired file's own tags where the source supplies one; the want's
    own ``track_no`` overrides it (see ``want_track``). Hardcoding 01 left filenames claiming
    track 1 while the embedded tag said 14, and the scanner reads the track number from either —
    so filename and tag have to agree, which holds because ``tag`` applies the same preference.
    """
    track = want_track(want, track)
    # A directory is unavoidable; "Singles" groups album-less tracks instead of creating one
    # single-track pseudo-album per song.
    real_album = bool(want["album"])
    album = safe(want["album"]) if real_album else "Singles"
    # Sources supply bare years, full dates, and full timestamps ("1971-10-30 00:00:00"). Take the
    # leading four digits when they are a plausible year, and otherwise omit it rather than writing
    # a timestamp into a directory name.
    #
    # Only a real album is dated. "Singles" is not a release and has no year, so stamping one on it
    # produced "Singles (1994)", "Singles (2000)" and "Singles (2007)" side by side — three
    # directories for one bucket that exists precisely to hold everything that has no album.
    year = str(want["year"] or "").strip()[:4]
    if real_album and year.isdigit() and 1000 <= int(year) <= 2999:
        album = f"{album} ({year})"
    # "Singles" is not a release, so it carries no position either — for the same reason it
    # carries no year. Every song in it is track 1 of its own single, which numbered 47 files in
    # this library "01" and made the bucket look like one broken album. A track number is written
    # only where it means "position on THIS album".
    stem = safe(want["title"])
    name = f"{int(track):02d} - {stem}{ext}" if (real_album and track) else f"{stem}{ext}"
    dest = os.path.join(LIBRARY, safe(folder_artist(want)), album, name)
    if not within_library(dest):
        # safe() should make this unreachable; assert it anyway, because the cost of being wrong is
        # writing audio outside the library.
        raise ValueError(f"refusing a destination outside {LIBRARY}: {dest}")
    return dest


# How many suffixed names to try before giving up on placing a file.
PLACE_ATTEMPTS = 50


def move_no_replace(src, dst):
    """Move ``src`` to ``dst``, raising FileExistsError rather than replacing an existing file.

    ``shutil.move`` becomes ``rename(2)`` within one filesystem, and rename REPLACES silently — so
    every "check ``os.path.exists`` then move" in this project was a check-then-act race against its
    own worker, with destroyed audio as the prize. The destination name is therefore claimed
    atomically: a hard link inside one filesystem, and an ``O_CREAT|O_EXCL`` copy across one, which
    is the case that matters here because staging lives on the state volume and the library is NFS.
    """
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError:
        copy_no_replace(src, dst)
    os.unlink(src)


def copy_no_replace(src, dst):
    """Copy ``src`` to ``dst``, claiming the destination name atomically.

    Raises FileExistsError if the name is taken — never truncates what is there. The bytes are
    staged as a ``.part`` sibling and hard-linked into place only once fully written and fsynced:
    streaming straight into ``dst`` would leave a truncated file at the canonical name if the
    process died mid-copy, and a truncated file named and tagged correctly is invisible to every
    later check. This is the placement primitive for callers that must leave the source alone
    (harvest, which copies out of a seeding download tree), and the cross-device half of
    ``move_no_replace``.
    """
    tmp = f"{dst}.part-{os.getpid()}"
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    try:
        with os.fdopen(fd, "wb") as out, open(src, "rb") as fh:
            shutil.copyfileobj(fh, out)
            out.flush()
            os.fsync(out.fileno())
        shutil.copystat(src, tmp)
        os.link(tmp, dst)           # the atomic claim; FileExistsError when the name is taken
    finally:
        _discard(tmp)


def place(src, dst):
    """Move ``src`` to ``dst``, or to the next free suffixed name. Returns the path actually used.

    The suffix loop retries on a lost race rather than re-checking existence, because only the
    create itself can decide who won.
    """
    stem, ext = os.path.splitext(dst)
    for n in range(1, PLACE_ATTEMPTS + 1):
        candidate = dst if n == 1 else f"{stem} ({n}){ext}"
        try:
            move_no_replace(src, candidate)
            return candidate
        except FileExistsError:
            continue
    raise FileExistsError(f"no free name for {dst} after {PLACE_ATTEMPTS} attempts")


def _discard(path):
    """Remove a staged file that failed verification. Only ever called on our own staging area."""
    try:
        os.unlink(path)
    except OSError:
        pass


def scan_probe_duration(path):
    from . import scan
    info = scan.probe(path)
    if not info:
        return None
    return info.get("duration") or None


def source_track_no(path):
    """Track number embedded in a freshly-acquired file, if the provider supplied one."""
    from . import scan
    info = scan.probe(path)
    return (info or {}).get("tag_track")


def tag(path, want, track=None):
    """Write minimal correct tags so the scanner reads the song, not the filename.

    ``artist`` is the full credit and ``albumartist`` the lead. Both are needed and they are not
    interchangeable: the full credit is what identity, reconciliation and provider search all key on,
    while the lead is what groups a collaboration with the rest of that artist's work.

    ``track`` resolves exactly as in ``destination`` — the want's own number first — so the
    filename and the embedded tag never disagree.
    """
    track = want_track(want, track)
    ext = os.path.splitext(path)[1].lower()
    lead = folder_artist(want)
    try:
        if ext == ".flac":
            m = FLAC(path)
            m["title"], m["artist"] = [want["title"]], [want["artist"]]
            m["albumartist"] = [lead]
            # No album tag when the source had no album. Falling back to the song title wrote a
            # false album equal to the title, which Jellyfin then shows as a one-track album per
            # song. Blank is honest; wrong is not.
            if want["album"]:
                m["album"] = [want["album"]]
            if want["year"]:
                m["date"] = [str(want["year"])]
            m["tracknumber"] = [str(int(track or 1))]
        elif ext in (".m4a", ".mp4"):
            m = MP4(path)
            m["\xa9nam"], m["\xa9ART"] = [want["title"]], [want["artist"]]
            m["aART"] = [lead]
            if want["album"]:
                m["\xa9alb"] = [want["album"]]
            if want["year"]:
                m["\xa9day"] = [str(want["year"])]
            m["trkn"] = [(int(track or 1), 0)]
        else:
            return False
        m.save()
        return True
    except Exception as e:                      # tagging must never lose an acquired file
        log(f"    tagging failed ({type(e).__name__}); keeping the file untagged")
        return False


def attempt(conn, want, provs):
    """Try every provider for one want. Returns 'have', 'unavailable' or 'empty'."""
    saw_any_candidate = False
    for entry in provs:
        p = entry["provider"]
        try:
            candidates = p.search(dict(want))
        except providers.RateLimited:
            raise
        except Exception as e:
            log(f"    {p.name}: search error {type(e).__name__}")
            continue
        if not candidates:
            continue
        for cand in candidates:
            saw_any_candidate = True
            ok, why = match.vet(dict(want), cand)
            if not ok:
                log(f"    {p.name}: rejected — {why}")
                continue
            log(f"    {p.name}: accepted '{cand['title'][:50]}' "
                f"({cand.get('duration')}s, {cand.get('codec')})")
            try:
                staged = p.fetch(cand, os.path.join(STAGING, p.name))
            except providers.RateLimited:
                raise
            except Exception as e:
                # Contained like search errors are. An uncaught fetch failure — a downloader hitting
                # its subprocess timeout, say — otherwise aborts the whole cycle, and every want
                # behind this one waits out INTERVAL for a problem that was one candidate's.
                log(f"    {p.name}: fetch error {type(e).__name__}")
                continue
            if staged == "QUEUED":
                # Soulseek is asynchronous: leave the want pending and pick the file up later.
                # last_attempt is what the stale-queue reaper in cycle() ages against.
                conn.execute("UPDATE wants SET status=?, provider=?, note=?, last_attempt=? "
                             "WHERE id=?",
                             (db.STATUS_SEARCHING, p.name, "queued at peer", time.time(),
                              want["id"]))
                conn.commit()
                db.log_event(conn, "queued", f"{want['artist']} - {want['title']}", p.name)
                return "queued"
            if not staged:
                continue
            ext = os.path.splitext(staged)[1].lower()
            track_no = source_track_no(staged)
            dest = destination(want, ext, track_no)
            if os.path.exists(dest) and not want["allow_dup"]:
                # Usually a previous cycle that placed the file and then died before writing the
                # want; recording it stops this want being re-fetched every cycle forever while the
                # audio sits there unclaimed.
                #
                # But the path is no longer unique per want. Since the artist directory became the
                # LEAD credit and "Singles" stopped carrying a year, "A feat. B – Song" and
                # "A feat. C – Song" are two legal wants that both resolve to
                # A/Singles/01 - Song.flac — so claiming whatever is there would attach this want to
                # a different recording. Claim it only when the audio itself agrees, and require
                # both lengths to be known: an unverifiable match is not a match.
                # Duration alone is not identity: two same-titled recordings can run the same
                # length, which is exactly the "A feat. B" / "A feat. C" collision this branch was
                # widened by. The file's own tags have to agree as well, and missing tags mean
                # "place alongside" rather than "claim".
                from . import scan as _scan
                info = _scan.probe(dest) or {}
                held = info.get("duration")
                # credit._loose, not db.norm: db.norm strips to [a-z0-9], so every non-Latin credit
                # collapses to the same string — "A feat. 坂本龍一" and "A feat. 李某" both become
                # "a feat" and would claim each other's audio. This project already learned that
                # lesson once; _loose exists for exactly it.
                tags_agree = (credit._loose(info.get("tag_artist") or "")
                              == credit._loose(want["artist"])
                              and db.strict_norm(info.get("tag_title") or "")
                              == db.strict_norm(want["title"]))
                if want["duration"] and held and tags_agree \
                        and match.duration_ok(want["duration"], held):
                    log(f"    destination already holds this recording; recording it: {dest}")
                    _discard(staged)
                    conn.execute(
                        "UPDATE wants SET status=?, provider=?, file_path=?, strikes=0, "
                        "retry_after=NULL, note=? WHERE id=?",
                        (db.STATUS_HAVE, p.name, dest, "file already at destination", want["id"]))
                    conn.commit()
                    return "have"
                log(f"    {dest} is taken by a different recording "
                    f"({held}s against {want['duration']}s wanted); placing this one alongside it")
            # Re-check for an existing copy at placement time: the library may have gained one
            # since this want was added (another want, a harvest, a manual copy). Only an explicit
            # allow_dup gets past this.
            if not want["allow_dup"]:
                twin = db.find_exact(conn, want["artist"], want["title"], want["duration"])
                if twin:
                    log(f"    already have this recording at {twin['path']}; not adding a copy")
                    _discard(staged)        # or it sits in staging until the next fetch clears it
                    conn.execute("UPDATE wants SET status=?, file_path=?, note=? WHERE id=?",
                                 (db.STATUS_HAVE, twin["path"],
                                  "already on disk at placement time", want["id"]))
                    conn.commit()
                    return "have"
            # Vet the file that actually ARRIVED, not the search result that promised it. A
            # downloader can exit non-zero, truncate, or hand back a different recording; up to here
            # every check has been against provider metadata. This is the last point at which a
            # wrong or partial file can still be refused, because once placed it is named and tagged
            # from the want and therefore looks correct forever.
            probed = scan_probe_duration(staged)
            if probed is None:
                log("    staged file is unreadable; discarding it")
                _discard(staged)
                continue
            if not match.duration_ok(want["duration"], probed):
                log(f"    staged file runs {probed:.0f}s but {want['duration']}s was wanted; "
                    "discarding it")
                _discard(staged)
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            final = place(staged, dest)
            tag(final, want, track_no)
            conn.execute(
                "UPDATE wants SET status=?, provider=?, file_path=?, strikes=0, retry_after=NULL,"
                " note=? WHERE id=?",
                (db.STATUS_HAVE, p.name, final, f"{cand.get('codec')} via {p.name}", want["id"]))
            conn.commit()
            db.log_event(conn, "acquired", f"{want['artist']} - {want['title']}",
                         f"{p.name} {cand.get('codec')}")
            log(f"    placed {final}")
            return "have"
    return "unavailable" if saw_any_candidate else "empty"


def job_summary(kind, r):
    """One line describing what an add did, for the UI to show when the job finishes."""
    if kind == "rescan":
        return (f"{r['seen']} file(s) seen, {r['updated']} new or changed, {r['removed']} removed, "
                f"{r['unreadable']} unreadable, {r['reconciled']} want(s) reconciled")
    if kind == "complete":
        outcome = r.get("outcome")
        if outcome == "missing":
            return "that wanted song no longer exists"
        if outcome == "single":
            return f"“{r['resolved']}” is a single — there is nothing further to queue"
        if outcome == "none":
            out = "no album containing this song was found on Deezer or Apple"
            if r.get("errors"):
                out += f" ({r['errors'][0]})"
            return out
        out = (f"resolved to “{r['album']}”"
               + (f" ({r['year']})" if r.get("year") else "")
               + f" via {r['source']}: ")
        if outcome == "already":
            out += (f"every track is already in the library or wanted "
                    f"({r['already']} held, {r.get('existing', 0)} wanted)")
        else:
            out += (f"{r['added']} queued of {r['total']} remaining track(s); {r['already']} "
                    f"already in the library; {r.get('existing', 0)} already wanted")
        if r.get("edition_differs"):
            out += (f" — note this is the edition “{r['resolved']}”, a different release from the "
                    "album named on the song, so it files as its own directory")
        if r.get("enriched"):
            out += (f"; {r['enriched']} song(s) gained album details — run refile to move any "
                    "already-placed file(s)")
        return out
    if kind == "album":
        return (f"{r['added']} queued of {r['total']} track(s); {r['already']} already in the "
                f"library; {r.get('existing', 0)} already wanted")
    out = (f"{r['added']} queued across {r['releases']} release(s); {r['already']} already in the "
           f"library; {r.get('existing', 0)} already wanted; {r['skipped_repackaging']} "
           f"compilation(s) skipped; {r['other_artist']} track(s) by other artists skipped")
    if r.get("enriched"):
        out += (f"; {r['enriched']} existing want(s) gained an album — run refile to move their "
                "files out of Singles")
    if not r.get("complete", True):
        out += " — WARNING: the catalogue was truncated, so this is NOT the whole discography"
    return out


def run_jobs(conn):
    """Perform any queued bulk adds. Returns how many ran.

    These used to happen inside the POST, which meant 6 s (MusicBrainz, rate limited) to 15 s
    (Deezer, one request per release) of blank browser before the redirect. The work is unchanged; it
    just belongs to whichever process is allowed to spend that long, which is this one.
    """
    jobs = db.claim_jobs(conn)
    for j in jobs:
        log(f"job {j['id']}: {j['kind']} {j['label'] or j['ref']} from {j['source']}")
        try:
            if j["kind"] == "album":
                r = bulk.add_album(conn, j["ref"], j["requested_by"], source=j["source"])
            elif j["kind"] == "complete":
                r = bulk.complete_album(conn, int(j["ref"]), j["requested_by"])
            elif j["kind"] == "rescan":
                # Off the request path for the same reason adds are: a post-import scan probes
                # hundreds of files over NFS, which is minutes — far past any proxy timeout.
                from . import scan
                r = scan.scan(conn, LIBRARY, log=log)
            elif j["kind"] == "artist":
                r = bulk.add_artist(conn, j["ref"], j["requested_by"], source=j["source"])
            else:
                # Refused, never guessed at. The old fallthrough ran add_artist, so any future
                # kind — or a job left queued across a version change — became a discography add
                # for whatever artist the catalogue matched to the ref.
                raise ValueError(f"unknown job kind {j['kind']!r}")
        except Exception as e:
            log(f"  job {j['id']} failed: {type(e).__name__}: {e}")
            db.finish_job(conn, j["id"], db.JOB_ERROR, f"{type(e).__name__}: {e}")
            db.log_event(conn, "add-failed", j["label"] or j["ref"], f"{type(e).__name__}: {e}")
            continue
        detail = job_summary(j["kind"], r)
        log(f"  job {j['id']} done: {detail}")
        db.finish_job(conn, j["id"], db.JOB_DONE, detail, r.get("batch"))
        if r.get("added"):
            # The web route's nudge races this job: the acquisition loop can consume it, select
            # its wants, and go back to sleep before these rows exist — leaving them idle for a
            # whole INTERVAL. Re-nudging after the add closes that window.
            try:
                open(NUDGE, "w").close()
            except OSError:
                pass
    return len(jobs)


JOB_POLL = 2
_job_thread = None


def job_loop():
    """Run queued adds on their own thread, with their own connection.

    An add must not queue behind downloads. Running jobs at the top of a cycle meant waiting out the
    whole cycle — measured at 65.6 s — and moving the call between wants only shortened it to one
    want's provider attempts, because a single want can take a minute on its own. Adds are pure
    catalogue work and share nothing with acquisition, so they get a thread.

    Its own ``db.connect``: sqlite3 connections are single-thread by default, and the fix for that is
    a second connection, never ``check_same_thread=False`` — that would put two threads on one
    connection, which is the genuinely unsafe case.
    """
    conn = db.init()
    while True:
        try:
            if not run_jobs(conn):
                time.sleep(JOB_POLL)
        except Exception as e:
            log(f"job loop error: {type(e).__name__}: {e}")
            time.sleep(JOB_POLL)


def cycle(conn):
    # Backstop only, and now actually conditional. Calling it unconditionally made this a SECOND
    # active runner: with more queued jobs than one claim batch, the thread and this loop both ran
    # adds and their BEGIN IMMEDIATE write phases contended, stalling the web process's enqueue.
    # claim_jobs is atomic so nothing ever double-ran, but the contention was real and the comment
    # described behaviour the code did not have.
    if _job_thread is None or not _job_thread.is_alive():
        run_jobs(conn)
    provs = [e for e in providers.enabled() if e["available"]]
    if not provs:
        log("no providers available — nothing to do")
        return 0
    log(f"providers: {', '.join(e['name'] for e in provs)}")
    for e in provs:
        if e["name"] == "tidal":
            try:
                e["provider"].refresh()
            except Exception as ex:
                # A hung tiddl must not cost the whole cycle; searches then fail loudly per want.
                log(f"tidal: refresh failed ({type(ex).__name__}); continuing with the old token")

    now = time.time()
    # Age out Soulseek enqueues that never delivered, or the want is stuck in SEARCHING forever —
    # no query selects that status. Re-pending re-runs the whole provider ladder, which may re-queue
    # at a different peer; harvest remains the catcher if the original transfer lands after all.
    stale = conn.execute(
        "UPDATE wants SET status=?, note=? WHERE status=? "
        "AND COALESCE(last_attempt, requested_at, 0) < ?",
        (db.STATUS_PENDING, "peer queue timed out; searching again",
         db.STATUS_SEARCHING, now - SEARCH_STALE)).rowcount
    # Unconditional: sqlite3 opens the implicit transaction on the UPDATE even when it matches
    # nothing, and an uncommitted one here is a write lock held across the whole 30-minute sleep —
    # every other connection then times out with "database is locked".
    conn.commit()
    if stale:
        log(f"{stale} queued-at-peer want(s) timed out; back to pending")
    rows = conn.execute(
        "SELECT * FROM wants WHERE status IN (?,?) AND (retry_after IS NULL OR retry_after<?) "
        "ORDER BY requested_at LIMIT ?",
        (db.STATUS_PENDING, db.STATUS_FAILED, now, PER_CYCLE)).fetchall()
    if not rows:
        log("nothing pending")
        return 0
    log(f"{len(rows)} want(s) this cycle (cap {PER_CYCLE})")

    got = 0
    for w in rows:
        log(f"  {w['artist']} - {w['title']}")
        try:
            outcome = attempt(conn, w, provs)
        except providers.RateLimited as e:
            log(f"!! throttled ({e}) — ending cycle early, no strike recorded")
            db.log_event(conn, "throttled", None, str(e))
            break
        if outcome == "have":
            got += 1
        elif outcome == "queued":
            continue
        elif outcome == "empty":
            conn.execute("UPDATE wants SET retry_after=?, last_attempt=? WHERE id=?",
                         (now + EMPTY_COOLDOWN, now, w["id"]))
            conn.commit()
        else:
            strikes = w["strikes"] + 1
            wait = backoff(strikes)
            conn.execute(
                "UPDATE wants SET status=?, strikes=?, retry_after=?, last_attempt=?, note=? "
                "WHERE id=?",
                (db.STATUS_UNAVAILABLE, strikes, now + wait, now,
                 f"no provider had it (strike {strikes}, retry in {wait // 86400}d)", w["id"]))
            conn.commit()
            log(f"    unavailable — strike {strikes}, retry in {wait // 86400}d")
    log(f"cycle done: {got} acquired")
    return got


def wait(seconds):
    """Sleep, but return early if the web UI has asked for a cycle now."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if os.path.exists(NUDGE):
            try:
                os.unlink(NUDGE)
            except OSError:
                pass
            log("nudged — starting a cycle early")
            return
        time.sleep(min(NUDGE_POLL, max(0.1, deadline - time.time())))


def main():
    conn = db.init()
    os.makedirs(STAGING, exist_ok=True)
    # Only this process runs jobs, and it has just started — so anything still marked running was
    # interrupted by a restart. Saying so beats leaving it reading "fetching the catalogue now"
    # forever, which is what claim_jobs deliberately refuses to retry.
    stranded = conn.execute("UPDATE jobs SET status=?, finished_at=?, detail=? WHERE status=?",
                            (db.JOB_ERROR, time.time(),
                             "interrupted by a restart — add it again", db.JOB_RUNNING)).rowcount
    conn.commit()
    if stranded:
        log(f"{stranded} add(s) were interrupted by a restart and are marked failed")
    # A nudge left over from a restart would fire a cycle immediately for no reason.
    if os.path.exists(NUDGE):
        try:
            os.unlink(NUDGE)
        except OSError:
            pass
    global _job_thread
    _job_thread = threading.Thread(target=job_loop, name="jobs", daemon=True)
    _job_thread.start()
    while True:
        try:
            cycle(conn)
        except Exception as e:
            log(f"cycle error: {type(e).__name__}: {e}")
            db.log_event(conn, "error", None, f"{type(e).__name__}: {e}")
        wait(INTERVAL)


if __name__ == "__main__":
    main()
