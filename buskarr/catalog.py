"""Catalogue sources — what music *exists*, as distinct from where to download it.

A catalogue is only ever a convenience here. Wants are identified by ``(artist, title, duration)``,
never by a catalogue's own ids, so no source's coverage limits what can be asked for; anything can
still be typed in by hand. This is the whole reason the track-first model escapes the ceiling that
made an album-and-MBID-keyed tool unusable for this library.

Three sources, because their blind spots are different:

  deezer       commercial streaming. Best for mainstream singles, and it lists singles that
               release-group-oriented databases omit. Blind spot: independent and regional artists
               are simply absent — searching a Canadian folk singer returned an unrelated American
               act of the same name and nothing else.
  musicbrainz  community-edited, so coverage is *best* exactly where commercial catalogues are
               worst. Uniquely carries hand-written ``disambiguation`` prose ("Canadian country and
               folk artist" vs "Canadian techno producer"), which is the only reliable way to tell
               same-named artists apart. Rate limited to 1 request/second — respected here by a
               module-level gate, not by hoping.
  itunes       Apple's Search API. No key, no auth, broad, and returns durations and album names.

Every source fails soft: a source that errors or times out contributes nothing and the others still
answer. A catalogue outage must not make the app unusable when manual entry would still work.
"""
import collections
import concurrent.futures
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import db

UA = "buskarr/1.0 (homelab music manager)"
TIMEOUT = 25
# MusicBrainz asks for at most one request per second per client and will start returning 503 to
# clients that ignore it. One process-wide gate, since concurrent searches share the budget.
MB_MIN_INTERVAL = 1.1
# 503 from MusicBrainz means "you are going too fast", so it is retried rather than treated as an
# outage. 502/504 are transient too. Anything else is a real error and raises immediately.
MB_RETRY_CODES = {502, 503, 504}
MB_RETRIES = 4
MB_BACKOFF = 2.0
# Paging caps. Reaching one means the discography is INCOMPLETE and the caller is told so.
MB_MAX_RECORDINGS = 1000
MB_MAX_RELEASES = 500
DEEZER_PAGE = 200          # what one request may return; the endpoint pages past it via "next"
DEEZER_MAX_RELEASES = 500  # absolute cap, matching MusicBrainz's
ITUNES_MAX_SONGS = 200
# Deezer has no per-release bulk endpoint, so a discography costs one request per release — 47 of
# them for a mid-sized artist, which took 14.9 s sequentially and dominated the whole add. Deezer
# tolerates roughly 50 requests per 5 s per IP, so 8 in flight is comfortably inside that while
# turning 15 s into about 2. Deliberately NOT used for MusicBrainz: its one-request-per-second gate
# is mandatory, and fanning out would only queue threads on the lock.
RELEASE_WORKERS = 8
_mb_gate = threading.Lock()
_mb_last = [0.0]


def _get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as fh:
        return json.load(fh)


def _fanout(fn, items, workers=RELEASE_WORKERS):
    """Map fn over items concurrently, preserving order. A failure comes back as None.

    Order is preserved because release order is the catalogue's own relevance ordering, and the
    repackaging check reads it.
    """
    def guard(x):
        try:
            return fn(x)
        except Exception:
            return None

    if not items:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(guard, items))


def _mb_get(path):
    """One MusicBrainz request, never less than MB_MIN_INTERVAL after the previous one STARTED.

    The request is issued while still holding the gate. Releasing first only spaced the local
    timestamps: a thread could stamp, stall in DNS, and land its request immediately after the next
    thread's — so the server saw two hits together under the concurrent search fan-out. Monotonic
    because a wall-clock step could produce a negative or enormous wait.

    503 is retried, because for this API it means "slow down", not "broken". Without the retry a
    throttled moment made a discography add fall back to the endpoint that cannot report albums, and
    the whole artist was filed under "Singles" — the failure looked like a data problem rather than a
    rate-limit one, which is exactly how it went unnoticed.
    """
    with _mb_gate:
        for attempt in range(MB_RETRIES):
            wait = MB_MIN_INTERVAL - (time.monotonic() - _mb_last[0])
            if wait > 0:
                time.sleep(wait)
            _mb_last[0] = time.monotonic()
            try:
                return _get(f"https://musicbrainz.org/ws/2/{path}")
            except urllib.error.HTTPError as e:
                if e.code not in MB_RETRY_CODES or attempt == MB_RETRIES - 1:
                    raise
                # Held inside the gate on purpose: backing off while letting other threads through
                # would keep hitting a server that has just asked us to stop.
                time.sleep(MB_BACKOFF * (2 ** attempt))
                _mb_last[0] = time.monotonic()


