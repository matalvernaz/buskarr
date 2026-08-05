"""Bulk additions — whole albums and whole artists, still as individual track wants.

A track-first model makes bulk adds strictly better than an album-first one. Adding an album you
partly own queues only the tracks you lack, because the duplicate check runs per track. Lidarr
rejects that same release outright for failing its 80% album-match threshold, which is how ~694
completed downloads ended up unimported.

Artist-level adds need two guards the album level does not.

A discography is full of compilations and "greatest hits" repackagings whose tracks you already own
under other releases. Those are skipped by overlap, not by title keywords — keyword lists never
converge ('The Conducted Tom Lehrer' and '10 Hits of Tom Lehrer' match nothing sensible).

And a catalogue's artist page lists *contributions*, so it carries tribute acts and guest spots
alongside the artist's own releases. ``credit.credited_to`` is what keeps them out; see that module
for why a guest credit does not count.
"""
import json
import uuid
import urllib.parse
import urllib.request

from . import catalog, credit, db

DEEZER = "https://api.deezer.com"
# A release most of whose tracks are already held is a repackaging, not new material.
REPACKAGE_OVERLAP = float(__import__("os").environ.get("REPACKAGE_OVERLAP", "0.4"))

# Which release gets to claim a song when it appears on several. Albums first, singles only to fill
# what no album covers: a song released as both belongs to the album, and filing it under
# "Artist/Singles/" instead is simply wrong. Unknown sits between EP and single — better to treat an
# untyped release as possibly-an-album than to demote it below a known single.
RELEASE_RANK = {"album": 0, "ep": 1, "single": 3, "broadcast": 4, "other": 4,
                # A recording MusicBrainz knows but that sits on no release at all — a cover, a live
                # take, a promo. Genuinely album-less, so it fills gaps and never claims a song an
                # actual release carries.
                "unreleased": 6}
RANK_UNKNOWN = 2
# Compilations last. They are usually caught by the repackaging guard as well, but ordering them
# last means the original release claims a song before a "greatest hits" can.
RANK_COMPILATION = 5


def release_order(items):
    """Sort key for one release's tracks: (preference rank, year, title).

    Earliest year breaks ties so an original album claims a song ahead of a later reissue.
    """
    kind = next((t.get("release_type") for t in items if t.get("release_type")), None)
    secondary = {str(s).lower() for t in items for s in (t.get("release_secondary") or [])}
    rank = RANK_COMPILATION if "compilation" in secondary else \
        RELEASE_RANK.get(str(kind).lower(), RANK_UNKNOWN)
    year = next((t.get("year") for t in items if t.get("year")), None)
    title = next((t.get("release_title") for t in items if t.get("release_title")), "") or ""
    return rank, (year or "9999"), title.lower()


def deezer(path):
    req = urllib.request.Request(f"{DEEZER}/{path}", headers={"User-Agent": "buskarr/1.0"})
    with urllib.request.urlopen(req, timeout=45) as fh:
        return json.load(fh)


def album_tracks(album_id, source="deezer"):
    """Return (album_title, year, [track dicts]) for one album from a catalogue source."""
    if source == "itunes":
        d = _get(f"https://itunes.apple.com/lookup?id={album_id}&entity=song&limit=200")
        rows = d.get("results", [])
        head = next((r for r in rows if r.get("wrapperType") == "collection"), {})
        year = (head.get("releaseDate") or "")[:4] or None
        tracks = []
        for i, t in enumerate((r for r in rows if r.get("wrapperType") == "track"), 1):
            ms = t.get("trackTimeMillis")
            tracks.append({"title": t.get("trackName"), "artist": t.get("artistName"),
                           "duration": (ms / 1000.0) if ms else None, "track_no": i})
        return head.get("collectionName"), year, tracks
    d = deezer(f"album/{album_id}")
    year = (d.get("release_date") or "")[:4] or None
    tracks = [{"title": t.get("title"), "artist": (t.get("artist") or {}).get("name"),
               "duration": float(t.get("duration") or 0) or None,
               "track_no": i}
              for i, t in enumerate(((d.get("tracks") or {}).get("data") or []), 1)]
    return d.get("title"), year, tracks


