"""Deduce the track positions a catalogue title match could not supply.

``add_artist`` and ``add_album`` number a want from the release listing it was attributed to. That
covers most of a library, but leaves gaps: a censored title spelled differently by two sources
("Eatin' P***y" against "Eatin' Pussy"), a bonus track absent from the listing, a release the
catalogues only carry under another name. A want with no position falls back to the downloaded
file's own tag, which is its position on whatever release the PROVIDER served — for a song fetched
as a standalone single that is 1, which is how six songs in one directory all came to be 01.

The gaps are not guesswork, though, because the rest of the album is known. If the listing says
the album has positions 1..14 and thirteen of them are already claimed by numbered wants, the
fourteenth file can only be the position left over. Where several are missing at once, the
listing's own durations separate them: each free position has a running time, and an unnumbered
file whose length matches exactly one of them must be that track.

So this assigns only what is FORCED, by elimination:

  * an unnumbered want is paired with a free position only when their durations agree,
  * and the pair is committed only when it is the sole candidate on BOTH sides,
  * then the elimination is repeated, because each committed pair frees information for the next.

A want left with two possible positions keeps none. Inventing an order would put a number on a
file that means nothing, and a wrong number is worse than an absent one — it is indistinguishable
from a real position.

Positions are never overwritten, and never reused inside one directory.
"""
import os

from . import bulk, catalog, credit, db


def log(msg):
    print(msg, flush=True)


def listing_for(lead, album):
    """(source, detail) for the release this album is filed under, or (None, None).

    Requires a STRICT title match and a credit to the lead, the same gate album completion uses:
    numbering a directory from a differently-titled release is how one edition's positions get
    stamped onto another's tracklist.
    """
    for source in bulk.COMPLETE_SOURCES:
        try:
            cands = catalog.get(source).search_albums(f"{lead} {album}")
        except Exception:
            continue
        hit = next((c for c in cands
                    if db.strict_norm(c["title"]) == db.strict_norm(album)
                    and (not c.get("artist") or credit.credited_to(c["artist"], lead))), None)
        if not hit:
            continue
        try:
            detail = bulk.album_detail(hit["ref"], source)
        except Exception:
            continue
        if detail and detail.get("tracks"):
            return source, detail
    return None, None


def deduce(unnumbered, free_positions):
    """{want id: position} for every pairing forced by elimination.

    ``unnumbered`` is [(id, title, duration)]; ``free_positions`` is [(position, title, duration)].
    A pair is forced when it is the only duration-compatible option on both sides. Iterated, so
    committing one pair can force the next.
    """
    pending = {w[0]: w for w in unnumbered}
    free = {p[0]: p for p in free_positions}
    out = {}
    while pending and free:
        strong, weak = {}, {}
        for wid, (_, wtitle, wdur) in pending.items():
            # Title AND duration is the strong signal; duration alone is the fallback for a want
            # the listing spells differently. They are kept apart because they must not compete as
            # equals: a title match losing to a coincidental length match would assign the wrong
            # position, and length collisions are common inside one album.
            strong[wid] = [pos for pos, (_, ptitle, pdur) in free.items()
                           if db._same_recording(wtitle, ptitle, wdur, pdur)]
            weak[wid] = [pos for pos, (_, _pt, pdur) in free.items()
                         if wdur and pdur and abs(wdur - pdur) <= db.TWIN_DURATION]

        def force(lists):
            """The sole want/position pair in these candidate lists, or None."""
            for wid, fits in lists.items():
                if len(fits) == 1 and sum(1 for f in lists.values() if fits[0] in f) == 1:
                    return wid, fits[0]
            return None

        # Strong matches settle first; every one committed removes a position from the weak
        # candidates, which is what turns an under-determined set into a forced one.
        chosen = {wid: (strong[wid] or weak[wid]) for wid in pending}
        forced = force(strong) or force(chosen)
        if forced is None:
            break
        wid, pos = forced
        out[wid] = pos
        pending.pop(wid)
        free.pop(pos)
    return out


def plan(conn, artist=None, log=log):
    """[(want id, position, artist, album, title)] for every deducible missing position."""
    sql = ("SELECT artist_lead, album FROM wants WHERE album IS NOT NULL AND track_no IS NULL "
           "AND artist_lead IS NOT NULL")
    args = []
    if artist:
        sql += " AND artist_lead=?"
        args.append(artist)
    groups = conn.execute(sql + " GROUP BY artist_lead, album ORDER BY artist_lead, album",
                          args).fetchall()
    log(f"{len(groups)} (artist, album) group(s) with unnumbered wants")
    out, no_listing, undetermined = [], 0, 0
    for g in groups:
        lead, album = g["artist_lead"], g["album"]
        rows = conn.execute(
            "SELECT id, title, duration, track_no FROM wants WHERE artist_lead=? AND album=?",
            (lead, album)).fetchall()
        unnumbered = [(r["id"], r["title"], r["duration"]) for r in rows if r["track_no"] is None]
        claimed = {r["track_no"] for r in rows if r["track_no"]}
        source, detail = listing_for(lead, album)
        if not detail:
            no_listing += len(unnumbered)
            log(f"  {lead} / {album!r}: no strict listing, {len(unnumbered)} left alone")
            continue
        free = [(t["track_no"], t["title"], t.get("duration")) for t in detail["tracks"]
                if t.get("track_no") and t["track_no"] not in claimed]
        got = deduce(unnumbered, free)
        undetermined += len(unnumbered) - len(got)
        for wid, pos in sorted(got.items(), key=lambda kv: kv[1]):
            title = next(t for i, t, _ in unnumbered if i == wid)
            out.append((wid, pos, lead, album, title))
        log(f"  {lead} / {album!r}: {len(got)}/{len(unnumbered)} deduced from {source}")
    log(f"\n{len(out)} deducible, {undetermined} under-determined, {no_listing} with no listing")
    return out


def renumber(conn, artist=None, dry_run=True, log=log):
    moves = plan(conn, artist, log)
    if not moves:
        log("nothing further can be deduced")
        return {"numbered": 0}
    log(f"\n{len(moves)} want(s) to number:")
    n = 0
    for wid, pos, lead, album, title in moves:
        log(f"  {'would set' if dry_run else 'setting'} {pos:>3}  {lead} / "
            f"{os.path.basename(str(album))[:28]} / {title[:44]}")
        if dry_run:
            continue
        # Guarded on NULL: another process may have numbered it since the plan was built.
        n += conn.execute("UPDATE wants SET track_no=? WHERE id=? AND track_no IS NULL",
                          (pos, wid)).rowcount
    if not dry_run:
        conn.commit()
        db.log_event(conn, "renumber", artist, f"{n} want(s) numbered by elimination")
        log(f"\n{n} want(s) numbered. Run refile --apply, then rescan.")
    else:
        log("\ndry run — nothing written. Re-run with --apply.")
    return {"numbered": n if not dry_run else len(moves)}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="Deduce track positions the catalogue title match could not supply.")
    ap.add_argument("--apply", action="store_true", help="actually write (default is dry-run)")
    ap.add_argument("--artist", default=None, help="limit to one lead artist")
    a = ap.parse_args()
    renumber(db.init(), artist=a.artist, dry_run=not a.apply)