def _credit(entity):
    """Render a MusicBrainz artist-credit as credited.

    ``", ".join(names)`` turned "A feat. B" into "A, B", and that string becomes the want's artist —
    so it reaches the filename and the tag. Each credit part carries its own ``joinphrase``.
    """
    parts = entity.get("artist-credit") or []
    if not parts:
        return ""
    out = []
    for c in parts:
        if isinstance(c, str):
            out.append(c)
            continue
        name = c.get("name") or (c.get("artist") or {}).get("name") or ""
        out.append(name + (c.get("joinphrase") or ""))
    return "".join(out).strip()


def _mb_browse(path, list_key, count_key, cap, page=100):
    """Page a MusicBrainz browse endpoint to completion. Returns (rows, truncated).

    Driven by the reported total, never by a short page. MusicBrainz returns fewer rows than the
    requested limit on heavy ``inc`` queries — release browse with recordings returned 55 of a
    reported 167 on the first page — so breaking at the first short page walked a third of the
    discography and then reported it complete. ``truncated`` is the only honest answer when the
    reported total was not reached.
    """
    rows, offset, total, truncated = [], 0, None, False
    while True:
        if offset >= cap:
            truncated = True
            break
        try:
            d = _mb_get(f"{path}&limit={page}&offset={offset}")
        except Exception:
            truncated = True
            break
        batch = d.get(list_key) or []
        if total is None:
            total = d.get(count_key)
        rows.extend(batch)
        if not batch:
            break                       # no progress available; stop rather than spin
        offset += len(batch)
        if total is not None:
            if offset >= total:
                break
        elif len(batch) < page:
            break                       # no count to trust; fall back to the old signal
    if total and len(rows) < total:
        truncated = True
    return rows, truncated


def _credit_ids(entity):
    return [(c.get("artist") or {}).get("id") for c in (entity.get("artist-credit") or [])
            if isinstance(c, dict) and (c.get("artist") or {}).get("id")]


