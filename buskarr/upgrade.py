"""Upgrade sweep — replace held lossy files with better copies of the same recording.

``match.is_upgrade`` has held the policy since the beginning (codec class first, then a bitrate
gain big enough to be worth the churn) and the Overview page has always counted the candidates;
this is the missing driver. It is a maintenance module like fold/refile/dedupe — dry-run by
default — rather than part of the acquisition loop, because an unattended sweep spends provider
quota and bandwidth on material that is already playable.

The rules that keep it safe:

  * The original is MOVED to ``UPGRADE_DIR``, never deleted. Any mistake is reversible with mv.
  * The new copy is placed BEFORE the original is quarantined, so no failure order leaves the
    library without a playable copy of the song.
  * Duration is vetted TIGHT (±4%) against the file on disk, not against the catalogue — the
    existing audio is the ground truth for which recording this want means.
  * The download is probed and must beat the original on its own MEASURED facts. A provider's
    claimed bitrate is advertising; a 96k stream in a FLAC container is not an upgrade.
  * Soulseek is excluded: its fetch is asynchronous, so a sweep cannot verify what it queued.
    The acquisition loop and harvest own that path.

A partial failure (quarantine or database write dies mid-row) is left for scan/reconcile, which
already repair both directions: a vanished old path re-points the want, an unindexed new file is
picked up by the next rescan.

Unlike its siblings, the dry run is not free — finding out whether a better copy exists costs one
provider search per file — so the default ``--limit`` is 25, worst files first. ``--limit 0``
sweeps everything.
"""
import os

from . import db, match, providers, scan, worker

UPGRADE_DIR = os.environ.get("UPGRADE_DIR", "/music/.upgrade_originals")
DEFAULT_LIMIT = 25


def log(msg):
    print(msg, flush=True)


def candidates(conn, artist=None, limit=DEFAULT_LIMIT):
    """Lossy-file-backed satisfied wants, worst quality first.

    Wants-driven rather than files-driven: the want carries the credit string that provider
    search and vetting key on, while a file's own tags are exactly what ``repair`` exists to
    distrust.
    """
    sql = ("SELECT w.id AS wid, w.artist AS artist, w.title AS title, w.album AS album,"
           " w.year AS year, w.artist_lead AS artist_lead, w.file_path AS path,"
           " w.track_no AS want_track,"
           " f.codec AS codec, f.bitrate AS bitrate, f.sample_rate AS sample_rate,"
           " f.bit_depth AS bit_depth, f.duration AS fdur, f.track_no AS track_no"
           " FROM wants w JOIN files f ON f.path = w.file_path"
           " WHERE w.status=? AND f.lossless=0 AND f.duration IS NOT NULL")
    args = [db.STATUS_HAVE]
    if artist:
        sql += " AND w.artist_lead=?"
        args.append(artist)
    sql += " ORDER BY f.bitrate ASC LIMIT ?"
    args.append(limit if limit else -1)
    return conn.execute(sql, args).fetchall()


