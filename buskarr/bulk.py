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

from . import catalog, credit, db, match

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


def album_detail(album_id, source="deezer"):
    """Everything one catalogue album fetch can say: title, year, album artist, type, tracks.

    The album artist and release type exist so a caller can refuse an album before queueing it —
    a "Various Artists" compilation or a tribute act's release passes a title search happily and
    is only visible as wrong here.

    ``track_no`` is the listing sequence, deliberately not the source's per-disc position: album
    directories are flat, and per-disc numbering would give a two-disc release two "05" files.
    """
    if source == "itunes":
        d = _get(f"https://itunes.apple.com/lookup?id={album_id}&entity=song&limit=200")
        rows = d.get("results", [])
        head = next((r for r in rows if r.get("wrapperType") == "collection"), {})
        tracks = []
        for i, t in enumerate((r for r in rows if r.get("wrapperType") == "track"), 1):
            ms = t.get("trackTimeMillis")
            tracks.append({"title": t.get("trackName"), "artist": t.get("artistName"),
                           "duration": (ms / 1000.0) if ms else None, "track_no": i})
        return {"title": head.get("collectionName"),
                "year": (head.get("releaseDate") or "")[:4] or None,
                "artist": head.get("artistName"),
                # Apple has no primary type; a one-track collection is a single by definition.
                "kind": "single" if len(tracks) == 1 else None,
                "tracks": tracks}
    d = deezer(f"album/{album_id}")
    tracks = [{"title": t.get("title"), "artist": (t.get("artist") or {}).get("name"),
               "duration": float(t.get("duration") or 0) or None,
               "track_no": i}
              for i, t in enumerate(((d.get("tracks") or {}).get("data") or []), 1)]
    return {"title": d.get("title"), "year": (d.get("release_date") or "")[:4] or None,
            "artist": (d.get("artist") or {}).get("name"),
            "kind": d.get("record_type"), "tracks": tracks}


def album_tracks(album_id, source="deezer"):
    """Return (album_title, year, [track dicts]) for one album from a catalogue source."""
    d = album_detail(album_id, source)
    return d["title"], d["year"], d["tracks"]


def _get(url):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "buskarr/1.0"})
    with urllib.request.urlopen(req, timeout=45) as fh:
        return json.load(fh)


def add_album(conn, album_id, requested_by=None, allow_dup=False, source="deezer",
              listing=None, attribution=None, enrich=None, enrich_track=None):
    """Add every track of an album as an individual want, in one transaction.

    Fetched first, written second. Committing per track let the worker claim rows mid-add, past the
    point where ``db.cancel_batch`` could undo them.

    ``listing`` is an optional pre-fetched ``(title, year, tracks)`` triple \u2014 a caller that already
    fetched the album to vet it passes it here, or the two fetches can disagree.

    ``attribution`` is an optional ``(album, year)`` pair written to the wants in place of the
    fetched title and year. Album and year travel together \u2014 they describe one release, and mixing
    a caller's album with a fetched year is how one album splits across two directories.

    ``enrich`` is an optional want id given the same attribution inside the same transaction, so a
    crash cannot queue an album while leaving the song that seeded it album-less, or vice versa.
    ``enrich_track`` is that want's position on this release, when the caller located it.

    Tracks whose want already exists are enriched in place (NULL album, year and track number
    only). Without this, an already-pending want acquired later was numbered from whatever release
    the provider served \u2014 the completed directory then held the source release's number, not this
    album's.
    """
    title, year, tracks = listing if listing else album_tracks(album_id, source)
    if attribution:
        title, year = attribution
    if not tracks:
        return {"album": title, "year": year, "added": 0, "already": 0, "existing": 0,
                "total": 0, "batch": None, "source": source, "enriched": 0}
    batch = uuid.uuid4().hex[:10]
    label = f"album \u201c{title}\u201d ({source})"
    added = already = existing = enriched = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for t in tracks:
            if not t["title"] or not t["artist"]:
                continue
            # An edition-noise variant of a want that already exists comes back from add_want as
            # that want (created=False), so the enrichment and counting below cover it.
            wid, created = db.add_want(conn, t["artist"], t["title"], title, year,
                                       t["duration"], requested_by, allow_dup=allow_dup,
                                       batch=batch, batch_label=label,
                                       track_no=t.get("track_no"), commit=False)
            if not created and db.enrich_want(conn, wid, title, year,
                                              track_no=t.get("track_no"), commit=False):
                enriched += 1
            row = conn.execute("SELECT status FROM wants WHERE id=?", (wid,)).fetchone()
            if row and row["status"] == db.STATUS_HAVE:
                already += 1
            elif created:
                added += 1
            else:
                existing += 1
        if enrich and db.enrich_want(conn, enrich, title, year, track_no=enrich_track,
                                     commit=False):
            enriched += 1
        db.log_event(conn, "add-album", title,
                     f"{added} queued, {already} already held", commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"album": title, "year": year, "added": added, "already": already,
            "existing": existing, "total": len(tracks), "batch": batch, "source": source,
            "enriched": enriched}