class Deezer:
    name = "deezer"
    api = "https://api.deezer.com"

    def search_tracks(self, q, limit=20):
        d = _get(f"{self.api}/search?q={urllib.parse.quote(q)}&limit={limit}")
        out = []
        for t in d.get("data", []):
            out.append({"source": self.name, "artist": (t.get("artist") or {}).get("name", ""),
                        "title": t.get("title") or "",
                        "album": (t.get("album") or {}).get("title") or "",
                        "year": None, "duration": float(t.get("duration") or 0) or None})
        return out

    def search_artists(self, q, limit=8):
        d = _get(f"{self.api}/search/artist?q={urllib.parse.quote(q)}&limit={limit}")
        return [{"source": self.name, "ref": str(a["id"]), "name": a.get("name") or "",
                 "hint": "", "releases": a.get("nb_album"), "listeners": a.get("nb_fan")}
                for a in d.get("data", [])]

    def known_for(self, ref, n=3):
        """Identifying titles. ``/top`` is empty for obscure artists — the very case that needs
        disambiguating — so fall back to the first release's listing."""
        try:
            d = _get(f"{self.api}/artist/{ref}/top?limit={n}")
            titles = [t["title"] for t in (d.get("data") or []) if t.get("title")]
            if titles:
                return titles[:n]
        except Exception:
            pass
        try:
            albums = _get(f"{self.api}/artist/{ref}/albums?limit=1").get("data") or []
            if not albums:
                return []
            d = _get(f"{self.api}/album/{albums[0]['id']}")
            return [t["title"] for t in
                    ((d.get("tracks") or {}).get("data") or [])[:n] if t.get("title")]
        except Exception:
            return []

    def artist_catalogue(self, ref):
        """(artist_name, [track], meta) for a bulk add — every track of every release.

        ``meta["complete"]`` is False when the release list hit the API page limit or any single
        release failed to load. Reporting a truncated discography as "everything by this artist" is
        worse than reporting it as partial.
        """
        name = (_get(f"{self.api}/artist/{ref}") or {}).get("name")
        # Paged with index/limit rather than one capped request: the endpoint returns at most one
        # page however large the limit, and stopping there silently dropped everything past it —
        # 236 of Mozart's 436 releases. The cap still exists, but reaching it is REPORTED.
        releases, complete = [], True
        while len(releases) < DEEZER_MAX_RELEASES:
            d = _get(f"{self.api}/artist/{ref}/albums?limit={DEEZER_PAGE}&index={len(releases)}")
            batch = d.get("data") or []
            releases.extend(batch)
            if not batch or not d.get("next"):
                break
        else:
            complete = False
            releases = releases[:DEEZER_MAX_RELEASES]
        fetched = _fanout(lambda rel: _get(f"{self.api}/album/{rel['id']}"), releases)
        tracks = []
        for rel, d in zip(releases, fetched):
            if d is None:
                # A release that failed to load means this is not the whole discography, and saying
                # otherwise is worse than saying it is partial.
                complete = False
                continue
            year = (d.get("release_date") or "")[:4] or None
            for t in ((d.get("tracks") or {}).get("data") or []):
                if not t.get("title"):
                    continue
                tracks.append({"artist": (t.get("artist") or {}).get("name") or name,
                               "artist_ids": [str((t.get("artist") or {}).get("id"))]
                                             if (t.get("artist") or {}).get("id") else [],
                               "title": t["title"], "album": d.get("title"),
                               "year": year if (year or "").isdigit() else None,
                               "duration": float(t.get("duration") or 0) or None,
                               "release": str(rel["id"]), "release_title": d.get("title"),
                               # "album" | "single" | "ep" | "compilation" — drives whether this
                               # release gets to claim a song before a single does.
                               "release_type": rel.get("record_type") or d.get("record_type"),
                               "release_secondary": []})
        return name, tracks, {"complete": complete, "artist_ref": str(ref)}


