"""Album-level acquisition for the tracks no provider carries — Prowlarr plus the torrent
client behind it.

Torrent releases are album-granular, so this can never be a worker provider: a release title
cannot pass the per-track gate, and must not be allowed to. The flow is enqueue-and-harvest,
the same shape slskd already uses. This module GRABS a release — Prowlarr sends it to
qBittorrent under the ``buskarr`` category, whose save path is a directory ``harvest`` scans —
and harvest stays the only door into the library, so every file still clears the full per-track
vetting when the download completes hours later.

What qualifies: wants marked UNAVAILABLE (every direct provider struck out) that carry an
album, grouped per (lead, album). Album-less unavailable wants cannot be found this way; they
are counted so the answer is visible rather than silently absent.

Every grab is recorded in the ``grabs`` table, and an album with any prior grab is skipped
unless ``--regrab``: on private trackers ratio is real money, and a sweep that re-downloads the
same release weekly is a liability, not a feature.

Dry-run by default. The dry run performs the Prowlarr searches (that is the only way to know
what exists) but grabs nothing.
"""
import json
import os
import time
import urllib.parse
import urllib.request

from . import credit, db, match

PROWLARR_URL = os.environ.get("PROWLARR_URL", "http://prowlarr:9696")
PROWLARR_API_KEY = os.environ.get("PROWLARR_API_KEY", "")
QBIT_URL = os.environ.get("QBIT_URL", "http://qbittorrent:8080")
# Prowlarr's audio category; sub-categories (lossless etc.) are included automatically.
AUDIO_CATEGORY = 3000
MIN_SEEDERS = 1
# A plausible album is megabytes to a few gigabytes. Outside that is a single stray track or a
# discography dump, and a discography torrent for one album's worth of wants is bad ratio spent.
MIN_SIZE = 30 * 1024 * 1024
MAX_SIZE = 4 * 1024 * 1024 * 1024


def log(msg):
    print(msg, flush=True)


def available():
    return bool(PROWLARR_API_KEY)