def _get(url):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "buskarr/1.0"})
    with urllib.request.urlopen(req, timeout=45) as fh:
        return json.load(fh)


def add_album(conn, album_id, requested_by=None, allow_dup=False, source="deezer"):
    """Add every track of an album as an individual want, in one transaction.

    Fetched first, written second. Committing per track let the worker claim rows mid-add, past the
    point where ``db.cancel_batch`` could undo them.
    """
    title, year, tracks = album_tracks(album_id, source)
    if not tracks:
        return {"album": title, "added": 0, "already": 0, "total": 0, "batch": None,
                "source": source}
    batch = uuid.uuid4().hex[:10]
    label = f"album \u201c{title}\u201d ({source})"
    added = already = existing = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for t in tracks:
            if not t["title"] or not t["artist"]:
                continue
            wid, created = db.add_want(conn, t["artist"], t["title"], title, year,
                                       t["duration"], requested_by, allow_dup=allow_dup,
                                       batch=batch, batch_label=label, commit=False)
            row = conn.execute("SELECT status FROM wants WHERE id=?", (wid,)).fetchone()
            if row and row["status"] == db.STATUS_HAVE:
                already += 1
            elif created:
                added += 1
            else:
                existing += 1
        db.log_event(conn, "add-album", title,
                     f"{added} queued, {already} already held", commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"album": title, "year": year, "added": added, "already": already,
            "existing": existing, "total": len(tracks), "batch": batch, "source": source}