class MusicBrainz:
    name = "musicbrainz"

    def search_tracks(self, q, limit=20):
        d = _mb_get(f"recording/?query={urllib.parse.quote(q)}&fmt=json&limit={limit}")
        out = []
        for r in d.get("recordings", []):
            credit = _credit(r)
            rel = (r.get("releases") or [{}])[0]
            date = rel.get("date") or ""
            out.append({"source": self.name, "artist": credit, "title": r.get("title") or "",
                        "album": rel.get("title") or "", "year": date[:4] or None,
                        "duration": (r["length"] / 1000.0) if r.get("length") else None})
        return out

    def search_artists(self, q, limit=8):
        d = _mb_get(f"artist/?query={urllib.parse.quote(q)}&fmt=json&limit={limit}")
        out = []
        for a in d.get("artists", []):
            # The disambiguation string is written by editors specifically to separate same-named
            # artists, which no commercial catalogue provides. Area is the next-best signal.
            bits = [b for b in (a.get("disambiguation"),
                                (a.get("area") or {}).get("name"),
                                a.get("type")) if b]
            out.append({"source": self.name, "ref": a["id"], "name": a.get("name") or "",
                        "hint": " · ".join(bits), "releases": None, "listeners": None})
        return out

    def known_for(self, ref, n=3):
        try:
            d = _mb_get(f"recording?artist={ref}&fmt=json&limit=25")
            seen, titles = set(), []
            for r in d.get("recordings", []):
                k = db.strict_norm(r.get("title") or "")
                if not k or k in seen:
                    continue
                seen.add(k)
                titles.append(r["title"])
                if len(titles) >= n:
                    break
            return titles
        except Exception:
            return []

    def artist_catalogue(self, ref):
        """Browse RELEASES with their tracklists, not bare recordings.

        Recording-browse cannot supply a release title — ``inc=releases`` is rejected there outright
        (400: "releases is not a valid inc parameter") — so every want this source produced carried
        ``album=None``, and ``worker.destination`` filed entire discographies under "Singles": 279
        Alan Jackson tracks and 211 Longest Johns tracks, not one of them attributed to an album.

        Release-browse was the endpoint to use all along. It carries the tracklist, the release
        title, the date, and ``release-group.primary-type`` — which is also the only thing that makes
        an albums-before-singles preference possible. It is cheaper too: one page per 100 *releases*
        rather than per 100 recordings, and 167 releases covered what took four pages of recordings.

        ``inc=artist-credits`` remains mandatory. Without it every row had to be stamped with the
        searched artist's name, which mis-credited every collaboration and made the credit gate
        compare a name against itself.

        Both endpoints are walked, because their coverage genuinely differs: for Alan Jackson,
        releases carry 151 distinct titles while recordings know 400, and **255 of those exist on no
        release at all** (covers, live takes, promos). Those are the real album-less songs — the
        "singles filling the gaps" — so they are kept, marked ``unreleased`` and ranked last, rather
        than either dropped or allowed to outrank an album.
        """
        name = None
        try:
            name = (_mb_get(f"artist/{ref}?fmt=json") or {}).get("name")
        except Exception:
            pass
        tracks, attributed = [], set()
        releases, rel_truncated = _mb_browse(
            f"release?artist={ref}&fmt=json&inc=recordings+artist-credits+release-groups",
            "releases", "release-count", MB_MAX_RELEASES)
        for rel in releases:
            group = rel.get("release-group") or {}
            year = (rel.get("date") or group.get("first-release-date") or "")[:4]
            for medium in (rel.get("media") or []):
                for tr in (medium.get("tracks") or []):
                    rec = tr.get("recording") or {}
                    title = tr.get("title") or rec.get("title")
                    if not title:
                        continue
                    # The track's own credit is what the release prints; the recording's is the
                    # canonical one. Prefer the release, since that is what the listener sees.
                    credited = tr if tr.get("artist-credit") else rec
                    length = tr.get("length") or rec.get("length")
                    attributed.add(db.strict_norm(title))
                    tracks.append({
                        "artist": _credit(credited) or name or "",
                        "artist_ids": _credit_ids(credited),
                        "title": title,
                        "album": rel.get("title"),
                        "year": year if year.isdigit() else None,
                        "duration": (length / 1000.0) if length else None,
                        "release": rel.get("id"),
                        "release_title": rel.get("title"),
                        "release_type": group.get("primary-type"),
                        "release_secondary": group.get("secondary-types") or [],
                    })
        recordings, rec_truncated = _mb_browse(
            f"recording?artist={ref}&fmt=json&inc=artist-credits",
            "recordings", "recording-count", MB_MAX_RECORDINGS)
        for r in recordings:
            title = r.get("title")
            # Already claimed by a release. Skipped on title alone deliberately: "everything by X"
            # means every song, not every take of every song — the same rule add_artist applies.
            if not title or db.strict_norm(title) in attributed:
                continue
            date = (r.get("first-release-date") or "")[:4]
            tracks.append({
                "artist": _credit(r) or name or "",
                "artist_ids": _credit_ids(r),
                "title": title,
                # No album, and none is invented: falling back to the song title wrote a fake
                # one-track album per song into the library and into Jellyfin.
                "album": None,
                "year": date if date.isdigit() else None,
                "duration": (r["length"] / 1000.0) if r.get("length") else None,
                "release": None,
                "release_title": None,
                "release_type": "unreleased",
                "release_secondary": [],
            })
        truncated = rel_truncated or rec_truncated
        if not name and tracks:
            # The artist lookup is a separate request and MusicBrainz returns 503 when busy. Fall
            # back to the commonest credit rather than reporting the artist as None, which would make
            # bulk.credited_to waive the name check entirely (the id filter still guards it).
            name = collections.Counter(t["artist"] for t in tracks if t["artist"]).most_common(1)
            name = name[0][0] if name else None
        return name, tracks, {"complete": not truncated, "artist_ref": ref}


