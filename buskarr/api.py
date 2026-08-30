"""A JSON API, for callers that are not a browser.

buskarr's own interface is HTML forms behind a sign-in proxy, which is right
for a person and useless to a companion service asking on someone's behalf.
This module is that second door.

It is **off unless ``BUSKARR_API_KEY`` is set**, and every route requires that
key. An unauthenticated mutation API is not a safe default to ship in a
package other people deploy, and a key that must be chosen deliberately is the
only default that cannot be left switched on by accident.

The API does not fetch or place anything itself. Adds become ``jobs`` rows and
``wants`` rows exactly as the HTML forms' do, and the worker acts on them --
invariant 6, and the reason a discography add returns immediately rather than
holding a request open for the five minutes the walk takes.

Vocabulary stays buskarr's: things are *added* and then *wanted*. A caller
that calls them requests can say so at its own edge.
"""
import os
import secrets

from fastapi import APIRouter, Body, Header, HTTPException, Query

from . import bulk, catalog, db

API_VERSION = 1

#: Shared secret. Unset means the API is not mounted at all.
API_KEY = os.environ.get("BUSKARR_API_KEY", "").strip()

#: What can be asked for. An artist is a whole discography, an album a release,
#: a track one song -- buskarr's own three add paths, nothing new.
UNITS = ("artist", "album", "track")

router = APIRouter(prefix="/api/v1")


def _authorise(key: str | None) -> None:
    """Every route's first line. A wrong or missing key is a 401, not a hint."""
    if not API_KEY:
        # Unreachable while the router is only mounted with a key set. Kept so
        # that mounting it another way cannot silently open the door.
        raise HTTPException(status_code=503, detail="API is not enabled.")
    # Constant-time, because this package is public and the comparison is the
    # only thing between a caller and the library.
    if not key or not secrets.compare_digest(key, API_KEY):
        raise HTTPException(status_code=401, detail="Bad or missing API key.")


def _nudge() -> bool:
    """Ask the worker to start now rather than at the end of its sleep."""
    try:
        open(os.environ.get("NUDGE_FILE", "/state/nudge"), "w").close()
        return True
    except OSError:
        return False


@router.get("/capabilities")
def capabilities(x_api_key: str | None = Header(default=None)) -> dict:
    """What this buskarr can be asked for."""
    _authorise(x_api_key)
    return {
        "version": API_VERSION,
        "units": list(UNITS),
        "sources": list(catalog.DEFAULT_ORDER),
    }


@router.get("/search")
def search(q: str = Query(""), unit: str = Query("track"),
           limit: int = Query(20, ge=1, le=50),
           x_api_key: str | None = Header(default=None)) -> dict:
    """Catalogue search for one kind of thing.

    One unit per call rather than all three at once. The HTML page fans out
    because a person typing a name usually has not decided which of the three
    they meant; a caller that asked for albums has decided.
    """
    _authorise(x_api_key)
    if unit not in UNITS:
        raise HTTPException(status_code=400, detail=f"unit must be one of {UNITS}")
    query = q.strip()
    if not query:
        return {"version": API_VERSION, "unit": unit, "query": "", "results": []}
    return {"version": API_VERSION, "unit": unit, "query": query,
            "results": _search(unit, query, limit)}


def _search(unit: str, query: str, limit: int) -> list[dict]:
    """Every source that can answer for this unit, merged.

    A source that raises is skipped rather than failing the search. These are
    three third-party catalogues and any of them can be down or rate-limiting;
    returning the two that answered beats returning nothing.
    """
    if unit == "track":
        per_source = []
        for name in catalog.DEFAULT_ORDER:
            source = catalog.get(name)
            try:
                per_source.append(source.search_tracks(query, limit))
            except Exception:
                continue
        return [_track_row(t) for t in catalog.merge_tracks(per_source)[:limit]]

    out: list[dict] = []
    # Artists lead with MusicBrainz: it is the only source carrying
    # editor-written disambiguation prose, which is the whole difficulty in
    # telling two same-named artists apart.
    order = catalog.ARTIST_ORDER if unit == "artist" else catalog.DEFAULT_ORDER
    for name in order:
        source = catalog.get(name)
        method = getattr(source, f"search_{unit}s", None)
        if method is None:
            continue
        try:
            rows = method(query, limit)
        except Exception:
            continue
        out.extend(_artist_row(r) if unit == "artist" else _album_row(r)
                   for r in rows)
    return out[:limit]