def add_artist(conn, ref, requested_by=None, skip_repackagings=True, source="deezer",
               max_tracks=0):
    """Add an artist's catalogue, one want per track, from any catalogue source.

    The whole catalogue is fetched BEFORE anything is written, then every want is inserted in a single
    transaction. Committing per track let the background worker claim rows while the add was still
    running — and once claimed, ``db.cancel_batch`` can no longer undo them, so a mistaken add became
    partially permanent. A failure partway now rolls back cleanly.

    Releases mostly already held are skipped as repackagings, or a discography add drags in every
    compilation. "Everything by X" means every SONG, not every recording of every song, so the
    already-held test ignores duration; otherwise a prolific live performer queues a hundred
    alternate takes of a catalogue already owned — measured on Tom Lehrer, 133 of them.
    """
    src = catalog.get(source)
    if src is None:
        raise ValueError(f"unknown catalogue source {source!r}")
    wanted, tracks, meta = src.artist_catalogue(ref)
    batch = uuid.uuid4().hex[:10]
    label = f"everything by {wanted or ref} ({source})"
    summary = {"artist": wanted, "source": source, "releases": 0, "skipped_repackaging": 0,
               "added": 0, "already": 0, "other_artist": 0, "existing": 0, "enriched": 0,
               "detail": [], "batch": batch, "complete": bool(meta.get("complete", True))}
    if not summary["complete"]:
        summary["detail"].append(
            "the catalogue was truncated by an API limit or a failed page, so this is NOT the "
            "complete discography")

    by_release = {}
    for t in tracks:
        # Keyed on the release ID, never the title: two distinct same-titled releases merged into one
        # bucket, and a 9/10-held release plus a 0/10-held one became 9/20 — over the repackaging
        # threshold, so ten wanted tracks were dropped.
        by_release.setdefault(t.get("release") or "", []).append(t)
    summary["releases"] = len(by_release)

    seen_titles, held_titles = set(), set()
    # One album, one directory. The same album exists as several MusicBrainz releases and some carry
    # no date at all, so tracks claimed from different editions produced "Who I Am (1994)" beside
    # "Who I Am". The first year seen for an album title wins for every track on it — releases are
    # already walked earliest-first, so that is the original.
    album_year = {}
    capped = False
    # Albums are walked before singles, so ``seen_titles`` gives a song to its album and a single
    # only gets what no album carried. Without this the traversal order was the catalogue's, which
    # is arbitrary — and on a source that reported no album at all, everything became a single.
    ordered = sorted(by_release.items(), key=lambda kv: release_order(kv[1]))
    conn.execute("BEGIN IMMEDIATE")
    try:
        for rel_key, items in ordered:
            if capped:
                break
            rel_name = next((t.get("release_title") for t in items if t.get("release_title")), None)
            own = []
            for t in items:
                if not t["title"]:
                    continue
                # Prefer catalogue ids when available: an id comparison beats name matching, which
                # is what let a tribute act through in the first place.
                by_id = credit.credited_by_id(t.get("artist_ids"), meta.get("artist_ref"))
                ok = by_id if by_id is not None else credit.credited_to(t["artist"], wanted)
                if ok:
                    own.append(t)
                else:
                    summary["other_artist"] += 1
            if not own:
                if items:
                    summary["detail"].append(
                        f"skipped {rel_name or 'untitled'!r}: no tracks credited to {wanted!r}")
                continue
            queue_new = True
            if skip_repackagings and rel_key and len(by_release) > 1:
                hits = [t for t in own if db.find_file(conn, t["artist"] or "", t["title"])]
                if len(hits) / len(own) > REPACKAGE_OVERLAP:
                    summary["skipped_repackaging"] += 1
                    summary["releases"] -= 1
                    held_titles.update(db.strict_norm(t["title"]) for t in hits)
                    summary["detail"].append(
                        f"skipped {rel_name or rel_key!r}: {len(hits)}/{len(own)} already held "
                        "(repackaging)")
                    # Skipped for QUEUEING only. Skipping the release outright also skipped
                    # attribution, so an artist whose albums you already own kept every track filed
                    # under "Singles" — the guard against compilations was silently deciding library
                    # layout as well. Ordering protects the attribution: real albums are walked
                    # before compilations, so the original has already claimed its tracks.
                    queue_new = False
            for t in own:
                key = db.strict_norm(t["title"])
                if key in seen_titles:
                    continue
                seen_titles.add(key)
                # Every track that got here passed the credit gate, so the wanted artist IS the lead
                # credit — no separate inference needed, and none of the three sources has to plumb
                # a lead-artist field through. It decides the library folder at placement time.
                album = t.get("album") or rel_name or None
                year = t.get("year")
                if album:
                    # Not setdefault: a first edition with no date would store None permanently and
                    # never upgrade, so later tracks kept their own years and the album split across
                    # directories anyway — the exact failure this map exists to prevent. The first
                    # REAL year wins, and every track on the album then reads it back.
                    key_album = db.norm(album)
                    if year and not album_year.get(key_album):
                        album_year[key_album] = year
                    year = album_year.get(key_album) or year
                held = db.find_file(conn, t["artist"] or "", t["title"])
                if held:
                    held_titles.add(key)
                if held or not queue_new:
                    # Nothing to fetch, but an existing want can still learn where the song belongs.
                    # This is the repair path for every want made before this source could report an
                    # album: it queues nothing and downloads nothing.
                    wid = db.find_want(conn, t["artist"], t["title"])
                    if wid and db.enrich_want(conn, wid, album, year, wanted or None, commit=False):
                        summary["enriched"] += 1
                    continue
                if max_tracks and summary["added"] >= max_tracks:
                    summary["detail"].append(f"stopped at the {max_tracks}-track cap")
                    capped = True
                    break
                wid, created = db.add_want(
                    conn, t["artist"], t["title"], album,
                    year, t.get("duration"), requested_by,
                    batch=batch, batch_label=label, artist_lead=wanted or None, commit=False)
                # Only a genuinely new row counts as queued; an already-pending want reported as
                # "queued" made the summary claim work that never happened.
                if created:
                    summary["added"] += 1
                else:
                    summary["existing"] += 1
                    if db.enrich_want(conn, wid, album, year, wanted or None, commit=False):
                        summary["enriched"] += 1
        summary["already"] = len(held_titles)
        db.log_event(conn, "add-artist", f"{source}:{ref}",
                     f"{summary['added']} queued, {summary['already']} held, "
                     f"{summary['skipped_repackaging']} repackagings skipped, "
                     f"{summary['other_artist']} tracks by other artists skipped"
                     + ("" if summary["complete"] else ", CATALOGUE INCOMPLETE"), commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return summary