class ITunes:
    name = "itunes"
    api = "https://itunes.apple.com"

    def search_tracks(self, q, limit=20):
        d = _get(f"{self.api}/search?term={urllib.parse.quote(q)}&entity=song&limit={limit}")
        out = []
        for t in d.get("results", []):
            ms = t.get("trackTimeMillis")
            out.append({"source": self.name, "artist": t.get("artistName") or "",
                        "title": t.get("trackName") or "",
                        "album": t.get("collectionName") or "",
                        "year": (t.get("releaseDate") or "")[:4] or None,
                        "duration": (ms / 1000.0) if ms else None})
        return out

    def search_artists(self, q, limit=8):
        d = _get(f"{self.api}/search?term={urllib.parse.quote(q)}"
                 f"&entity=musicArtist&limit={limit}")
        return [{"source": self.name, "ref": str(a.get("artistId")),
                 "name": a.get("artistName") or "", "hint": a.get("primaryGenreName") or "",
                 "releases": None, "listeners": None}
                for a in d.get("results", []) if a.get("artistId")]

    def known_for(self, ref, n=3):
        try:
            d = _get(f"{self.api}/lookup?id={ref}&entity=song&limit={n + 1}")
            return [r["trackName"] for r in d.get("results", [])
                    if r.get("wrapperType") == "track" and r.get("trackName")][:n]
        except Exception:
            return []

    def artist_catalogue(self, ref):
        d = _get(f"{self.api}/lookup?id={ref}&entity=song&limit={ITUNES_MAX_SONGS}")
        results = d.get("results", [])
        name = next((r.get("artistName") for r in results if r.get("artistName")), None)
        songs = [t for t in results if t.get("wrapperType") == "track" and t.get("trackName")]
        tracks = []
        for t in songs:
            ms = t.get("trackTimeMillis")
            year = (t.get("releaseDate") or "")[:4]
            tracks.append({"artist": t.get("artistName") or name,
                           "artist_ids": [str(t["artistId"])] if t.get("artistId") else [],
                           "title": t["trackName"],
                           "album": t.get("collectionName"),
                           "year": year if year.isdigit() else None,
                           "duration": (ms / 1000.0) if ms else None,
                           "release": str(t.get("collectionId") or ""),
                           "release_title": t.get("collectionName"),
                           # Apple exposes no primary type. A one-track collection is a single by
                           # definition; anything longer is left unknown rather than guessed at.
                           "release_type": ("single" if t.get("trackCount") == 1 else None),
                           "release_secondary": []})
        # The lookup endpoint has no paging, so a full page means there is probably more.
        complete = len(results) < ITUNES_MAX_SONGS
        return name, tracks, {"complete": complete, "artist_ref": str(ref)}


SOURCES = {s.name: s for s in (Deezer(), MusicBrainz(), ITunes())}
DEFAULT_ORDER = ["deezer", "musicbrainz", "itunes"]
# For picking between same-named artists MusicBrainz leads, because it is the only source carrying
# editor-written disambiguation prose — the whole point of that column.
ARTIST_ORDER = ["musicbrainz", "deezer", "itunes"]


def get(name):
    return SOURCES.get(name)


def _track_key(t):
    """Identity for cross-source dedup: same song from three catalogues is one row.

    Duration is bucketed rather than compared exactly because catalogues disagree by a second or two
    on the same recording, but parentheticals are preserved — ``(live)`` and ``(reprise)`` are
    different recordings and must not collapse.
    """
    dur = int((t["duration"] or 0) // 5)
    return db.norm(t["artist"]), db.strict_norm(t["title"]), dur


def merge_tracks(results):
    """Collapse per-source track lists into one ranked list, recording which sources had each.

    Each source returns its own results in relevance order, and that order is the single most
    valuable thing it tells us — so it is preserved as ``rank`` (the best position the track reached
    in any source) and used as the primary sort. Sorting merged results alphabetically instead threw
    relevance away completely: a search for one artist led with an unrelated one whose name happened
    to sort earlier.
    """
    merged = {}
    for lst in results:
        for i, t in enumerate(lst or []):
            if not t.get("artist") or not t.get("title"):
                continue
            k = _track_key(t)
            cur = merged.get(k)
            if cur is None:
                merged[k] = dict(t, sources=[t["source"]], rank=i)
                continue
            if t["source"] not in cur["sources"]:
                cur["sources"].append(t["source"])
            cur["rank"] = min(cur["rank"], i)
            # Keep whichever copy carries the most usable metadata.
            for field in ("duration", "album", "year"):
                if not cur.get(field) and t.get(field):
                    cur[field] = t[field]
    # Relevance first; agreement across catalogues breaks ties in its favour.
    return sorted(merged.values(), key=lambda t: (t["rank"], -len(t["sources"])))