def _api(path, body=None):
    req = urllib.request.Request(
        f"{PROWLARR_URL.rstrip('/')}/api/v1/{path}",
        headers={"X-Api-Key": PROWLARR_API_KEY, "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=60) as fh:
        raw = fh.read()
        return json.loads(raw) if raw else None


def _qbit(path, data=None):
    req = urllib.request.Request(
        f"{QBIT_URL.rstrip('/')}/api/v2/{path}",
        data=urllib.parse.urlencode(data).encode() if data else None)
    with urllib.request.urlopen(req, timeout=30) as fh:
        return fh.read().decode()


def adopt_torrents(qbit=_qbit, log=log):
    """Switch this category's torrents to automatic management. Returns how many needed it.

    Without this the category is only a LABEL: qBittorrent applies a category's save path solely
    under automatic torrent management, which is off by default and not something Prowlarr's
    grab can request — so every grab landed in the global default directory, which is not
    mounted here, and harvest could never see a byte of it. AutoTMM makes qbit relocate the
    data (finished or not) to the category path, and seeding continues from there.

    Idempotent, and run at the start of every sweep as well as after grabbing: a magnet that
    resolves slowly appears in the torrent list only after the sweep that grabbed it has ended,
    so the next run is what adopts it.
    """
    try:
        ts = json.loads(qbit("torrents/info?category=buskarr"))
    except Exception as e:
        log(f"qbittorrent unreachable ({type(e).__name__}) — grabbed torrents stay outside the "
            "harvest directories until a later run can adopt them")
        return 0
    strays = [t["hash"] for t in ts if not t.get("auto_tmm")]
    if strays:
        qbit("torrents/setAutoManagement", {"hashes": "|".join(strays), "enable": "true"})
        log(f"{len(strays)} torrent(s) switched to automatic management — qbittorrent moves "
            "them to the category save path")
    return len(strays)


def album_key(lead, album):
    return f"{db.norm(lead or '')}|{db.strict_norm(album or '')}"


def wanted_albums(conn):
    """[(lead, album, artist, missing)] for unavailable wants that carry an album, plus the
    count of album-less ones this path cannot help."""
    rows = conn.execute(
        "SELECT artist_lead AS lead, album, MIN(artist) AS artist, COUNT(*) AS missing"
        " FROM wants WHERE status=? AND album IS NOT NULL"
        " GROUP BY artist_lead, album ORDER BY missing DESC",
        (db.STATUS_UNAVAILABLE,)).fetchall()
    albumless = conn.execute(
        "SELECT COUNT(*) n FROM wants WHERE status=? AND album IS NULL",
        (db.STATUS_UNAVAILABLE,)).fetchone()["n"]
    return rows, albumless


def pick_release(releases, artist, album):
    """The single release worth grabbing, or None.

    Torrent-only (no usenet client is configured in Prowlarr), seeded, album-sized, and the
    title must carry both the artist and the album name — the same two gates acquisition uses,
    pointed at a release title instead of a track title. Lossless first, then seeders.
    """
    fit = []
    for r in releases:
        if (r.get("protocol") or "").lower() != "torrent":
            continue
        if (r.get("seeders") or 0) < MIN_SEEDERS:
            continue
        size = r.get("size") or 0
        if not MIN_SIZE <= size <= MAX_SIZE:
            continue
        title = r.get("title") or ""
        if not match.artist_ok(artist, title):
            continue
        if not match.title_ok(album, title, "", artist):
            continue
        fit.append(r)
    fit.sort(key=lambda r: (0 if "flac" in (r.get("title") or "").lower() else 1,
                            -(r.get("seeders") or 0)))
    return fit[0] if fit else None


def sweep(conn, dry_run=True, limit=0, regrab=False, api=_api, log=log):
    if not available():
        log("PROWLARR_API_KEY is not set — nothing can be searched")
        return {"albums": 0, "grabbed": 0}
    adopt_torrents(log=log)
    groups, albumless = wanted_albums(conn)
    if albumless:
        log(f"{albumless} unavailable want(s) carry no album and cannot be found this way")
    if not groups:
        log("no unavailable wants carry an album — nothing to grab")
        return {"albums": 0, "grabbed": 0}
    if limit:
        groups = groups[:limit]
    log(f"{len(groups)} album(s) with unavailable tracks, most-missing first:")
    grabbed = 0
    for g in groups:
        lead = credit.lead_artist(g["artist"])
        key = album_key(g["lead"], g["album"])
        prior = conn.execute("SELECT release_title, grabbed_at FROM grabs WHERE album_key=?"
                             " ORDER BY grabbed_at DESC LIMIT 1", (key,)).fetchone()
        if prior and not regrab:
            log(f"  {lead} — {g['album']} ({g['missing']} missing): already grabbed "
                f"{prior['release_title']!r}; --regrab to grab again")
            continue
        q = urllib.parse.urlencode({"query": f"{lead} {g['album']}",
                                    "categories": AUDIO_CATEGORY, "type": "search"})
        try:
            releases = api(f"search?{q}") or []
        except Exception as e:
            log(f"  {lead} — {g['album']}: search failed ({type(e).__name__})")
            continue
        best = pick_release(releases, lead, g["album"])
        if not best:
            log(f"  {lead} — {g['album']} ({g['missing']} missing): no fitting release "
                f"among {len(releases)} result(s)")
            continue
        size_gb = (best.get("size") or 0) / 1e9
        log(f"  {lead} — {g['album']} ({g['missing']} missing): "
            f"{'would grab' if dry_run else 'grabbing'} {best['title']!r} "
            f"[{best.get('indexer')}, {size_gb:.2f} GB, {best.get('seeders')} seeders]")
        if dry_run:
            grabbed += 1
            continue
        try:
            # Prowlarr's grab action: it pushes the release to its configured download client.
            api("search", {"guid": best["guid"], "indexerId": best["indexerId"]})
        except Exception as e:
            log(f"    grab failed ({type(e).__name__})")
            continue
        conn.execute(
            "INSERT INTO grabs (album_key, artist, album, release_title, indexer_id, guid,"
            " size, seeders, grabbed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (key, g["artist"], g["album"], best.get("title"), best.get("indexerId"),
             best.get("guid"), best.get("size"), best.get("seeders"), time.time()))
        # The wants stay UNAVAILABLE — only harvest may say otherwise — but the note tells the
        # Wanted page why waiting is the right move.
        conn.execute(
            "UPDATE wants SET note=? WHERE status=? AND artist_lead IS ? AND album=?",
            ("release grabbed via prowlarr — run harvest when the download completes",
             db.STATUS_UNAVAILABLE, g["lead"], g["album"]))
        conn.commit()
        db.log_event(conn, "grabbed", f"{lead} — {g['album']}",
                     f"{best.get('title')} [{best.get('indexer')}]")
        grabbed += 1
    log(f"\n{grabbed} release(s) {'would be grabbed' if dry_run else 'grabbed'}")
    if not dry_run and grabbed:
        # Catch what already resolved; anything slower is adopted by the next run.
        time.sleep(5)
        adopt_torrents(log=log)
    if dry_run:
        log("dry run — nothing sent to the download client. Re-run with --apply.")
    elif grabbed:
        log("when the downloads complete, run: python3 -m buskarr.harvest --apply")
    return {"albums": len(groups), "grabbed": grabbed}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="Grab album releases for unavailable tracks via Prowlarr.")
    ap.add_argument("--apply", action="store_true",
                    help="actually grab (default is dry-run; searches still run)")
    ap.add_argument("--limit", type=int, default=0, help="albums per run; 0 for all")
    ap.add_argument("--regrab", action="store_true",
                    help="grab even for albums with a recorded prior grab")
    a = ap.parse_args()
    sweep(db.init(), dry_run=not a.apply, limit=a.limit, regrab=a.regrab)