# Identity of a recording across edition labels lives in db, so every caller shares one rule.
TWIN_DURATION = db.TWIN_DURATION
_word_fold = db.word_fold


def _remember_recording(seen, t):
    """Record a claimed listing track under both title folds, for ``_release_twin``."""
    for k in {db.norm(t["title"] or ""), _word_fold(t["title"] or "")}:
        seen.setdefault(k, []).append(t)


def _release_twin(seen, t):
    """A track already claimed in this add that IS this recording, or None.

    Same song under edition noise: ``db.norm`` drops the parenthetical and the remaster
    keywords, so "Redneck Shit (2016 Remaster)" reduces to the plain title. That is too loose on
    its own — it also equates "(live)" with the studio take — so the running times must agree as
    well. A different take is a different length and survives; a relabelled reissue of one master
    does not, which is what stopped a first-time artist add downloading its album, its deluxe and
    its greatest-hits in full.
    """
    dur = t.get("duration")
    if not dur:
        return None
    for k in {db.norm(t["title"] or ""), _word_fold(t["title"] or "")}:
        for prev in seen.get(k, []):
            if prev.get("duration") and abs(prev["duration"] - dur) <= TWIN_DURATION:
                return prev
    return None


# How many candidate releases to fetch and inspect per catalogue before concluding a song's album
# cannot be identified. Each costs one album fetch.
COMPLETE_CANDIDATES = 5
# Deezer first for the same reason it leads elsewhere; Apple fills its gaps. MusicBrainz is
# deliberately absent: it models an album as a release-group with many differing releases, and
# choosing one is exactly the guesswork that made an album-keyed tool unusable for this library.
COMPLETE_SOURCES = ("deezer", "itunes")


def _album_acceptable(detail, lead):
    """Would completing this release be completing the SEED's album? None if yes, else the reason.

    Compilations are refused outright: a track search resolves to whatever release is most popular,
    which for a well-known song is often a "Greatest Hits" — and completing that queues a tracklist
    nobody asked for. The album artist must be credited to the seed's lead for the same reason the
    artist add gates on it: a tribute act's album passes a title search happily.
    """
    if (detail.get("kind") or "").lower() == "compilation":
        return "a compilation"
    artist = detail.get("artist")
    if artist and not credit.credited_to(artist, lead):
        return f"an album by {artist!r}, not {lead!r}"
    return None


def _seed_position(tracks, seed):
    """Index of the listing track that IS the seed song, or None.

    ``match.vet`` rather than the ``add_want`` duplicate key: a listing spells the held song
    "Song (Remastered)" while the want says "Song", which are different wants by design — so
    without this check the completion would re-download the very track that seeded it. Ties are
    broken on duration, the strongest of the three signals.
    """
    fits = [i for i, t in enumerate(tracks) if match.vet(seed, t)[0]]
    if not fits:
        return None
    want_dur = seed.get("duration")
    if want_dur:
        fits.sort(key=lambda i: abs((tracks[i].get("duration") or want_dur) - want_dur))
    return fits[0]