def _artist_row(row: dict) -> dict:
    return {"unit": "artist", "source": row.get("source"), "ref": row.get("ref"),
            "name": row.get("name") or "", "hint": row.get("hint") or "",
            "releases": row.get("releases"), "listeners": row.get("listeners")}


def _album_row(row: dict) -> dict:
    return {"unit": "album", "source": row.get("source"), "ref": row.get("ref"),
            "title": row.get("title") or "", "artist": row.get("artist") or "",
            "kind": row.get("kind"), "tracks": row.get("tracks")}


def _track_row(row: dict) -> dict:
    return {"unit": "track", "source": row.get("source"),
            "artist": row.get("artist") or "", "title": row.get("title") or "",
            "album": row.get("album") or "", "year": row.get("year"),
            "duration": row.get("duration"),
            "sources": sorted(row.get("sources") or [])}


@router.post("/add")
def add(unit: str = Body(..., embed=True),
        ref: str = Body("", embed=True),
        source: str = Body("deezer", embed=True),
        artist: str = Body("", embed=True),
        title: str = Body("", embed=True),
        album: str = Body("", embed=True),
        year: str = Body("", embed=True),
        duration: float | None = Body(None, embed=True),
        requested_by: str = Body("", embed=True, alias="requestedBy"),
        x_api_key: str | None = Header(default=None)) -> dict:
    """Add one artist, album or track.

    Returns a `reference` the caller keeps and hands back to `/state`. It is
    not a want id for the bulk units: an artist add produces hundreds of wants
    and the thing that has a single state is the job that creates them.
    """
    _authorise(x_api_key)
    if unit not in UNITS:
        raise HTTPException(status_code=400, detail=f"unit must be one of {UNITS}")

    conn = db.connect()
    try:
        if unit == "track":
            if not artist.strip() or not title.strip():
                raise HTTPException(
                    status_code=400, detail="A track needs an artist and a title.")
            want_id, created = db.add_want(
                conn, artist.strip(), title.strip(), album.strip() or None,
                year.strip() or None, duration, requested_by or None)
            db.log_event(conn, "added" if created else "already-wanted",
                         f"{artist} - {title}", requested_by or "api")
            _nudge()
            return {"version": API_VERSION, "ok": True, "unit": unit,
                    "reference": f"want:{want_id}", "created": created,
                    "message": ("Added." if created else "Already on the list.")}

        if not ref.strip():
            raise HTTPException(
                status_code=400, detail=f"An {unit} add needs a catalogue ref.")
        label = (f"everything by {artist or ref}" if unit == "artist"
                 else f"the album {title or ref}")
        job_id = db.add_job(conn, unit, ref.strip(), source, label,
                            requested_by or None)
        db.log_event(conn, "queued-add", label, f"{unit} from {source}")
        _nudge()
        return {"version": API_VERSION, "ok": True, "unit": unit,
                "reference": f"job:{job_id}", "created": True,
                "message": f"Queued {label}."}
    finally:
        conn.close()


@router.get("/state")
def state(reference: str = Query(...),
          x_api_key: str | None = Header(default=None)) -> dict:
    """How far along one add is.

    `have` and `total` are tracks, for every unit. A single track is one of
    one, which means a caller can report progress the same way whatever was
    asked for.
    """
    _authorise(x_api_key)
    kind, _, ident = reference.partition(":")
    conn = db.connect()
    try:
        if kind == "want":
            return _want_state(conn, ident)
        if kind == "job":
            return _job_state(conn, ident)
    finally:
        conn.close()
    raise HTTPException(status_code=400,
                        detail="reference must be want:<id> or job:<id>")