def _attempt(conn, r, provs, dry_run, log):
    """Try to upgrade one file. Returns 'upgraded', 'planned', or 'nothing'."""
    old_path = r["path"]
    if not os.path.exists(old_path) or not worker.within_library(old_path):
        return "nothing"
    existing = {"codec": r["codec"], "bitrate": r["bitrate"],
                "sample_rate": r["sample_rate"], "bit_depth": r["bit_depth"]}
    # The file's probed duration, not the want's: the audio on disk is the recording being
    # replaced, and the catalogue's number can be a different edit of the same song.
    vet_want = {"artist": r["artist"], "title": r["title"], "duration": r["fdur"]}
    for entry in provs:
        p = entry["provider"]
        try:
            cands = p.search(vet_want)
        except providers.RateLimited:
            raise
        except Exception as e:
            log(f"    {p.name}: search error {type(e).__name__}")
            continue
        for cand in cands:
            if not match.is_upgrade(existing, cand):
                continue
            ok, why = match.vet(vet_want, cand, tight=True)
            if not ok:
                continue
            if dry_run:
                log(f"  would upgrade {r['artist']} - {r['title']}"
                    f"  [{r['codec']} {int((r['bitrate'] or 0) / 1000)}k -> "
                    f"{cand.get('codec')} via {p.name}]")
                return "planned"
            try:
                staged = p.fetch(cand, os.path.join(worker.STAGING, "upgrade"))
            except providers.RateLimited:
                raise
            except Exception as e:
                log(f"    {p.name}: fetch error {type(e).__name__}")
                continue
            if not staged or staged == "QUEUED":
                continue
            info = scan.probe(staged) or {}
            if not info.get("duration") \
                    or not match.duration_ok(r["fdur"], info["duration"], tight=True):
                log(f"    arrived at {info.get('duration')}s against {r['fdur']:.0f}s held; "
                    "discarded")
                worker._discard(staged)
                continue
            got = {"codec": info.get("codec"), "bitrate": info.get("bitrate"),
                   "sample_rate": info.get("sample_rate"), "bit_depth": info.get("bit_depth")}
            if not match.is_upgrade(existing, got):
                log(f"    arrived as {got['codec']} {int((got['bitrate'] or 0) / 1000)}k — no "
                    "better than what is held; discarded")
                worker._discard(staged)
                continue
            # track_no included so worker.want_track keeps the listing's number through the
            # replacement — otherwise the better copy adopts its own source release's position.
            want_d = {"artist": r["artist"], "title": r["title"], "album": r["album"],
                      "year": r["year"], "artist_lead": r["artist_lead"],
                      "track_no": r["want_track"]}
            track = info.get("tag_track") or r["track_no"]
            dest = worker.destination(want_d, os.path.splitext(staged)[1].lower(), track)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            final = worker.place(staged, dest)
            worker.tag(final, want_d, track)
            # Only now, with the better copy safely in the library, does the original move out.
            qdest = os.path.join(UPGRADE_DIR, os.path.relpath(old_path, worker.LIBRARY))
            os.makedirs(os.path.dirname(qdest), exist_ok=True)
            worker.place(old_path, qdest)
            conn.execute(
                "UPDATE wants SET provider=?, file_path=?, note=? WHERE id=?",
                (p.name, final,
                 f"upgraded {r['codec']} {int((r['bitrate'] or 0) / 1000)}k -> "
                 f"{got['codec']} {int((got['bitrate'] or 0) / 1000)}k", r["wid"]))
            conn.execute("DELETE FROM files WHERE path=?", (old_path,))
            conn.commit()
            db.log_event(conn, "upgraded", f"{r['artist']} - {r['title']}",
                         f"{r['codec']} -> {got['codec']} via {p.name}")
            log(f"  upgraded {r['artist']} - {r['title']}: {r['codec']} "
                f"{int((r['bitrate'] or 0) / 1000)}k -> {got['codec']} "
                f"{int((got['bitrate'] or 0) / 1000)}k  ({os.path.basename(final)})")
            return "upgraded"
    return "nothing"


def sweep(conn, dry_run=True, limit=DEFAULT_LIMIT, artist=None, provs=None, log=log):
    if provs is None:
        provs = [e for e in providers.enabled()
                 if e["available"] and e["name"] != "soulseek"]
        for e in provs:
            if e["name"] == "tidal":
                try:
                    e["provider"].refresh()
                except Exception as ex:
                    log(f"tidal: refresh failed ({type(ex).__name__}); trying the old token")
    rows = candidates(conn, artist, limit)
    if not rows:
        log("no lossy files behind satisfied wants — nothing to upgrade")
        return {"examined": 0, "upgraded": 0}
    if not provs:
        log("no synchronous providers available — nothing can be fetched")
        return {"examined": 0, "upgraded": 0}
    log(f"{len(rows)} lossy file(s) to try, worst first"
        + (f" (limit {limit}; --limit 0 for all)" if limit else "") + ":")
    upgraded = examined = 0
    for r in rows:
        examined += 1
        log(f"  {r['artist']} - {r['title']}  [{r['codec']} "
            f"{int((r['bitrate'] or 0) / 1000)}k]")
        try:
            outcome = _attempt(conn, r, provs, dry_run, log)
        except providers.RateLimited as e:
            log(f"!! throttled ({e}) — stopping the sweep, retry later")
            break
        except Exception as e:
            log(f"    error {type(e).__name__}: {e} — both copies left in place; rescan will "
                "settle the records")
            continue
        if outcome in ("upgraded", "planned"):
            upgraded += 1
    verb = "would be upgraded" if dry_run else "upgraded"
    log(f"\n{examined} examined, {upgraded} {verb}")
    if dry_run:
        log("dry run — nothing fetched or moved. Re-run with --apply.")
    elif upgraded:
        log(f"originals are under {UPGRADE_DIR} — restore any with mv. Rescan now so the files "
            "table matches.")
    return {"examined": examined, "upgraded": upgraded}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Replace held lossy files with better copies.")
    ap.add_argument("--apply", action="store_true", help="actually fetch and replace "
                    "(default is dry-run, which still costs one provider search per file)")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"files per run, worst first; 0 for all (default {DEFAULT_LIMIT})")
    ap.add_argument("--artist", default=None, help="limit to one lead artist")
    a = ap.parse_args()
    sweep(db.init(), dry_run=not a.apply, limit=a.limit, artist=a.artist)