def complete_album(conn, want_id, requested_by=None):
    """Queue the rest of the album one already-held song belongs to.

    Resolution is two-tier. When the want (or its file's tags) already names an album, a catalogue
    album search for it is trusted first — that respects the attribution deciding where the song is
    filed today. Otherwise a track search finds the song, and the release it belongs to comes with
    it. Either way the chosen release is fetched and must contain the seed song (``match.vet``)
    before anything is queued: an album title plus a same-named artist is not evidence enough, and
    feeding the gate the want's own metadata is exactly what the vetting invariant forbids.

    Returns a dict whose ``outcome`` is one of ``missing``, ``single``, ``none``, ``already`` or
    ``completed``. Raises when every catalogue call failed — an outage must finish the job as an
    error, not as "this song has no album".
    """
    want = conn.execute("SELECT * FROM wants WHERE id=?", (want_id,)).fetchone()
    if not want:
        return {"outcome": "missing"}
    lead = credit.lead_artist(want["artist"])
    album, year, duration = want["album"], want["year"], want["duration"]
    if want["file_path"]:
        # Scan writes album tags to the files table but never back onto wants, so a song adopted
        # from disk knows its album only here.
        f = conn.execute("SELECT album, year, duration FROM files WHERE path=?",
                         (want["file_path"],)).fetchone()
        if f:
            album, year, duration = album or f["album"], year or f["year"], \
                duration or f["duration"]
    seed = {"artist": want["artist"], "title": want["title"], "duration": duration}
    errors, searched = [], 0
    chosen = None                     # (source, ref, detail, attribution)

    def inspect(ref, source):
        """Fetch one candidate release; return its detail only if it can be completed."""
        try:
            detail = album_detail(ref, source)
        except Exception as e:
            errors.append(f"{source} album {ref}: {type(e).__name__}")
            return None
        why = _album_acceptable(detail, lead)
        if why:
            errors.append(f"skipped “{detail.get('title')}”: {why}")
            return None
        if _seed_position(detail["tracks"], seed) is None:
            errors.append(f"skipped “{detail.get('title')}”: it does not contain this song")
            return None
        return detail

    if album:
        for source in COMPLETE_SOURCES:
            try:
                cands = catalog.get(source).search_albums(f"{lead} {album}")
                searched += 1
            except Exception as e:
                errors.append(f"{source} album search: {type(e).__name__}")
                continue
            banded = []
            for c in cands:
                if c.get("artist") and not credit.credited_to(c["artist"], lead):
                    continue
                # strict_norm equality is a spelling difference of the SAME release, so the seed's
                # attribution is reused and the new tracks join its directory. Mere norm equality
                # ("Who I Am" against "Who I Am (Deluxe Edition)") is a different release with a
                # different tracklist: it is queued under its own true title rather than relabelled,
                # and the split is reported instead of papered over with false tags.
                if db.strict_norm(c["title"]) == db.strict_norm(album):
                    banded.append((0, c))
                elif db.norm(c["title"]) == db.norm(album):
                    banded.append((1, c))
            banded.sort(key=lambda bc: bc[0])
            for band, c in banded[:COMPLETE_CANDIDATES]:
                detail = inspect(c["ref"], source)
                if detail:
                    attribution = (album, year or detail["year"]) if band == 0 else None
                    chosen = (source, c["ref"], detail, attribution)
                    break
            if chosen:
                break

    if not chosen:
        for source in COMPLETE_SOURCES:
            try:
                hits = catalog.get(source).search_tracks(f"{lead} {want['title']}")
                searched += 1
            except Exception as e:
                errors.append(f"{source} track search: {type(e).__name__}")
                continue
            refs = []
            for h in hits:
                ref = h.get("album_ref")
                if not ref or ref in refs:
                    continue
                # The hit must BE the wanted song before its album means anything — the first
                # fuzzy search result is how a different song's album would get completed.
                if match.vet(seed, h)[0]:
                    refs.append(ref)
            qualifying = []
            for ref in refs[:COMPLETE_CANDIDATES]:
                detail = inspect(ref, source)
                if detail:
                    qualifying.append((ref, detail))
            if qualifying:
                # A song usually lives on both its album and a single; completing "the album"
                # means preferring the album, so the artist-add's release ranking applies.
                qualifying.sort(key=lambda rd: (
                    RELEASE_RANK.get((rd[1].get("kind") or "").lower(), RANK_UNKNOWN),
                    str(rd[1].get("year") or "9999")))
                ref, detail = qualifying[0]
                attribution = None
                if album and db.strict_norm(detail["title"]) == db.strict_norm(album):
                    attribution = (album, year or detail["year"])
                chosen = (source, ref, detail, attribution)
                break

    if not chosen:
        if not searched and errors:
            raise RuntimeError("no catalogue could be reached: " + "; ".join(errors[:4]))
        return {"outcome": "none", "errors": errors}

    source, ref, detail, attribution = chosen
    tracks = list(detail["tracks"])
    pos = _seed_position(tracks, seed)
    seed_track = None
    if pos is not None:
        # Popped, never renumbered: the remaining tracks keep their positions on the release, and
        # the seed's own position rides along so its file can be renumbered by refile if the
        # download that satisfied it came from a differently-ordered release.
        seed_track = tracks.pop(pos).get("track_no")
    if not tracks:
        return {"outcome": "single", "resolved": detail["title"], "source": source}
    r = add_album(conn, ref, requested_by, source=source,
                  listing=(detail["title"], detail["year"], tracks),
                  attribution=attribution, enrich=want_id, enrich_track=seed_track)
    r.update({"outcome": "completed" if r["added"] else "already",
              "resolved": detail["title"],
              "edition_differs": bool(album) and not attribution,
              "errors": errors})
    return r


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
    # Recordings this add has already claimed, keyed loosely so an edition-noise respelling of
    # one already claimed is recognisable. See _release_twin.
    seen_recordings = {}
    # One album, one spelling, one year, one directory. Keyed on strict_norm, never db.norm:
    # norm strips parentheticals, so "Bones in the Ocean (10 Year Anniversary Edition)" shared a
    # year slot with "Bones in the Ocean" and the edition was stamped with the base album's year.
    # Values are [spelling, year]. The first spelling seen wins; the first REAL year wins (not
    # first-sight setdefault — a dateless first edition would pin None forever, and later tracks
    # keeping their own years is exactly the "Who I Am (1994)" beside "Who I Am" split). Seeded
    # from wants that already exist for this artist, so a re-add whose source has since respelled
    # or re-dated a release joins the existing directory instead of opening a variant beside it —
    # one MusicBrainz respelling between two adds duplicated a whole release.
    album_known = {}
    if wanted:
        for r in conn.execute(
                "SELECT DISTINCT album, year FROM wants WHERE artist_lead=? AND album IS NOT NULL "
                "ORDER BY year IS NULL, album", (db.folder_key(wanted),)):
            album_known.setdefault(db.strict_norm(r["album"]), [r["album"], r["year"]])
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
                # Deliberately still counted against what is held ON DISK only. Counting tracks
                # claimed earlier in this same add tips a deluxe edition over the threshold, and
                # a release skipped here is skipped WHOLE — which discards its genuine bonus
                # tracks. Duplicate masters inside one add are the per-track twin check's job
                # below, which drops exactly the repeats and keeps everything new.
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
                # Edition noise is not a different song. strict_norm preserves parentheticals —
                # right for "(live)" and "(reprise)", wrong for "(2016 Remaster)",
                # "(Community version)" and "(A Tribute to …)", which named the SAME recording:
                # each queued its own want, and the provider returned one recording for all of
                # them. Duration is what separates the two cases, so a norm-equal title only
                # collapses when the running times agree; a genuinely different take survives.
                if _release_twin(seen_recordings, t):
                    continue
                seen_titles.add(key)
                _remember_recording(seen_recordings, t)
                # Every track that got here passed the credit gate, so the wanted artist IS the lead
                # credit — no separate inference needed, and none of the three sources has to plumb
                # a lead-artist field through. It decides the library folder at placement time.
                album = t.get("album") or rel_name or None
                year = t.get("year")
                if album:
                    entry = album_known.setdefault(db.strict_norm(album), [album, None])
                    # Album and year travel together — they describe one release — so adopting
                    # the known spelling adopts its year with it.
                    album = entry[0]
                    if year and not entry[1]:
                        entry[1] = year
                    year = entry[1] or year
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