def _want_state(conn, ident: str) -> dict:
    row = conn.execute("SELECT status FROM wants WHERE id=?", (ident,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such want.")
    have = 1 if row["status"] == db.STATUS_HAVE else 0
    return {"version": API_VERSION, "state": row["status"], "have": have,
            "total": 1, "message": _WANT_MESSAGES.get(row["status"], row["status"])}


#: buskarr's want statuses, said in a way a listener can act on.
_WANT_MESSAGES = {
    db.STATUS_PENDING: "Waiting to be searched for.",
    db.STATUS_SEARCHING: "Searching.",
    db.STATUS_HAVE: "In the library.",
    db.STATUS_UNAVAILABLE: "No provider has it.",
    db.STATUS_FAILED: "The download failed.",
}


def _job_state(conn, ident: str) -> dict:
    job = conn.execute(
        "SELECT status, label, batch, detail FROM jobs WHERE id=?",
        (ident,)).fetchone()
    if job is None:
        raise HTTPException(status_code=404, detail="No such job.")
    if job["status"] in (db.JOB_QUEUED, db.JOB_RUNNING):
        # No batch exists until the job finishes writing one, so there is
        # genuinely nothing to count yet. Saying "0 of 0" would read as a
        # failed add rather than one that has not started.
        return {"version": API_VERSION, "state": job["status"], "have": 0,
                "total": None,
                "message": ("Queued." if job["status"] == db.JOB_QUEUED
                            else "Adding tracks now.")}
    if job["status"] == db.JOB_ERROR:
        return {"version": API_VERSION, "state": db.JOB_ERROR, "have": 0,
                "total": 0, "message": job["detail"] or "The add failed."}

    counts = conn.execute(
        "SELECT COUNT(*) total, SUM(CASE WHEN status=? THEN 1 ELSE 0 END) have "
        "FROM wants WHERE batch=?", (db.STATUS_HAVE, job["batch"])).fetchone()
    total = int(counts["total"] or 0)
    have = int(counts["have"] or 0)
    return {"version": API_VERSION,
            "state": "have" if total and have >= total else "pending",
            "have": have, "total": total,
            "message": (f"{have} of {total} tracks in the library."
                        if total else "The add produced no tracks.")}


@router.post("/cancel")
def cancel(reference: str = Body(..., embed=True),
           x_api_key: str | None = Header(default=None)) -> dict:
    """Stop looking for something. Never removes audio already on disk.

    A batch cancel keeps rows the worker has already satisfied and rows it is
    part-way through -- `db.cancel_batch` deletes only what is still pending,
    which is what makes this safe to call while a cycle is running.
    """
    _authorise(x_api_key)
    kind, _, ident = reference.partition(":")
    conn = db.connect()
    try:
        if kind == "want":
            cur = conn.execute(
                "DELETE FROM wants WHERE id=? AND status IN (?,?)",
                (ident, db.STATUS_PENDING, db.STATUS_UNAVAILABLE))
            conn.commit()
            return {"version": API_VERSION, "removed": cur.rowcount > 0,
                    "message": ("Stopped looking for it." if cur.rowcount
                                else "It was already downloaded; nothing removed.")}
        if kind == "job":
            job = conn.execute("SELECT batch FROM jobs WHERE id=?",
                               (ident,)).fetchone()
            if job is None:
                raise HTTPException(status_code=404, detail="No such job.")
            if not job["batch"]:
                # Queued or running: there are no wants to cancel yet. Deleting
                # the job row is the whole of it.
                cur = conn.execute("DELETE FROM jobs WHERE id=? AND status=?",
                                   (ident, db.JOB_QUEUED))
                conn.commit()
                return {"version": API_VERSION, "removed": cur.rowcount > 0,
                        "message": ("Removed from the queue." if cur.rowcount
                                    else "It has already started; let it finish.")}
            removed, kept = db.cancel_batch(conn, job["batch"])
            return {"version": API_VERSION, "removed": removed > 0,
                    "message": f"Stopped looking for {removed} tracks. "
                               f"{kept} already downloaded were kept."}
    finally:
        conn.close()
    raise HTTPException(status_code=400,
                        detail="reference must be want:<id> or job:<id>")
