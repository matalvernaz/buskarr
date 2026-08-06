"""SQLite store for buskarr.

Two tables carry everything:

``files``  – one row per audio file actually on disk, with the quality facts needed to decide
             whether something is worth replacing. This is what makes buskarr library-aware:
             "what do I have, and how good is it" is a query, not a guess.
``wants``  – one row per song someone wants, with its attempt history. A want is a *track*, never
             an album, because the album-as-atom assumption is precisely what makes Lidarr
             unusable for singles-heavy libraries.

Identity is (artist, title, duration) rather than any external ID. That is deliberate: keying on
MusicBrainz is what caps a library at whatever MusicBrainz happens to know, which for
novelty/web-native artists is a small fraction of reality.
"""
import os
import re
import sqlite3
import time
import unicodedata

DB_PATH = os.environ.get("BUSKARR_DB", "/state/buskarr.db")

SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS files (
    path         TEXT PRIMARY KEY,
    artist       TEXT NOT NULL,
    album        TEXT,
    title        TEXT NOT NULL,
    -- Both titles are kept deliberately. Embedded tags in this library are frequently wrong —
    -- whole albums where every file carries one bogus title — while the filenames are correct.
    -- Deduplication must group on the filename title; tag repair compares the two.
    file_title   TEXT,
    tag_title    TEXT,
    norm_artist  TEXT NOT NULL,
    norm_title   TEXT NOT NULL,
    norm_file    TEXT,
    track_no     INTEGER,
    year         TEXT,
    codec        TEXT,
    bitrate      INTEGER,
    sample_rate  INTEGER,
    bit_depth    INTEGER,
    duration     REAL,
    size         INTEGER,
    lossless     INTEGER NOT NULL DEFAULT 0,
    provider     TEXT,
    mtime        REAL,
    scanned_at   REAL
);

CREATE TABLE IF NOT EXISTS wants (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    artist        TEXT NOT NULL,
    title         TEXT NOT NULL,
    norm_artist   TEXT NOT NULL,
    norm_title    TEXT NOT NULL,
    album         TEXT,
    year          TEXT,
    duration      REAL,
    track_no      INTEGER,
    requested_by  TEXT,
    requested_at  REAL,
    status        TEXT NOT NULL DEFAULT 'pending',
    provider      TEXT,
    file_path     TEXT,
    strikes       INTEGER NOT NULL DEFAULT 0,
    last_attempt  REAL,
    retry_after   REAL,
    note          TEXT,
    UNIQUE(norm_artist, norm_title)
);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        REAL NOT NULL,
    kind      TEXT NOT NULL,
    subject   TEXT,
    detail    TEXT
);

-- Bulk adds, queued rather than performed in the request. Fetching a discography costs one HTTP
-- round trip per release plus MusicBrainz's mandatory one-per-second, which was 6 to 15 seconds of
-- blank browser before the redirect. The worker owns the work; the web process only enqueues.
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,
    ref           TEXT NOT NULL,
    source        TEXT NOT NULL,
    label         TEXT,
    requested_by  TEXT,
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL,
    status        TEXT NOT NULL DEFAULT 'queued',
    detail        TEXT,
    batch         TEXT
);

-- Album releases sent to the torrent client by buskarr.grab. The record is what stops a sweep
-- grabbing the same release again next week — on private trackers ratio is real money, so
-- "did we already grab this album" must survive restarts, not live in anyone's memory.
CREATE TABLE IF NOT EXISTS grabs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    album_key      TEXT NOT NULL,
    artist         TEXT,
    album          TEXT,
    release_title  TEXT,
    indexer_id     INTEGER,
    guid           TEXT,
    size           INTEGER,
    seeders        INTEGER,
    grabbed_at     REAL NOT NULL
);
"""

# Kept separate from the tables, and applied AFTER migrate(): files_normfile references
# norm_file, a column added after the first release, so on an existing database this index
# cannot be created until the ALTER has run.
SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS files_norm ON files(norm_artist, norm_title);
CREATE INDEX IF NOT EXISTS files_normfile ON files(norm_artist, norm_file);
CREATE INDEX IF NOT EXISTS files_artist ON files(artist);
CREATE INDEX IF NOT EXISTS wants_status ON wants(status);
CREATE INDEX IF NOT EXISTS events_at ON events(at DESC);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
"""

JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_ERROR = "error"

# Statuses a want can hold. 'unavailable' means every provider was tried and none had it — it is
# not an error, and it is kept visible rather than deleted so the answer stays discoverable.
STATUS_PENDING = "pending"
STATUS_HAVE = "have"
STATUS_SEARCHING = "searching"
STATUS_UNAVAILABLE = "unavailable"
STATUS_FAILED = "failed"

LOSSLESS_CODECS = {"flac", "alac", "pcm_s16le", "pcm_s24le", "wav"}


def norm(s):
    """Normalise a title or artist for matching.

    Drops parentheticals (``(Official Audio)``, ``(From "X" Soundtrack)``), edition noise, and all
    punctuation. Parentheticals matter more than they look: a Deezer track called
    ``Brian Song (From "Life Of Brian" Original Motion Picture Soundtrack)`` is the same song as a
    local ``Brian Song``, and treating them as different is how a library fills with duplicates.
    """
    s = unicodedata.normalize("NFKD", s or "").lower()
    s = s.replace("’", "'").replace("´", "'")
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", s)
    s = re.sub(r"\b(remaster(ed)?|explicit|album version|radio edit|single version)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def connect(path=None):
    conn = sqlite3.connect(path or DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# Columns added after the first release. CREATE TABLE IF NOT EXISTS silently does nothing to an
# existing table, so new columns need an explicit, idempotent migration.
MIGRATIONS = [
    ("files", "file_title", "TEXT"),
    ("files", "tag_title", "TEXT"),
    ("files", "norm_file", "TEXT"),
    # Set when someone deliberately wants a second copy of something already held.
    ("wants", "allow_dup", "INTEGER NOT NULL DEFAULT 0"),
    # Groups the wants created by one bulk add, so a wrong one can be undone in a single action.
    ("wants", "batch", "TEXT"),
    ("wants", "batch_label", "TEXT"),
    # The lead credit, when the source knew it. `artist` stays the FULL credit — it is the search
    # evidence and the artist tag — while this decides the library folder, so a collaboration lands
    # with the rest of that artist's work instead of in a folder of its own.
    ("wants", "artist_lead", "TEXT"),
    # The artist DIRECTORY a file sits in, which by construction is its lead credit. Stored so the
    # Library tab groups and filters on the same key the Wanted tab does — otherwise "remove this
    # artist" would mean two different sets of tracks on the two pages.
    ("files", "artist_lead", "TEXT"),
    # The albumartist TAG as it actually reads on disk, which is not the same question as
    # `artist_lead` (the directory). Jellyfin groups albums on this tag, so a file whose tag is
    # empty or spelled differently splits an artist there while looking correct here. Recorded so
    # repair can see the disagreement.
    ("files", "album_artist", "TEXT"),
    # The song's position on the release named in `album`, when a catalogue listing supplied it.
    # Without it, placement numbered files from the acquired file's own tag — the position on
    # whatever release the provider happened to serve — which gave one completed album two
    # "05" files and no "04".
    ("wants", "track_no", "INTEGER"),
]


def migrate(conn):
    """Add columns introduced after the first release. Safe to run concurrently.

    Held under BEGIN IMMEDIATE, and each column re-checked after the lock is taken. The web process
    and the worker both call ``init()`` and they start together on every deploy: without the lock
    both could read ``PRAGMA table_info``, both see a column missing, and the loser of the ALTER race
    would get "duplicate column name" — a worker that exits at startup, or a 500 on a page load.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table, column, coltype in MIGRATIONS:
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if not have:
                continue                   # table not created yet; the schema script handles it
            if column not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    backfill_artist_lead(conn)
    backfill_file_lead(conn)


def backfill_artist_lead(conn):
    """Give every want a lead artist, so grouping and filtering can be done in SQL.

    Rows added before the column existed have NULL, and a Python-side fallback would mean loading
    every want to group them. Costs one indexless scan of a few hundred rows per call once complete,
    which is cheaper than the alternative it removes.
    """
    from . import credit
    # migrate() runs BEFORE the schema script, so on a fresh database this table does not exist yet
    # and an unguarded SELECT made init() raise "no such table: wants" — every process, every start.
    # Production only survived it because the database already existed; a new /state volume, which is
    # exactly the disaster-recovery path, could not boot.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wants'").fetchone():
        return 0
    rows = conn.execute("SELECT id, artist FROM wants WHERE artist_lead IS NULL").fetchall()
    if not rows:
        return 0
    # "AND artist_lead IS NULL" repeated in the UPDATE: nothing holds a transaction across the SELECT
    # and the write, so a catalogue-supplied lead committed in between (the worker's enrich_want)
    # would otherwise be overwritten with this heuristic — which for an "&" credit is the whole
    # credit, sending refile off to undo the fold.
    conn.executemany("UPDATE wants SET artist_lead=? WHERE id=? AND artist_lead IS NULL",
                     [(folder_key(credit.lead_artist(r["artist"])), r["id"]) for r in rows])
    conn.commit()
    return len(rows)


def backfill_file_lead(conn):
    """Give every library file the artist directory it lives in. Returns how many were filled.

    The first path component under the library root IS the lead credit — that is what
    ``worker.destination`` puts there. Derived rather than parsed from tags, because the tag carries
    the full collaboration credit and grouping on that is exactly what scattered one band across
    eight artist entries.
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='files'").fetchone():
        return 0
    root = os.environ.get("LIBRARY_DIR", "/music").rstrip("/") + "/"
    rows = conn.execute("SELECT path FROM files WHERE artist_lead IS NULL").fetchall()
    if not rows:
        return 0
    updates = []
    for r in rows:
        rel = r["path"][len(root):] if r["path"].startswith(root) else r["path"].lstrip("/")
        parts = rel.split(os.sep)
        if len(parts) > 1:
            updates.append((parts[0], r["path"]))
    if updates:
        conn.executemany(
            "UPDATE files SET artist_lead=? WHERE path=? AND artist_lead IS NULL", updates)
        conn.commit()
    return len(updates)


def init(path=None):
    """Open the database and bring it fully up to date. Three steps, and the order is forced twice.

    Tables first: on a FRESH database ``migrate`` finds no tables, skips every column, and the schema
    script then creates tables without them — ``init`` returned a connection whose very first
    ``add_want`` failed with "table wants has no column named allow_dup". Indexes last: on an
    EXISTING database ``files_normfile`` references ``norm_file``, a column added after the first
    release, so the index cannot be created until the ALTER has run.
    """
    conn = connect(path)
    conn.executescript(SCHEMA_TABLES)
    migrate(conn)
    conn.executescript(SCHEMA_INDEXES)
    conn.commit()
    return conn


def log_event(conn, kind, subject=None, detail=None, commit=True):
    conn.execute("INSERT INTO events (at, kind, subject, detail) VALUES (?,?,?,?)",
                 (time.time(), kind, subject, detail))
    if commit:
        conn.commit()


def upsert_file(conn, row):
    row = dict(row)
    row["norm_artist"] = norm(row["artist"])
    row["norm_title"] = norm(row["title"])
    row["norm_file"] = norm(row.get("file_title") or row["title"])
    row["lossless"] = 1 if (row.get("codec") or "").lower() in LOSSLESS_CODECS else 0
    row["scanned_at"] = time.time()
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    updates = ", ".join(f"{k}=excluded.{k}" for k in row if k != "path")
    conn.execute(f"INSERT INTO files ({cols}) VALUES ({marks}) "
                 f"ON CONFLICT(path) DO UPDATE SET {updates}", list(row.values()))


def strict_norm(s):
    """Normalisation for duplicate detection — parentheticals PRESERVED.

    ``norm()`` strips them, which is right for matching a Deezer soundtrack suffix to a local file
    but wrong here: ``(reprise)``, ``(demo)``, ``(Live Bait)`` are different recordings, and
    treating them as the same would refuse a legitimate addition.
    """
    s = unicodedata.normalize("NFKD", s or "").lower().replace("\u2019", "'").replace("\u00b4", "'")
    return re.sub(r"[^a-z0-9()]+", " ", s).strip()


# How closely two running times must agree before an edition-noise title match is believed to be
# the same recording. A genuinely different take of one song is a different length; a reissue of
# one master is not. Tighter than match.acceptable_delta on purpose — this decides whether to
# refuse an addition, so a false positive costs the user something they asked for.
TWIN_DURATION = 4.0


def word_fold(s):
    """``strict_norm`` with parentheses dissolved, so a suffix and a parenthetical compare equal.

    ``Song (Chiptune)`` and ``Song - Chiptune`` name one recording; ``norm`` equates them only by
    throwing the word away entirely, which also equates them with plain ``Song``. Folding the
    punctuation keeps the words and drops only the bracketing.
    """
    return " ".join(strict_norm(s).replace("(", " ").replace(")", " ").split())


# Words that describe how a release was PACKAGED. Stripping them cannot change which performance
# a title names, so two titles equal after their removal are one recording relabelled.
EDITION_NOISE = re.compile(
    r"\b(?:re-?master(?:ed)?|remasters?|\d{4}|version|ver|edit|edition|deluxe|anniversary|"
    r"expanded|bonus|explicit|clean|censored|mono|stereo|album|single|radio|original|extended|"
    r"mix|reissue|digital|hd|remix(?:ed)?ing)\b")

# Words that describe a distinct PERFORMANCE. A title carrying one of these that its counterpart
# lacks is a different recording, however close the running times happen to fall. These are why
# bare ``norm`` cannot be used to detect twins: it strips every parenthetical, so it read
# "Pants Drunk (Electric Timebomb remix)" as a relabelling of "Pants Drunk", and a remix, a demo
# and a live take would all have been refused as duplicates of the studio cut.
PERFORMANCE_WORDS = frozenset(
    "live remix demo acoustic instrumental reprise karaoke cover unplugged session rehearsal "
    "acapella acappella cappella orchestral chiptune symphonic dub alternate outtake "
    "rerecorded".split())


def edition_fold(s):
    """Title reduced to the words that identify the PERFORMANCE, edition packaging removed.

    ``strict_norm`` has already turned "re-recorded" into two words, so it is rejoined first —
    otherwise "recorded" survives as an unmatched token and a re-recording looks like a
    relabelling of the original.
    """
    folded = word_fold(s).replace("re recorded", "rerecorded")
    return tuple(sorted(w for w in EDITION_NOISE.sub(" ", folded).split() if w))


def _same_recording(title_a, title_b, dur_a, dur_b, tolerance=TWIN_DURATION):
    """Do these two name one recording differing only by an edition label?

    Three conditions, and all of them are needed. The running times must be known and agree,
    because a genuinely different take is a different length. The titles must be equal once
    edition packaging is stripped. And neither may carry a performance word the other lacks —
    duration alone does not settle it, since a remix can land within a second of its original.
    """
    if not dur_a or not dur_b or abs(dur_a - dur_b) > tolerance:
        return False
    if edition_fold(title_a) != edition_fold(title_b):
        return False
    def perf(t):
        words = word_fold(t).replace("re recorded", "rerecorded").split()
        return {w for w in words if w in PERFORMANCE_WORDS}

    return perf(title_a) == perf(title_b)


def _credit_kin(artist_a, artist_b):
    """True when either credit is led by the other — "Act" against "Act & Guest"."""
    from . import credit
    return (credit.credited_to(artist_a, credit.lead_artist(artist_b))
            or credit.credited_to(artist_b, credit.lead_artist(artist_a)))


# Candidate credits for a twin lookup. Equality alone is not enough: a want credited
# "Act & Guest" normalises to "act guest", and lead_artist deliberately never splits on "&", so
# neither its norm_artist nor its artist_lead equals a listing's plain "Act". The prefix clauses
# run in both directions to catch a collaboration either side; credit._loose via _credit_kin is
# what actually decides, this only narrows the scan.
_KIN_SQL = ("norm_artist=:na OR norm_artist LIKE :na || ' %' OR :na LIKE norm_artist || ' %' "
            "OR artist_lead=:lead")


def find_want_twin(conn, artist, title, duration, exclude=None):
    """An existing want that IS this recording under a different edition label, or None.

    Artist-scoped, not release-scoped. A release-scoped check cannot see that the
    "Song (2010 Remaster)" on a greatest-hits album is the "Song" already wanted from the
    original album, so adding the compilation separately queued the same master again.
    """
    if not duration:
        return None
    rows = conn.execute(
        f"SELECT * FROM wants WHERE {_KIN_SQL}",
        {"na": norm(artist), "lead": folder_key(_lead(artist))}).fetchall()
    for r in rows:
        if exclude and r["id"] == exclude:
            continue
        if strict_norm(r["title"]) == strict_norm(title):
            continue          # the exact key already covers this; not an edition variant
        if _same_recording(title, r["title"], duration, r["duration"]) \
                and _credit_kin(artist, r["artist"]):
            return r
    return None


def find_recording(conn, artist, title, duration):
    """A held FILE that is this recording under a different edition label, or None.

    ``find_exact`` compares strict titles, so a file held as "Song" is invisible to an addition
    spelled "Song (2016 Remaster)" — which is how one master arrived on disk several times.
    """
    if not duration:
        return None
    rows = conn.execute(
        f"SELECT * FROM files WHERE {_KIN_SQL}",
        {"na": norm(artist), "lead": folder_key(_lead(artist))}).fetchall()
    for r in rows:
        for cand in (r["file_title"], r["title"]):
            if cand and strict_norm(cand) != strict_norm(title) \
                    and _same_recording(title, cand, duration, r["duration"]):
                return r
    return None


def _lead(artist):
    from . import credit
    return credit.lead_artist(artist)


def find_exact(conn, artist, title, duration=None, tolerance=4.0):
    """Find a file that is the SAME RECORDING — strict title plus agreeing duration.

    Used to decide whether an addition would be a duplicate. Deliberately stricter than
    ``find_file``: a false positive here silently refuses something the user actually wants.
    """
    want_title = strict_norm(title)
    rows = conn.execute("SELECT * FROM files WHERE norm_artist=?", (norm(artist),)).fetchall()
    for r in rows:
        # Compare against BOTH titles. Tags are censored or wrong often enough to matter — a file
        # named "Nice Motherfucking Truck" carrying the tag "Nice Motherf#&g Truck" was invisible
        # to a tag-only lookup, which would have fetched a duplicate of something already held.
        titles = {strict_norm(r["file_title"] or ""), strict_norm(r["title"] or "")}
        if want_title not in titles:
            continue
        if duration and r["duration"] and abs(r["duration"] - duration) > tolerance:
            continue        # same title, different length => different recording
        return r
    return None


def find_file(conn, artist, title, duration=None, tolerance=4.0):
    """Locate an existing file for a song.

    When a duration is given it is **enforced**, not merely preferred. It used to fall back to any
    same-titled file when nothing matched on length, which silently marked a want for a 132-second
    recording satisfied by a 79-second one — so the wanted version was never fetched and the want
    reported success.

    Callers that genuinely want *any* recording of the song omit the duration: "everything by this
    artist" means every song, not every take of every song.
    """
    nt = norm(title)
    rows = conn.execute(
        "SELECT * FROM files WHERE norm_artist=? AND (norm_title=? OR norm_file=?)",
        (norm(artist), nt, nt)).fetchall()
    # norm() strips parentheticals, so "Song (live)" and "Song" collide here. When the WANTED title
    # carries a parenthetical it is an explicit request for that specific recording, so require it
    # to survive strict comparison too; otherwise a duration-less want for the live version reports
    # satisfied against the studio take and the live version is never fetched. A want with no
    # parenthetical stays loose on purpose — it should still match "Song (Album Version)".
    if "(" in (title or ""):
        st = strict_norm(title)
        rows = [r for r in rows
                if st in {strict_norm(r["file_title"] or ""), strict_norm(r["title"] or "")}]
    if duration:
        return next((r for r in rows if r["duration"]
                     and abs(r["duration"] - duration) <= tolerance), None)
    return rows[0] if rows else None


def add_want(conn, artist, title, album=None, year=None, duration=None, requested_by=None,
             allow_dup=False, batch=None, batch_label=None, artist_lead=None, track_no=None,
             commit=True):
    """Register a wanted song. Returns (id, created).

    Unless ``allow_dup``, an addition whose recording is already on disk is recorded as already
    satisfied rather than queued, so nothing is fetched twice by accident. Passing
    ``allow_dup=True`` is how someone asks for a second copy on purpose.

    ``artist_lead`` is the lead credit where the caller knows it — "The Longest Johns" for a track
    credited "The Longest Johns & Lucy Humphris". Optional: a hand-typed want has no catalogue to ask,
    and the placement path falls back to trimming the credit itself.

    ``track_no`` is the song's position on ``album`` — the pair describe the same release, like
    ``album`` and ``year`` do. Placement prefers it to the acquired file's own tag, whose number
    is the position on whatever release the provider served.
    """
    # Wants are keyed on the STRICT title. norm() strips parentheticals, which would make
    # "Song" and "Song (live)" the same want and silently refuse the second — they are different
    # recordings and both are legitimately wantable.
    from . import credit
    # Derived here when the caller has no catalogue answer — a hand-typed want used to be inserted
    # with a NULL lead, which guaranteed the next page load turned into a write transaction to fill
    # it in. Steady state should be read-only.
    artist_lead = folder_key(artist_lead or credit.lead_artist(artist))
    na, nt = norm(artist), strict_norm(title)
    existing = conn.execute("SELECT id FROM wants WHERE norm_artist=? AND norm_title=?",
                            (na, nt)).fetchone()
    if existing:
        return existing["id"], False
    if not allow_dup:
        # The same master relabelled is not a second song. Every bulk path grew its own guard
        # against this after one provider served one recording to every edition-variant want;
        # putting it here means the manual Add form and any future caller inherit it too.
        # allow_dup remains the way to ask for a second copy deliberately.
        twin = find_want_twin(conn, artist, title, duration)
        if twin:
            return twin["id"], False
    have = None if allow_dup else (find_exact(conn, artist, title, duration)
                                   or find_recording(conn, artist, title, duration))
    cur = conn.execute(
        "INSERT INTO wants (artist,title,norm_artist,norm_title,album,year,duration,track_no,"
        "requested_by,requested_at,status,file_path,allow_dup,note,batch,batch_label,artist_lead) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (artist, title, na, nt, album, year, duration, track_no, requested_by, time.time(),
         STATUS_HAVE if have else STATUS_PENDING, have["path"] if have else None,
         1 if allow_dup else 0,
         "already on disk" if have else ("extra copy requested" if allow_dup else None),
         batch, batch_label, artist_lead))
    if commit:
        conn.commit()
    return cur.lastrowid, True


def folder_key(name):
    """The artist DIRECTORY name for an artist. Both ``artist_lead`` columns store this form.

    ``files.artist_lead`` is read off disk, so it is necessarily the sanitised spelling, while
    ``wants.artist_lead`` came straight from the catalogue: "AC/DC" grouped as ``AC/DC`` on the
    Wanted tab and ``AC_DC`` on the Library tab, and "remove this artist" then meant two different
    sets of tracks on the two pages. Storing the directory form in both is what makes them one key.
    The display name is unaffected — that comes from ``wants.artist``, the full credit.

    Imported inside the function because ``worker`` imports this module at module level; the call
    only happens long after both are loaded.
    """
    from . import worker
    return worker.safe(name) if name else name


def count_wants_for_paths(conn, paths):
    """How many wants point at any of these files."""
    if not paths:
        return 0
    total = 0
    for i in range(0, len(paths), 400):
        part = paths[i:i + 400]
        marks = ",".join("?" * len(part))
        total += conn.execute(
            f"SELECT COUNT(*) n FROM wants WHERE file_path IN ({marks})", part).fetchone()["n"]
    return total


def delete_wants_for_paths(conn, paths):
    """Remove wants pointing at these files. Returns how many were removed.

    Chunked, and a no-op on an empty list. A single ``IN (...)`` clause failed twice over: SQLite
    caps host parameters, so a large artist raised "too many SQL variables"; and an empty list
    rendered ``IN ()``, which is a syntax error rather than a no-op — reachable whenever every file
    had already gone by the time the confirmation was submitted.
    """
    if not paths:
        return 0
    removed = 0
    for i in range(0, len(paths), 400):
        part = paths[i:i + 400]
        marks = ",".join("?" * len(part))
        removed += conn.execute(
            f"DELETE FROM wants WHERE file_path IN ({marks})", part).rowcount
    return removed


def find_want(conn, artist, title):
    """Row id of the want for this exact song, or None. Same key ``add_want`` deduplicates on."""
    r = conn.execute("SELECT id FROM wants WHERE norm_artist=? AND norm_title=?",
                     (norm(artist), strict_norm(title))).fetchone()
    return r["id"] if r else None


def enrich_want(conn, want_id, album=None, year=None, artist_lead=None, track_no=None,
                commit=True):
    """Fill in facts a want was created without. Returns True if anything changed.

    Only ever fills NULLs — an existing value is the user's or an earlier catalogue's and is not
    overwritten. This exists because wants are keyed on (artist, title) and ``add_want`` therefore
    skips one that already exists, so a source that has since learned the album had no way to say so.
    Every MusicBrainz artist add before the release-browse fix produced album-less wants, and the
    whole discography was filed under "Singles" as a result.
    """
    row = conn.execute("SELECT album, year, artist_lead, track_no FROM wants WHERE id=?",
                       (want_id,)).fetchone()
    if row is None:
        return False
    sets, args = [], []
    if album and not row["album"]:
        # Album and year describe the SAME release, so they are written together — filling the album
        # while keeping a year that came from somewhere else split one album across two directories:
        # "A Lot About Livin’ (2000)" sitting beside "A Lot About Livin’ (2004)".
        sets += ["album=?", "year=?"]
        args += [album, year]
    elif year and not row["year"]:
        sets.append("year=?")
        args.append(year)
    if track_no and not row["track_no"] and album:
        # A track number is a fact about ONE release, so it is only written alongside the album it
        # belongs to: either the album being filled in above, or one the row already names with the
        # same spelling. Filling it against a row attributed to a different release would number the
        # file from the wrong tracklist — the very defect this column exists to close.
        if not row["album"] or strict_norm(row["album"]) == strict_norm(album):
            sets.append("track_no=?")
            args.append(track_no)
    if artist_lead and not row["artist_lead"]:
        # folder_key here too. This was the one writer that skipped it, so an enrichment that won a
        # race against the backfill left a raw credit in a column every other path stores sanitised.
        sets.append("artist_lead=?")
        args.append(folder_key(artist_lead))
    if not sets:
        return False
    conn.execute(f"UPDATE wants SET {', '.join(sets)} WHERE id=?", args + [want_id])
    if commit:
        conn.commit()
    return True


def cancel_batch(conn, batch):
    """Undo a bulk add. Returns (removed, kept).

    Removed: wants still PENDING, and wants marked satisfied by a file that was already on disk
    (``provider IS NULL``) — those downloaded nothing, so the row is pure bookkeeping and deleting it
    touches no audio. Without this second case an album you already own in full leaves a dozen rows
    no button can clear.

    Kept: anything actually downloaded, since the row is the provenance for a real file; and anything
    UNAVAILABLE, so an undo does not quietly wipe its strike history and restart the backoff.
    """
    cur = conn.execute(
        "DELETE FROM wants WHERE batch=? AND (status=? OR (status=? AND provider IS NULL))",
        (batch, STATUS_PENDING, STATUS_HAVE))
    kept = conn.execute("SELECT COUNT(*) n FROM wants WHERE batch=?", (batch,)).fetchone()
    conn.commit()
    return cur.rowcount, (kept["n"] if kept else 0)


def add_job(conn, kind, ref, source, label=None, requested_by=None):
    """Queue a bulk add for the worker. Returns the job id."""
    cur = conn.execute(
        "INSERT INTO jobs (kind, ref, source, label, requested_by, created_at, status) "
        "VALUES (?,?,?,?,?,?,?)",
        (kind, ref, source, label, requested_by, time.time(), JOB_QUEUED))
    conn.commit()
    return cur.lastrowid


def claim_jobs(conn, limit=5):
    """Take queued jobs for this worker, marking them running in one transaction.

    Claimed before any of them runs, so a crash mid-batch leaves them RUNNING rather than queued —
    visible as stuck rather than silently repeating a partial add on the next cycle.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status=? ORDER BY created_at LIMIT ?",
            (JOB_QUEUED, limit)).fetchall()
        if rows:
            conn.executemany("UPDATE jobs SET status=?, started_at=? WHERE id=?",
                             [(JOB_RUNNING, time.time(), r["id"]) for r in rows])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return rows


def finish_job(conn, job_id, status, detail=None, batch=None):
    conn.execute("UPDATE jobs SET status=?, finished_at=?, detail=?, batch=? WHERE id=?",
                 (status, time.time(), detail, batch, job_id))
    conn.commit()


def recent_jobs(conn, limit=8):
    """Queued and running jobs first, then the most recently finished — the add's own progress view."""
    return conn.execute(
        "SELECT * FROM jobs ORDER BY (status IN (?,?)) DESC, "
        "COALESCE(finished_at, started_at, created_at) DESC LIMIT ?",
        (JOB_QUEUED, JOB_RUNNING, limit)).fetchall()


def jobs_in_flight(conn):
    return conn.execute("SELECT COUNT(*) n FROM jobs WHERE status IN (?,?)",
                        (JOB_QUEUED, JOB_RUNNING)).fetchone()["n"]


def recent_batches(conn, limit=10):
    """Bulk adds that still have cancellable wants, newest first."""
    return conn.execute(
        "SELECT batch, batch_label, requested_by, MAX(requested_at) at, COUNT(*) total,"
        " SUM(status=?) pending, SUM(status=?) have,"
        " SUM(status=? OR (status=? AND provider IS NULL)) cancellable"
        " FROM wants WHERE batch IS NOT NULL GROUP BY batch"
        " ORDER BY at DESC LIMIT ?",
        (STATUS_PENDING, STATUS_HAVE, STATUS_PENDING, STATUS_HAVE, limit)).fetchall()


def library_stats(conn):
    r = conn.execute(
        "SELECT COUNT(*) n, SUM(lossless) ll, SUM(size) bytes, "
        "SUM(CASE WHEN lossless=0 AND bitrate>0 AND bitrate<250000 THEN 1 ELSE 0 END) upgradable "
        "FROM files").fetchone()
    w = conn.execute(
        "SELECT status, COUNT(*) n FROM wants GROUP BY status").fetchall()
    return {
        "files": r["n"] or 0,
        "lossless": r["ll"] or 0,
        "bytes": r["bytes"] or 0,
        "upgradable": r["upgradable"] or 0,
        "wants": {row["status"]: row["n"] for row in w},
        # Leads, not credits: counting the full credit strings reports every collaboration as an
        # extra artist, disagreeing with both the directory count and the Library tab's grouping.
        "artists": conn.execute(
            "SELECT COUNT(DISTINCT artist_lead) n FROM files").fetchone()["n"],
    }
