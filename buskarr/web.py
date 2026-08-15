"""buskarr web UI — FastAPI, server-rendered, accessible by construction.

No SPA, no client framework, no build step. Every control is a real ``<button>`` or ``<a>``, every
input has a ``<label for>``, status regions are announced with ``aria-live``, and every page works
without JavaScript. That is not decoration: a screen-reader user is the primary user here.

Identity comes from oauth2-proxy's forwarded headers. The app binds to the traefik network with no
published port, so nothing but Traefik can reach it and those headers can be trusted.
"""
import concurrent.futures
import json
import os
import time
import urllib.parse
import urllib.request

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from . import bulk, catalog, db, match, providers, worker

LIBRARY = os.environ.get("LIBRARY_DIR", "/music")
DEEZER = "https://api.deezer.com"
NUDGE = os.environ.get("NUDGE_FILE", "/state/nudge")
app = FastAPI(title="buskarr")
# One-time schema init for this process; requests then open plain connections. Running the full
# init — schema DDL, a BEGIN IMMEDIATE migration, two backfills — on every request took the
# database's only write lock per page load, for work that can only ever act once per deploy.
db.init().close()

# Every state-changing route is a plain HTML form POST. SameSite cookies do not stop a *same-site*
# cross-origin post, and CORS does not apply to form submissions, so a page on any sibling
# subdomain could queue music or trigger a rescan under the signed-in session. Checking the
# origin of unsafe methods is the whole mitigation this app needs — it has no JS and no API clients.
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
ALLOWED_ORIGIN_HOSTS = {h.strip().lower() for h in
                        os.environ.get("ALLOWED_ORIGIN_HOSTS", "").split(",") if h.strip()}


def _origin_host(request: Request):
    raw = request.headers.get("origin") or request.headers.get("referer") or ""
    if not raw:
        return None
    return urllib.parse.urlsplit(raw).hostname or None


# Routes that delete audio. These are held to a stricter origin rule than the rest: the general
# allowance for a request carrying no provenance headers exists so an over-zealous privacy setup can
# still queue music, and that trade is not worth making for an irreversible delete.
DESTRUCTIVE_PATHS = ("/wants/artist/remove", "/library/artist/delete")


@app.middleware("http")
async def block_cross_origin_writes(request: Request, call_next):
    if request.method not in SAFE_METHODS:
        raw = request.headers.get("origin") or request.headers.get("referer") or ""
        host = _origin_host(request)
        expected = {h for h in
                    ((request.headers.get("x-forwarded-host") or request.url.hostname or "")
                     .split(",")[0].strip().lower(),) if h} | ALLOWED_ORIGIN_HOSTS
        # A missing Origin AND Referer is allowed: some privacy setups strip both, and a browser
        # cannot be made to send them cross-origin for a form post without also revealing itself.
        if host and expected and host.lower() not in expected:
            return PlainTextResponse(
                f"Refused: this looks like a cross-site request (from {host}).", status_code=403)
        # Deletions additionally require a header that is present AND parseable. "Origin: null" from
        # a sandboxed document parses to no hostname and would otherwise have sailed through the
        # check above, as would a request with no provenance at all.
        if request.url.path in DESTRUCTIVE_PATHS and not host:
            return PlainTextResponse(
                "Refused: a request that deletes audio must carry an Origin or Referer this host "
                f"recognises (got {raw!r}).", status_code=403)
    return await call_next(request)


def user_of(request):
    """User identity from oauth2-proxy, falling back to a marker when SSO is absent."""
    for h in ("x-forwarded-email", "x-forwarded-user", "x-auth-request-email"):
        v = request.headers.get(h)
        if v:
            return v
    return "anonymous"


def esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def human_bytes(n):
    n = n or 0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def page(title, body, user="", flash=""):
    """One shared shell. Skip link first, landmarks, and a live region for status messages."""
    flash_html = (f'<p id="flash" role="status" aria-live="polite" class="flash">{esc(flash)}</p>'
                  if flash else '<p id="flash" role="status" aria-live="polite"></p>')
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} — buskarr</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, sans-serif; line-height: 1.5; margin: 0 auto; max-width: 60rem;
         padding: 1rem; }}
  a.skip {{ position: absolute; left: -9999px; }}
  a.skip:focus {{ position: static; display: inline-block; padding: .5rem; }}
  nav ul {{ list-style: none; display: flex; gap: 1rem; padding: 0; flex-wrap: wrap; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #8884; }}
  form.inline {{ display: inline; }}
  .flash:not(:empty) {{ padding: .5rem .75rem; border: 2px solid currentColor; }}
  fieldset {{ border: 1px solid #8886; margin: 1rem 0; }}
  button {{ font: inherit; padding: .35rem .7rem; }}
  input[type=text] {{ font: inherit; padding: .35rem; min-width: 18rem; }}
  dl.stats {{ display: grid; grid-template-columns: auto 1fr; gap: .25rem 1rem; }}
  dl.stats dt {{ font-weight: 600; }}
  .muted {{ opacity: .75; }}
</style>
</head>
<body>
<a class="skip" href="#main">Skip to main content</a>
<header>
  <h1>buskarr</h1>
  <nav aria-label="Main"><ul>
    <li><a href="/">Overview</a></li>
    <li><a href="/search">Add music</a></li>
    <li><a href="/wants">Wanted</a></li>
    <li><a href="/library">Library</a></li>
    <li><a href="/activity">Activity</a></li>
  </ul></nav>
  <p class="muted">Signed in as {esc(user or 'unknown')}</p>
</header>
{flash_html}
<main id="main">
{body}
</main>
</body></html>""")


# --------------------------------------------------------------------------- overview

@app.get("/", response_class=HTMLResponse)
def overview(request: Request, msg: str = ""):
    conn = db.connect()
    s = db.library_stats(conn)
    provs = providers.enabled()
    want_rows = "".join(
        f"<tr><th scope='row'>{esc(k)}</th><td>{v}</td></tr>"
        for k, v in sorted(s["wants"].items())) or "<tr><td colspan='2'>No requests yet.</td></tr>"
    prov_rows = "".join(
        f"<tr><th scope='row'>{esc(p['name'])}</th>"
        f"<td>{'available' if p['available'] else 'not configured'}</td>"
        f"<td>{esc(p['hint'])}</td></tr>" for p in provs)
    body = f"""
<h2>Overview</h2>
<dl class="stats">
  <dt>Tracks on disk</dt><dd>{s['files']}</dd>
  <dt>Lossless</dt><dd>{s['lossless']} ({(s['lossless'] / s['files'] * 100) if s['files'] else 0:.0f}%)</dd>
  <dt>Worth upgrading</dt><dd>{s['upgradable']} <span class="muted">(lossy under 250 kbps)</span></dd>
  <dt>Artists</dt><dd>{s['artists']}</dd>
  <dt>Library size</dt><dd>{human_bytes(s['bytes'])}</dd>
</dl>

<h3>Wanted by status</h3>
<table><caption class="muted">Every entry is a single track, never an album.</caption>
<tbody>{want_rows}</tbody></table>

<h3>Providers</h3>
<table>
<caption class="muted">Tried in this order; the first candidate that passes vetting wins.</caption>
<thead><tr><th scope="col">Provider</th><th scope="col">Status</th>
<th scope="col">Typical quality</th></tr></thead>
<tbody>{prov_rows}</tbody></table>

<h3>Maintenance</h3>
<form method="post" action="/rescan">
  <button type="submit">Rescan library from disk</button>
</form>
<p class="muted">Reads tags and probes quality for every file. Runs in the background — the result
appears under Adds in progress on the Wanted page. Safe any time; it never writes to the
library.</p>
"""
    return page("Overview", body, user_of(request), msg)


@app.post("/rescan")
def rescan(request: Request):
    """Queue the scan for the worker rather than running it in the request.

    A settled library rescans in seconds, but the rescans that matter — after a harvest or a
    refile — probe hundreds of files over NFS and run for minutes, which is longer than every
    proxy in front of this app will wait. Same rule as bulk adds: the web process only enqueues.
    """
    conn = db.connect()
    db.add_job(conn, "rescan", LIBRARY, "disk", "library rescan", user_of(request))
    db.log_event(conn, "queued-rescan", None, user_of(request))
    note = ("Rescan queued — the worker picks it up within a couple of seconds. The result "
            "appears under Adds in progress on the Wanted page.")
    return RedirectResponse(f"/wants?msg={urllib.parse.quote(note)}", status_code=303)


# --------------------------------------------------------------------------- search

def _mode_radios(mode):
    opts = [("all", "Everything"), ("track", "Songs only"), ("album", "Albums only"),
            ("artist", "Artists only")]
    return "".join(
        f'<label><input type="radio" name="mode" value="{v}"'
        f'{" checked" if mode == v else ""}> {esc(lbl)}</label> '
        for v, lbl in opts)


SEARCH_WORKERS = 6
DISAMBIGUATION_TRACKS = 3
# Song rows rendered from one search. The cap is for the screen reader, not the server — /wants
# rendered in 18 ms and was still unusable at 789 controls. Reaching it is REPORTED: this printed
# "54 result(s)" above 30 rows and gave no sign the other 24 existed.
TRACK_RESULTS_SHOWN = 30


def _fanout(fn, items):
    """Map fn over items concurrently, preserving order. Exceptions come back as None."""
    def guard(x):
        try:
            return fn(x)
        except Exception:
            return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=SEARCH_WORKERS) as pool:
        return list(pool.map(guard, items))


def _all_sources(method, q, limit):
    """Call one search method on every catalogue source concurrently.

    A source that errors or times out contributes nothing rather than failing the page: catalogues
    are a convenience, and manual entry still works when every one of them is down.
    """
    names = catalog.DEFAULT_ORDER
    got = _fanout(lambda n: getattr(catalog.get(n), method)(q, limit), names)
    return {n: (r or []) for n, r in zip(names, got)}


def _source_note(per_source):
    dead = [n for n, r in per_source.items() if not r]
    if not dead:
        return ""
    return (f"<p class='muted'>No results from: {esc(', '.join(dead))}. "
            "Other sources are shown.</p>")


def _track_results(conn, q):
    per = _all_sources("search_tracks", q, 20)
    merged = catalog.merge_tracks(per.values())
    if not merged:
        return (_source_note(per) + "<p>No results from any catalogue. If the song is not listed "
                "anywhere, use <a href='#manual'>Add a song by name</a> below — that asks the "
                "download providers directly.</p>")
    shown = merged[:TRACK_RESULTS_SHOWN]
    rows = []
    for t in shown:
        dur = int(t["duration"] or 0)
        tol = match.acceptable_delta(t["duration"])
        held = db.find_file(conn, t["artist"], t["title"], t["duration"],
                            tolerance=tol if tol else 4.0)
        status = (f"on disk — {esc(held['codec'])} {int((held['bitrate'] or 0) / 1000)}k"
                  if held else "<span class='muted'>not in library</span>")
        hidden = (f'<input type="hidden" name="artist" value="{esc(t["artist"])}">'
                  f'<input type="hidden" name="title" value="{esc(t["title"])}">'
                  f'<input type="hidden" name="album" value="{esc(t.get("album") or "")}">'
                  f'<input type="hidden" name="year" value="{esc(t.get("year") or "")}">'
                  f'<input type="hidden" name="duration" value="{dur or ""}">')
        if held:
            action = (f'<form method="post" action="/want" class="inline">{hidden}'
                      f'<input type="hidden" name="allow_dup" value="1">'
                      f'<button type="submit">Add another copy of \u201c{esc(t["title"])}\u201d '
                      f'by {esc(t["artist"])} anyway</button></form>')
        else:
            action = (f'<form method="post" action="/want" class="inline">{hidden}'
                      f'<button type="submit">Add \u201c{esc(t["title"])}\u201d by '
                      f'{esc(t["artist"])}</button></form>')
        rows.append(f"<tr><td>{esc(t['artist'])}</td><td>{esc(t['title'])}</td>"
                    f"<td>{esc(t.get('album') or '')}</td>"
                    f"<td>{dur // 60}:{dur % 60:02d}</td>"
                    f"<td>{esc(', '.join(t['sources']))}</td>"
                    f"<td>{status}</td><td>{action}</td></tr>")
    # The caption carries the count, because that is what a screen reader announces on entering the
    # table, and it is the only place the cap can be stated where reaching it cannot go unnoticed.
    count = (f"{len(merged)} song(s)." if len(shown) == len(merged) else
             f"The first {len(shown)} of {len(merged)} song(s), best matches first.")
    return (_source_note(per) + "<h3>Songs</h3><table>"
            f"<caption class='muted'>{count} Searched Deezer, MusicBrainz and Apple together. "
            "Songs listed by more than one catalogue are shown first, since agreement is a better "
            "signal than any single catalogue's ranking.</caption>"
            "<thead><tr><th scope='col'>Artist</th><th scope='col'>Title</th>"
            "<th scope='col'>Album</th><th scope='col'>Length</th><th scope='col'>Listed by</th>"
            "<th scope='col'>In library?</th><th scope='col'>Action</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")


def _artist_results(conn, q):
    per = _all_sources("search_artists", q, 6)
    flat = [r for name in catalog.DEFAULT_ORDER for r in per[name]]
    if not flat:
        return (_source_note(per) + "<p>No artists matched any catalogue. Use "
                "<a href='#manual'>Add a song by name</a> below to add songs directly.</p>")
    # Fill in identifying titles for sources that give no descriptive hint of their own.
    needs = [r for r in flat if not r["hint"]]
    filled = _fanout(lambda r: catalog.get(r["source"]).known_for(r["ref"],
                                                                 DISAMBIGUATION_TRACKS), needs)
    for r, titles in zip(needs, filled):
        r["hint"] = ", ".join(titles) if titles else ""
    # Interleave by relevance within each catalogue, MusicBrainz first — it is the only source with
    # editor-written descriptions, which is the one thing that reliably separates same-named artists.
    for name in per:
        for i, r in enumerate(per[name]):
            r["rank"] = i
    flat.sort(key=lambda r: (r["rank"], catalog.ARTIST_ORDER.index(r["source"])
                             if r["source"] in catalog.ARTIST_ORDER else 9))
    rows = []
    for r in flat:
        hint = r["hint"] or "nothing listed to identify this one"
        extra = []
        if r.get("releases"):
            extra.append(f"{r['releases']} releases")
        if r.get("listeners"):
            extra.append(f"{r['listeners']} listeners")
        rows.append(
            f"<tr><td>{esc(r['name'])}</td><td>{esc(hint)}</td>"
            f"<td>{esc(r['source'])}</td><td>{esc(', '.join(extra))}</td><td>"
            f"<form method='post' action='/want/artist' class='inline'>"
            f"<input type='hidden' name='artist_id' value='{esc(r['ref'])}'>"
            f"<input type='hidden' name='source' value='{esc(r['source'])}'>"
            # Carried so the queued job can be labelled with a name before the catalogue is fetched.
            f"<input type='hidden' name='artist_name' value='{esc(r['name'])}'>"
            f"<button type='submit'>Add everything by {esc(r['name'])} "
            f"({esc(hint)}) from {esc(r['source'])}</button></form></td></tr>")
    return (_source_note(per) + "<h3>Artists</h3><table><caption class='muted'>Read the "
            "Description column before adding: different artists share names, and a catalogue will "
            "not always rank the one you mean first. MusicBrainz descriptions are written by "
            "editors for exactly this purpose, so they are listed first.</caption>"
            "<thead><tr><th scope='col'>Artist</th><th scope='col'>Description</th>"
            "<th scope='col'>Catalogue</th><th scope='col'>Size</th>"
            f"<th scope='col'>Action</th></tr></thead><tbody>{''.join(rows)}</tbody></table>")


def _album_results(conn, q):
    """Albums come from Deezer and Apple only.

    MusicBrainz models an album as a release-group with many differing releases, and choosing one is
    exactly the guesswork that made an album-keyed tool unusable for this library. Its recordings are
    reachable through artist search instead, where no such choice has to be made.
    """
    per = {"deezer": _safe(_deezer_albums, q, 10), "itunes": _safe(_itunes_albums, q, 10)}
    pairs = [(name, a) for name in ("deezer", "itunes") for a in per[name]]
    if not pairs:
        return _source_note(per) + "<p>No albums matched.</p>"
    listings = _fanout(lambda pair: bulk.album_tracks(pair[1]["id"], pair[0]), pairs)
    rows = []
    for (name, a), listing in zip(pairs, listings):
        titles = [t["title"] for t in (listing[2] if listing else []) if t.get("title")]
        opens = ", ".join(titles[:DISAMBIGUATION_TRACKS]) if titles else "no tracks listed"
        rows.append(
            f"<tr><td>{esc(a['artist'])}</td><td>{esc(a['title'])}</td>"
            f"<td>{esc(opens)}</td><td>{a.get('tracks') or '?'}</td><td>{esc(name)}</td><td>"
            f"<form method='post' action='/want/album' class='inline'>"
            f"<input type='hidden' name='album_id' value='{esc(str(a['id']))}'>"
            f"<input type='hidden' name='source' value='{esc(name)}'>"
            f"<input type='hidden' name='album_label' value='"
            f"{esc(a['title'])} by {esc(a['artist'])}'>"
            f"<button type='submit'>Add album \u201c{esc(a['title'])}\u201d by "
            f"{esc(a['artist'])}, starting {esc(opens)}</button></form></td></tr>")
    return (_source_note(per) + "<h3>Albums</h3><table><caption class='muted'>Adds one want per "
            "track. Tracks already held are marked as such rather than fetched, so a partly-owned "
            "album queues only what is missing.</caption>"
            "<thead><tr><th scope='col'>Artist</th><th scope='col'>Album</th>"
            "<th scope='col'>Opens with</th><th scope='col'>Tracks</th>"
            "<th scope='col'>Catalogue</th><th scope='col'>Action</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return []


def _itunes_albums(q, limit):
    req = urllib.request.Request(
        f"https://itunes.apple.com/search?term={urllib.parse.quote(q)}"
        f"&entity=album&limit={limit}", headers={"User-Agent": "buskarr/1.0"})
    with urllib.request.urlopen(req, timeout=30) as fh:
        data = json.load(fh)
    return [{"id": a["collectionId"], "title": a.get("collectionName") or "",
             "artist": a.get("artistName") or "", "tracks": a.get("trackCount")}
            for a in data.get("results", []) if a.get("collectionId")]


def _deezer_albums(q, limit):
    d = deezer_get(f"search/album?q={urllib.parse.quote(q)}&limit={limit}")
    return [{"id": a["id"], "title": a.get("title") or "",
             "artist": (a.get("artist") or {}).get("name") or "",
             "tracks": a.get("nb_tracks")} for a in d.get("data", [])]


def deezer_get(path):
    req = urllib.request.Request(f"{DEEZER}/{path}", headers={"User-Agent": "buskarr/1.0"})
    with urllib.request.urlopen(req, timeout=30) as fh:
        return json.load(fh)


def _search(conn, q, mode="all"):
    """One query, every catalogue, every kind of result.

    Artists, albums and songs are fetched concurrently rather than behind a mode switch: a search
    for a name is usually ambiguous about which of the three you meant, and making you guess first
    costs a whole page load to find out. Each section is a heading, so a screen reader can jump
    straight to the one that turned out to be relevant.

    The narrower modes remain because the full sweep costs more requests, and MusicBrainz is limited
    to one per second.
    """
    if mode in ("track", "album", "artist"):
        return {"track": _track_results, "album": _album_results,
                "artist": _artist_results}[mode](conn, q)

    def isolated(fn):
        """Run a renderer on its OWN connection.

        sqlite3 connections default to check_same_thread=True and are not safe to drive from several
        threads regardless, so the request's connection cannot be handed to the pool. Passing it made
        every default search render "track search failed: ProgrammingError" — the one renderer that
        touches the database. Do not "fix" that by setting check_same_thread=False; that permits the
        genuinely unsafe case instead of avoiding it.
        """
        def run(query):
            c = db.connect()
            try:
                return fn(c, query)
            finally:
                c.close()
        return run

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {kind: pool.submit(isolated(fn), q) for kind, fn in
                (("artist", _artist_results), ("album", _album_results),
                 ("track", _track_results))}
        out = {}
        for kind, fut in jobs.items():
            try:
                out[kind] = fut.result()
            except Exception as e:
                out[kind] = (f"<p role='alert'>{esc(kind)} search failed: "
                             f"{esc(type(e).__name__)}</p>")

    # Artists first: picking the right artist is the decision most often gone wrong, and getting it
    # right makes the album and song sections below unnecessary.
    nav = ("<nav aria-label='Result sections'><p>Jump to: "
           "<a href='#r-artist'>Artists</a> · <a href='#r-album'>Albums</a> · "
           "<a href='#r-track'>Songs</a></p></nav>")
    return (nav
            + f"<section id='r-artist'>{out['artist']}</section>"
            + f"<section id='r-album'>{out['album']}</section>"
            + f"<section id='r-track'>{out['track']}</section>")


@app.get("/search", response_class=HTMLResponse)
def search_form(request: Request, q: str = "", msg: str = "", mode: str = "all"):
    results = ""
    if q.strip():
        conn = db.connect()
        try:
            results = _search(conn, q, mode)
        except Exception as e:
            results = f"<p role='alert'>Search failed: {esc(type(e).__name__)}</p>"
    body = f"""
<h2>Add music</h2>
<form method="get" action="/search" role="search">
  <fieldset>
    <legend>Search the catalogues</legend>
    <p><label for="q">Artist and song</label><br>
    <input type="text" id="q" name="q" value="{esc(q)}" autocomplete="off"
           aria-describedby="qhelp"></p>
    <p id="qhelp" class="muted">Deezer, MusicBrainz and Apple are searched together. Deezer lists
    singles that release-oriented databases omit; MusicBrainz covers independent and regional
    artists that commercial catalogues leave out, and describes artists who share a name; Apple
    fills gaps in both. None of them gates what can be added — anything missing everywhere can
    still be typed in below.</p>
    <p><fieldset><legend>What to search for</legend>{_mode_radios(mode)}</fieldset></p>
    <p><button type="submit">Search</button></p>
  </fieldset>
</form>
{results}
<h3 id="manual">Add a song by name</h3>
<form method="post" action="/want">
  <fieldset>
    <legend>Straight to the download providers, no catalogue lookup</legend>
    <p id="manualhelp" class="muted">Use this when catalogue search cannot find the artist at all.
    Deezer is a commercial streaming catalogue, so independent and regional artists are often absent
    from it even when the download providers have them. Only an artist and a song title are
    needed.</p>
    <p><label for="m_artist">Artist</label><br>
    <input type="text" id="m_artist" name="artist" required aria-describedby="manualhelp"></p>
    <p><label for="m_title">Song title</label><br>
    <input type="text" id="m_title" name="title" required></p>
    <p><label for="m_album">Album (optional)</label><br>
    <input type="text" id="m_album" name="album"></p>
    <p><label for="m_year">Year (optional)</label><br>
    <input type="text" id="m_year" name="year" inputmode="numeric"></p>
    <p><button type="submit">Add this song</button></p>
  </fieldset>
</form>
"""
    return page("Add music", body, user_of(request), msg)


def _nudge():
    """Ask the worker to start a cycle now instead of at the end of its sleep. True if signalled.

    Every path that queues something wantable calls this, or the want sits idle for up to a full
    INTERVAL — half an hour on this deployment — with nothing in the UI to say so.
    """
    try:
        open(NUDGE, "w").close()
        return True
    except OSError:
        return False


# Appended to the confirmation of anything that queued a want, so the message never leaves the
# question of when it starts unanswered — that silence is what the nudge existed to fix.
NUDGED_NOTE = " Searching for it now — it appears under Activity within a minute or two."
UNNUDGED_NOTE = (" The worker could not be signalled, so the search starts on its next cycle, "
                 "within half an hour.")


@app.post("/search-now")
def search_now(request: Request):
    """Ask the worker for an early cycle. The web process never fetches anything itself."""
    note = ("Searching now — check Activity in a minute or two." if _nudge() else
            "Could not signal the worker. It searches on its next cycle, within half an hour.")
    db.log_event(db.connect(), "nudge", None, user_of(request) or "")
    return RedirectResponse(f"/wants?msg={urllib.parse.quote(note)}", status_code=303)


def _enqueue(request, kind, ref, source, label):
    """Queue a bulk add and nudge the worker. Redirects immediately — the fetch is not our job.

    Performing the add here meant the browser sat on a blank page for 6 to 15 seconds: a discography
    costs one HTTP request per release, and MusicBrainz enforces one request per second. Nudging the
    same file ``/search-now`` uses means the worker starts within its 5-second poll instead of waiting
    out INTERVAL, so the wanted list is usually filling in before the page is read.
    """
    conn = db.connect()
    db.add_job(conn, kind, ref, source, label, user_of(request))
    db.log_event(conn, "queued-add", label, f"{kind} from {source}")
    _nudge()                    # if it fails the worker still picks the job up on its next cycle
    note = (f"Adding {label} from {source} — the worker starts within a few seconds and adds "
            "tracks as it goes. Progress is under Adds in progress below; reload this page to "
            "see it advance.")
    return RedirectResponse(f"/wants?msg={urllib.parse.quote(note)}", status_code=303)


@app.post("/want/artist")
def add_artist_route(request: Request, artist_id: str = Form(...),
                     source: str = Form("deezer"), artist_name: str = Form("")):
    return _enqueue(request, "artist", artist_id, source,
                    f"everything by {artist_name or artist_id}")


@app.post("/want/album")
def add_album_route(request: Request, album_id: str = Form(...), source: str = Form("deezer"),
                    album_label: str = Form("")):
    return _enqueue(request, "album", album_id, source,
                    f"the album {album_label or album_id}")


@app.post("/want")
def create_want(request: Request, artist: str = Form(...), title: str = Form(...),
                album: str = Form(""), year: str = Form(""), duration: str = Form(""),
                allow_dup: str = Form("")):
    conn = db.connect()
    try:
        dur = float(duration) if duration else None
    except ValueError:
        dur = None
    wid, created = db.add_want(conn, artist, title, album or None, year or None, dur,
                              user_of(request), allow_dup=bool(allow_dup))
    db.log_event(conn, "added" if created else "already-wanted",
                 f"{artist} - {title}", user_of(request))
    row = conn.execute("SELECT status FROM wants WHERE id=?", (wid,)).fetchone()
    if not created:
        note = f"“{title}” by {artist} was already on the list."
    elif row and row["status"] == db.STATUS_HAVE:
        note = f"“{title}” by {artist} is already on disk, so nothing will be fetched."
    else:
        # The bulk paths have nudged since they moved off the request path; this one never did, so
        # a single added song was the only thing in the project that waited out a whole INTERVAL.
        note = ((f"Added “{title}” by {artist} as an extra copy — duplicate check bypassed."
                 if allow_dup else f"Added “{title}” by {artist}.")
                + (NUDGED_NOTE if _nudge() else UNNUDGED_NOTE))
    return RedirectResponse(f"/wants?msg={urllib.parse.quote(note)}", status_code=303)


# --------------------------------------------------------------------------- wants

# One discography add turns this list into hundreds of rows, and each row carries up to two buttons
# whose labels are full sentences so their accessible names stand alone. Unpaged that came to 184 KB
# and ~790 controls: the server rendered it in 18 ms, but a screen reader still has to build a virtual
# buffer over all of it, which is the part that actually felt slow.
WANTS_PER_PAGE = 100
WANTS_PAGE_SIZES = (25, 50, 100, 250)
# A ceiling on ?per=, so a hand-edited URL cannot ask for a page nothing can read. Stated when hit,
# never applied silently.
WANTS_MAX_PER = 500
WANTS_MIN_PER = 10
# Two ways to read the same list: one row per song, or one row per lead artist.
WANTS_GROUPS = ("track", "artist")
# The activity log is deliberately a tail rather than a paged history, but the page has
# to say so — an unstated cap reads as "this is everything".
ACTIVITY_LIMIT = 200


def _wants_url(**params):
    """A /wants link with empty values dropped — omitting a parameter returns it to its default.

    The path is always included. A bare query string is not enough and an empty one is actively
    wrong: ``href=""`` resolves to the current URL *including* its query, so "all statuses" would do
    nothing when followed from a filtered page.
    """
    kept = {k: v for k, v in params.items() if v not in ("", None)}
    return "/wants" + (("?" + urllib.parse.urlencode(kept)) if kept else "")


def _artist_groups(conn, status=""):
    """Wanted songs collapsed to one row per LEAD artist.

    Grouped on ``artist_lead`` rather than the credit, so a collaboration counts towards the artist
    it is filed under instead of appearing as a separate act.
    """
    # Counts are deliberately NOT status-filtered, even when a status filter is active: the remove
    # button deletes every want for the artist regardless of status, so a filtered count in its
    # label would promise "remove 2" and then remove twelve. The filter decides which artists are
    # LISTED; the numbers always describe the whole artist.
    sql = ("SELECT artist_lead AS lead, COUNT(*) total, SUM(status=?) pending, "
           "SUM(status=?) have, SUM(status=?) unavailable, "
           "SUM(status=? AND provider IS NOT NULL) downloaded FROM wants "
           "GROUP BY artist_lead")
    args = [db.STATUS_PENDING, db.STATUS_HAVE, db.STATUS_UNAVAILABLE, db.STATUS_HAVE]
    if status:
        sql += " HAVING SUM(status=?) > 0"
        args.append(status)
    sql += " ORDER BY artist_lead COLLATE NOCASE"
    return conn.execute(sql, args).fetchall()


def _artist_group_block(conn, status, per):
    rows = _artist_groups(conn, status)
    if not rows:
        return "<p>Nothing wanted yet. Use <a href='/search'>Add music</a>.</p>"
    trs = []
    for i, r in enumerate(rows):
        lead = r["lead"] or "Unknown"
        view = _wants_url(group="track", artist=r["lead"], status=status,
                          per=per if per != WANTS_PER_PAGE else None)
        # Each checkbox gets its own id and a real <label for>, and the label states the count so it
        # is meaningful when read on its own.
        # The label names the artist. Dozens of rows each reading "also delete the downloaded
        # files" are indistinguishable in a screen reader's list of form controls.
        box = (f"<input type='checkbox' id='df{i}' name='delete_files' value='1'>"
               f"<label for='df{i}'> also delete the {r['downloaded']} downloaded "
               f"file(s) for {esc(lead)}</label>"
               if r["downloaded"] else
               "<span class='muted'>no downloaded files</span>")
        trs.append(
            f"<tr><td><a href='{esc(view)}'>{esc(lead)}</a></td>"
            f"<td>{r['total']}</td><td>{r['pending']}</td><td>{r['have']}</td>"
            f"<td>{r['unavailable']}</td><td>"
            f"<form method='post' action='/wants/artist/remove' class='inline'>"
            f"<input type='hidden' name='artist' value='{esc(lead)}'>"
            f"<input type='hidden' name='status' value='{esc(status)}'>"
            f"{box} "
            f"<button type='submit'>Remove all {r['total']} wanted song(s) by {esc(lead)}"
            f"</button></form></td></tr>")
    return (f"<table><caption>Wanted songs by artist — {len(rows)} artist(s)"
            + (f", listing only those with something {esc(status)}" if status else "")
            + ". Counts and removals cover every wanted song by the artist, whatever the filter. "
            "Ticking the box deletes the audio as well, and asks for confirmation first."
            "</caption>"
            "<thead><tr><th scope='col'>Artist</th><th scope='col'>Wanted songs</th>"
            "<th scope='col'>Still pending</th><th scope='col'>In library</th>"
            "<th scope='col'>Unavailable</th><th scope='col'>Action</th></tr></thead>"
            f"<tbody>{''.join(trs)}</tbody></table>")


def _page_links(status, artist, per, page_no, pages, total, first, last):
    """(previous, next) anchors, or empty strings at the ends of the list.

    Each label says where it goes and how far through the list that is. "Next" and "2" are useless as
    accessible names — read out of context, which is how a screen reader user meets them in a list of
    links, they say nothing about what they do.

    ``page_no`` rather than ``page`` throughout this module: the latter is the HTML shell function.
    """
    prev_link = next_link = ""
    if page_no > 1:
        prev_first = (page_no - 2) * per + 1
        prev_link = (f"<a href='{esc(_wants_url(status=status, artist=artist, per=per, page=page_no - 1))}'>"
                     f"Previous page, wanted songs {prev_first} to {first - 1} of {total}</a>")
    if page_no < pages:
        next_last = min(total, last + per)
        next_link = (f"<a href='{esc(_wants_url(status=status, artist=artist, per=per, page=page_no + 1))}'>"
                     f"Next page, wanted songs {last + 1} to {next_last} of {total}</a>")
    return prev_link, next_link


JOB_WORDING = {
    db.JOB_QUEUED: "waiting for the worker to pick it up",
    db.JOB_RUNNING: "fetching the catalogue now",
    db.JOB_DONE: "finished",
    db.JOB_ERROR: "failed",
}


def _jobs_block(conn):
    """Bulk adds in flight and their results.

    An add is queued rather than performed in the request, so its outcome cannot arrive in the
    redirect's flash message any more — it has to live somewhere durable, and this is that place. No
    auto-refresh: a meta-refresh would rebuild a screen reader's buffer and move focus on every tick,
    which is worse than reloading deliberately.
    """
    jobs = db.recent_jobs(conn)
    if not jobs:
        return ""
    rows = "".join(
        f"<tr><td>{esc(j['label'] or j['ref'])}</td><td>{esc(j['source'])}</td>"
        f"<td>{esc(JOB_WORDING.get(j['status'], j['status']))}</td>"
        f"<td>{esc(j['detail'] or '')}</td></tr>" for j in jobs)
    live = db.jobs_in_flight(conn)
    caption = ("Adding a whole artist or album — or finding the album a song belongs to — takes "
               "several catalogue requests, so it runs in the background. "
               + (f"{live} still in progress — reload this page to see it advance."
                  if live else "Nothing in progress."))
    return ("<h3 id=\"adds\">Adds in progress</h3>"
            f"<table><caption class='muted'>{caption}</caption>"
            "<thead><tr><th scope='col'>Addition</th><th scope='col'>Catalogue</th>"
            "<th scope='col'>State</th><th scope='col'>Result</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>")


@app.get("/wants", response_class=HTMLResponse)
def wants(request: Request, msg: str = "", status: str = "", artist: str = "",
          group: str = "track",
          # Aliased: the URL parameter is ?page=, but a local named `page` would shadow the page()
          # shell this handler returns through.
          page_no: int = Query(1, alias="page"), per: int = WANTS_PER_PAGE):
    conn = db.connect()
    clauses, args = [], []
    if status:
        clauses.append("status=?")
        args.append(status)
    if artist:
        clauses.append("artist_lead=?")
        args.append(artist)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    # Counted separately from the page of rows so the total is the real total. The old handler took
    # 500 rows and said nothing, so 86 of 586 wants simply were not on the page.
    total = conn.execute(f"SELECT COUNT(*) n FROM wants{where}", args).fetchone()["n"]
    # Both ends reported, not just the ceiling: WANTS_MAX_PER promises limits are stated rather than
    # applied silently, and a ?per=5 was being raised to 10 without a word.
    asked_per = per
    per = max(WANTS_MIN_PER, min(per, WANTS_MAX_PER))
    capped = per != asked_per
    pages = max(1, -(-total // per))
    # Clamped rather than 404'd: a bookmarked page that has since fallen off the end should show the
    # last page, not an empty table.
    page_no = max(1, min(page_no, pages))
    offset = (page_no - 1) * per
    rows = conn.execute(
        f"SELECT * FROM wants{where} ORDER BY requested_at DESC LIMIT ? OFFSET ?",
        args + [per, offset]).fetchall()
    first, last = (offset + 1 if rows else 0), offset + len(rows)
    counts = {r["status"]: r["n"] for r in
              conn.execute("SELECT status, COUNT(*) n FROM wants GROUP BY status")}
    keep = dict(group=group if group != "track" else None, artist=artist or None,
                per=per if per != WANTS_PER_PAGE else None)
    filters = " ".join(
        f"<a href='{esc(_wants_url(status=k, **keep))}'>{esc(k)} ({v})</a>"
        for k, v in sorted(counts.items()))
    # The grouping toggle. The current view is <strong>, not a link to itself — a link that goes
    # nowhere is noise in a screen reader's links list.
    toggle = " ".join(
        f"<strong>grouped by {g}</strong>" if g == group else
        f"<a href='{esc(_wants_url(status=status, artist=artist or None, group=g if g != 'track' else None, per=per if per != WANTS_PER_PAGE else None))}'>"
        f"Group by {g}</a>" for g in WANTS_GROUPS)
    trs = []
    for w in rows:
        detail = w["note"] or ""
        if w["allow_dup"]:
            detail = (detail + " · extra copy (duplicate check off)").strip(" ·")
        trs.append(
            f"<tr><td>{esc(w['artist'])}</td><td>{esc(w['title'])}</td>"
            f"<td>{esc(w['status'])}</td><td>{esc(w['provider'] or '')}</td>"
            f"<td>{esc(detail)}</td><td>{esc(w['requested_by'] or '')}</td>"
            # Both labels name the ARTIST as well as the title. Two wants can share a title — a
            # cover, or the same song by two acts — and a screen reader's controls list shows nothing
            # but the accessible name, so title-only buttons were indistinguishable there.
            # A satisfied want gets neither retry nor remove: retrying one re-downloaded the
            # recording just to discard it against the existing copy, and the handler now refuses
            # that anyway. Its Action cell instead offers album completion — queue the rest of
            # whatever release this song came from.
            "<td>"
            + (f"<form method='post' action='/wants/{w['id']}/complete-album' class='inline'>"
               f"<button type='submit'>Get the rest of the album for “{esc(w['title'])}” by "
               f"{esc(w['artist'])}</button></form>"
               if w["status"] == db.STATUS_HAVE else
               f"<form method='post' action='/wants/{w['id']}/retry' class='inline'>"
               f"<button type='submit'>Retry “{esc(w['title'])}” by {esc(w['artist'])}</button>"
               "</form>")
            + ("" if w["status"] == db.STATUS_HAVE else
               f"<form method='post' action='/wants/{w['id']}/remove' class='inline'>"
               f"<button type='submit'>Remove “{esc(w['title'])}” by {esc(w['artist'])} "
               f"from the wanted list</button></form>")
            + "</td></tr>")
    # The caption carries the range, because that is what a screen reader announces on entering the
    # table — the paging controls above it have already been passed by then.
    scope = "".join([f", {esc(status)} only" if status else "",
                     f", by {esc(artist)} only" if artist else ""]) + ("," if status or artist else "")
    table = (f"<table><caption>Wanted songs{scope} {first} to {last} of {total}, "
             f"newest first.</caption>"
             "<thead><tr><th scope='col'>Artist</th><th scope='col'>Title</th>"
             "<th scope='col'>Status</th><th scope='col'>Provider</th><th scope='col'>Detail</th>"
             "<th scope='col'>Added by</th><th scope='col'>Action</th></tr></thead>"
             f"<tbody>{''.join(trs)}</tbody></table>"
             if trs else "<p>Nothing wanted yet. Use <a href='/search'>Add music</a>.</p>")
    prev_link, next_link = _page_links(status, artist, per, page_no, pages, total, first, last)
    sizes = " ".join(
        f"<strong>{n} per page</strong>" if n == per else
        f"<a href='{esc(_wants_url(status=status, artist=artist or None, per=n if n != WANTS_PER_PAGE else None))}'>"
        f"Show {n} per page</a>"
        for n in WANTS_PAGE_SIZES)
    pager = ""
    if trs:
        pager = ("<nav aria-label=\"Wanted list pages\">"
                 f"<p>Showing {first} to {last} of {total} wanted song"
                 f"{'' if total == 1 else 's'} — page {page_no} of {pages}."
                 + (f" A page size of {asked_per} was requested and adjusted to {per}, the "
                    f"allowed range being {WANTS_MIN_PER} to {WANTS_MAX_PER}." if capped else "")
                 + "</p>"
                 f"<p>{prev_link} {next_link}</p><p>{sizes}</p></nav>")
    # Repeated below the table as plain links, not a second landmark: having walked 100 rows, the
    # next page should be one keystroke away rather than a trip back to the top.
    pager_foot = (f"<p class='muted'>{prev_link} {next_link}</p>"
                  if trs and (prev_link or next_link) else "")
    # One list, two readings. Track view keeps the paging; artist view is one row per lead artist
    # and needs none — there are tens of artists, not hundreds of songs.
    if group == "artist":
        main_block = _artist_group_block(conn, status, per)
    else:
        main_block = f"{pager}\n{table}\n{pager_foot}"
    scoped = (f" &middot; showing only {esc(artist)} "
              f"<a href='{esc(_wants_url(status=status, group=group if group != 'track' else None))}'>"
              f"(clear the {esc(artist)} filter)</a>" if artist else "")
    jobs_block = _jobs_block(conn)
    batches = db.recent_batches(conn)
    if batches:
        brows = "".join(
            f"<tr><td>{esc(b['batch_label'] or b['batch'])}</td>"
            f"<td>{b['total']}</td><td>{b['pending']}</td><td>{b['have']}</td><td>"
            + (f"<form method='post' action='/wants/batch/{esc(b['batch'])}/cancel' class='inline'>"
               f"<button type='submit'>Undo {esc(b['batch_label'] or 'this add')}, "
               f"removing {b['cancellable']} want(s)</button></form>"
               if b["cancellable"] else
               "<span class='muted'>nothing left to undo — all downloaded</span>")
            + "</td></tr>"
            for b in batches)
        batch_block = (
            "<h3 id=\"batches\">Recent additions</h3>"
            "<table><caption class='muted'>Added a whole album or artist by mistake? Undoing "
            "removes the wants still waiting, and the ones that only recorded music you already "
            "owned. Nothing that was actually downloaded is touched, and no audio is ever deleted."
            "</caption><thead><tr><th scope='col'>Addition</th><th scope='col'>Tracks</th>"
            "<th scope='col'>Still pending</th><th scope='col'>In library</th>"
            f"<th scope='col'>Action</th></tr></thead><tbody>{brows}</tbody></table>")
    else:
        batch_block = ""
    body = f"""
<h2>Wanted</h2>
<p>{filters} <a href="{esc(_wants_url(**keep))}">all statuses</a></p>
<p>{toggle}{scoped}</p>
<form method="post" action="/search-now">
  <button type="submit">Search for wanted music now</button>
</form>
{jobs_block}
{main_block}
{batch_block}
"""
    return page("Wanted", body, user_of(request), msg)


@app.post("/wants/batch/{batch}/cancel")
def cancel_batch_route(request: Request, batch: str):
    conn = db.connect()
    label = conn.execute("SELECT batch_label FROM wants WHERE batch=? LIMIT 1",
                         (batch,)).fetchone()
    removed, kept = db.cancel_batch(conn, batch)
    db.log_event(conn, "cancel-batch", (label["batch_label"] if label else batch),
                 f"{removed} pending want(s) removed, {kept} left alone")
    note = (f"Cancelled {removed} pending want(s) from "
            f"{(label['batch_label'] if label else 'that add')}.")
    if kept:
        note += (f" {kept} kept: actually downloaded, or marked unavailable with a retry history "
                 "worth keeping. No audio was deleted.")
    return RedirectResponse(f"/wants?msg={urllib.parse.quote(note)}", status_code=303)


# Declared BEFORE /wants/{wid}/remove: Starlette matches in declaration order and
# "{wid}" happily matches the literal segment "artist", so the parameterised route claimed
# this URL and failed with a 422 int-parse error on "artist".
@app.post("/wants/artist/remove")
def remove_artist_wants(request: Request, artist: str = Form(...),
                        delete_files: str = Form(""), confirm: str = Form(""),
                        status: str = Form(""), path: list[str] = Form(default=[])):
    conn = db.connect()
    rows = conn.execute("SELECT * FROM wants WHERE artist_lead=?", (artist,)).fetchall()
    if not rows:
        return RedirectResponse(
            f"/wants?msg={urllib.parse.quote(f'Nothing wanted by {artist}.')}", status_code=303)
    # Only files this artist's wants actually fetched, and only those sitting in this artist's own
    # directory: a want records the path of an existing copy when it finds one, and that copy can
    # belong to somebody else entirely. The Library tab is where "delete this artist's music" lives.
    owned = [r["file_path"] for r in rows
             if r["file_path"] and r["provider"] and os.path.isfile(r["file_path"])
             and _owned_by(r["file_path"], artist)]
    if delete_files and owned and not confirm:
        return _confirm_page(
            request, "Confirm deletion", artist, owned, "/wants/artist/remove",
            {"artist": artist, "status": status},
            f"Removing all {len(rows)} wanted song(s) by {esc(artist)} from the wanted list, and "
            "deleting the audio those wants downloaded.")
    deleted, freed = [], 0
    if delete_files and owned:
        # Intersected with the reviewed list, so anything downloaded since the confirmation page
        # was rendered is left alone rather than deleted unseen.
        reviewed = set(_reviewed_paths(path))
        deleted, freed, _ = _delete_files(conn, [p for p in owned if p in reviewed], artist, artist)
    conn.execute("DELETE FROM wants WHERE artist_lead=?", (artist,))
    conn.commit()
    db.log_event(conn, "remove-artist", artist,
                 f"{len(rows)} want(s) removed, {len(deleted)} file(s) deleted")
    note = f"Removed {len(rows)} wanted song(s) by {artist}."
    note += (f" Deleted {len(deleted)} file(s), freeing {human_bytes(freed)}." if deleted
             else " No audio was touched.")
    return RedirectResponse(f"/wants?msg={urllib.parse.quote(note)}", status_code=303)


@app.post("/wants/{wid}/remove")
def remove_want(request: Request, wid: int):
    conn = db.connect()
    row = conn.execute("SELECT title, artist, status FROM wants WHERE id=?", (wid,)).fetchone()
    if not row:
        return RedirectResponse("/wants?msg=No+such+want.", status_code=303)
    if row["status"] == db.STATUS_HAVE:
        # Removing a satisfied want would not delete the file, so it would only lose the record of
        # why the file is there.
        note = (f"\u201c{row['title']}\u201d is already in the library, so its want is kept as the "
                "record of how it got there. Nothing was removed.")
        return RedirectResponse(f"/wants?msg={urllib.parse.quote(note)}", status_code=303)
    conn.execute("DELETE FROM wants WHERE id=?", (wid,))
    conn.commit()
    db.log_event(conn, "remove-want", f"{row['artist']} - {row['title']}", "removed by hand")
    note = f"Removed \u201c{row['artist']} - {row['title']}\u201d from the wanted list."
    return RedirectResponse(f"/wants?msg={urllib.parse.quote(note)}", status_code=303)


@app.post("/wants/{wid}/complete-album")
def complete_album_route(request: Request, wid: int):
    """Queue a job to find the album this held song belongs to and add its remaining tracks.

    Resolution needs catalogue HTTP, so like every add it runs in the worker; this handler only
    enqueues. Held songs only — completing a song not yet acquired would key the album search on
    unverified metadata, and the button is only rendered on held rows anyway.
    """
    conn = db.connect()
    row = conn.execute("SELECT title, artist, status FROM wants WHERE id=?", (wid,)).fetchone()
    if not row:
        return RedirectResponse("/wants?msg=No+such+want.", status_code=303)
    if row["status"] != db.STATUS_HAVE:
        note = (f"“{row['title']}” by {row['artist']} is not in the library yet — its album can "
                "be completed once the song itself has arrived.")
        return RedirectResponse(f"/wants?msg={urllib.parse.quote(note)}", status_code=303)
    dup = conn.execute("SELECT 1 FROM jobs WHERE kind='complete' AND ref=? AND status IN (?,?)",
                       (str(wid), db.JOB_QUEUED, db.JOB_RUNNING)).fetchone()
    if dup:
        note = (f"Already looking for the album of “{row['title']}” — see Adds in progress "
                "below.")
        return RedirectResponse(f"/wants?msg={urllib.parse.quote(note)}", status_code=303)
    return _enqueue(request, "complete", str(wid), "catalogues",
                    f"the rest of the album of “{row['title']}” by {row['artist']}")


@app.post("/wants/{wid}/retry")
def retry(request: Request, wid: int):
    conn = db.connect()
    row = conn.execute("SELECT title, artist, status FROM wants WHERE id=?", (wid,)).fetchone()
    if not row:
        return RedirectResponse("/wants?msg=No+such+want.", status_code=303)
    if row["status"] == db.STATUS_HAVE:
        # Re-pending a satisfied want downloads the recording again just to discard it against
        # the copy already held. To genuinely replace a file, delete it and rescan.
        note = (f"“{row['title']}” by {row['artist']} is already in the library — "
                "nothing to retry.")
        return RedirectResponse(f"/wants?msg={urllib.parse.quote(note)}", status_code=303)
    conn.execute("UPDATE wants SET status=?, retry_after=NULL, strikes=0, "
                 "note='retry requested' WHERE id=?", (db.STATUS_PENDING, wid))
    conn.commit()
    db.log_event(conn, "retry", f"{row['artist']} - {row['title']}", user_of(request))
    note = "Queued for another attempt." + (NUDGED_NOTE if _nudge() else UNNUDGED_NOTE)
    return RedirectResponse(f"/wants?msg={urllib.parse.quote(note)}", status_code=303)


def _file_token(path):
    """``path|size|mtime`` — a file's identity as it was shown on a confirmation page."""
    try:
        st = os.stat(path)
        return f"{path}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        return f"{path}|?|?"


def _reviewed_paths(tokens):
    """Paths from confirmation tokens whose file is still byte-for-byte the one reviewed.

    A token whose size or mtime no longer matches is dropped, not deleted: the audio at that name is
    no longer the audio the user looked at.
    """
    out = []
    for token in tokens:
        # rsplit, because a path may itself contain "|".
        parts = token.rsplit("|", 2)
        if len(parts) != 3:
            continue
        pth = parts[0]
        if _file_token(pth) == token:
            out.append(pth)
    return out


def _owned_by(path, lead):
    """True only if ``path`` sits inside that artist's own directory.

    A want's ``file_path`` is not proof of ownership: when placement finds an existing copy it
    records the twin's path, which can live under a different artist entirely (a compilation, say).
    Deleting "everything for artist A" would then unlink audio belonging to B.
    """
    root = os.path.realpath(os.path.join(LIBRARY, worker.safe(lead or "")))
    real = os.path.realpath(path)
    return real.startswith(root + os.sep)


def _delete_files(conn, paths, lead, log_subject):
    """Unlink audio and forget it, pruning empty directories. Returns (deleted, bytes, skipped).

    This is the only code in the project that deletes audio it did not just create, so each path has
    to clear two checks at the last moment: inside the library at all, and inside THIS artist's
    directory.
    """
    deleted, freed, skipped = [], 0, 0
    for path in paths:
        if not worker.within_library(path) or not _owned_by(path, lead) \
                or not os.path.isfile(path):
            skipped += 1
            continue
        try:
            freed += os.path.getsize(path)
            os.unlink(path)
        except OSError:
            skipped += 1
            continue
        deleted.append(path)
        conn.execute("DELETE FROM files WHERE path=?", (path,))
        d = os.path.dirname(path)
        while os.path.realpath(d) != os.path.realpath(LIBRARY):
            try:
                os.rmdir(d)
            except OSError:
                break
            d = os.path.dirname(d)
    conn.commit()
    if deleted:
        db.log_event(conn, "delete-files", log_subject,
                     f"{len(deleted)} file(s), {human_bytes(freed)}")
    return deleted, freed, skipped


def _confirm_page(request, title, lead, files, action, extra_fields, warning):
    """Confirmation screen listing every file that would be deleted.

    Deleting audio takes two deliberate signals — ticking the box and then confirming this list —
    because it is irreversible and because the file list is the only way to see what "this artist"
    actually resolved to before it happens.
    """
    rows = "".join(
        f"<tr><td>{esc(os.path.relpath(p, LIBRARY))}</td>"
        f"<td>{human_bytes(os.path.getsize(p)) if os.path.isfile(p) else 'missing'}</td></tr>"
        for p in files)
    total = sum(os.path.getsize(p) for p in files if os.path.isfile(p))
    hidden = "".join(f"<input type='hidden' name='{esc(k)}' value='{esc(v)}'>"
                     for k, v in extra_fields.items())
    # The exact list under review travels with the confirmation. Re-deriving it on the confirming
    # POST meant anything the worker downloaded in between was deleted without ever appearing on
    # this page. The handler still intersects these against what it independently owns, so a stale
    # or tampered path can only ever delete LESS than the honest answer, never more.
    # Path AND identity. A pathname alone is not the file: if a reviewed track is moved and something
    # else lands at the same name before the confirmation is submitted, the replacement would pass a
    # path-only intersection and be deleted unreviewed. Size and mtime are enough to notice that.
    hidden += "".join(f"<input type='hidden' name='path' value='{esc(_file_token(pth))}'>"
                      for pth in files)
    body = f"""
<h2>Delete {len(files)} file(s) for {esc(lead)}?</h2>
<p>{warning}</p>
<p><strong>This cannot be undone.</strong> {human_bytes(total)} of audio will be removed from disk.</p>
<table><caption>Files that will be deleted.</caption>
<thead><tr><th scope="col">File</th><th scope="col">Size</th></tr></thead>
<tbody>{rows}</tbody></table>
<form method="post" action="{esc(action)}">
  {hidden}
  <input type="hidden" name="delete_files" value="1">
  <input type="hidden" name="confirm" value="yes">
  <button type="submit">Yes, delete these {len(files)} file(s) for {esc(lead)}</button>
</form>
<form method="get" action="/wants">
  <button type="submit">Cancel and keep everything</button>
</form>
"""
    return page(title, body, user_of(request))


@app.post("/library/artist/delete")
def delete_library_artist(request: Request, artist: str = Form(...), confirm: str = Form(""),
                          delete_files: str = Form(""), path: list[str] = Form(default=[])):
    conn = db.connect()
    owned = [r["path"] for r in
             conn.execute("SELECT path FROM files WHERE artist_lead=? ORDER BY path", (artist,))
             if os.path.isfile(r["path"])]
    if not owned:
        return RedirectResponse(
            f"/library?msg={urllib.parse.quote(f'No files on disk for {artist}.')}", status_code=303)
    if not confirm:
        # Wants pointing at these files must go too. Leaving them would let the next scan see the
        # audio missing, flip each want back to pending, and re-download everything just deleted.
        doomed = db.count_wants_for_paths(conn, owned)
        return _confirm_page(
            request, "Confirm deletion", artist, owned, "/library/artist/delete",
            {"artist": artist},
            f"Deleting every track filed under {esc(artist)} in the library. "
            f"{doomed} matching wanted-list entr{'y' if doomed == 1 else 'ies'} will be removed too, "
            "so nothing re-downloads them.")
    # Only what was on the confirmation page AND is still this artist's. The intersection can only
    # ever shrink the set, so a stale or tampered submission deletes less, never more.
    reviewed = set(_reviewed_paths(path))
    targets = [p for p in owned if p in reviewed]
    unreviewed = len(owned) - len(targets)
    deleted, freed, skipped = _delete_files(conn, targets, artist, artist)
    # Keyed on what was ACTUALLY unlinked. Removing wants for files that survived an NFS error would
    # destroy the provenance of audio still sitting on disk.
    doomed = db.delete_wants_for_paths(conn, deleted)
    conn.commit()
    db.log_event(conn, "delete-artist", artist,
                 f"{len(deleted)} file(s) deleted, {doomed} want(s) removed")
    note = (f"Deleted {len(deleted)} file(s) for {artist}, freeing {human_bytes(freed)}, and removed "
            f"{doomed} wanted-list entr{'y' if doomed == 1 else 'ies'}.")
    if skipped:
        note += f" {skipped} file(s) were skipped as unreadable or outside {artist}'s folder."
    if unreviewed:
        note += (f" {unreviewed} file(s) appeared after the confirmation page was shown and were "
                 "left alone; delete again to include them.")
    return RedirectResponse(f"/library?msg={urllib.parse.quote(note)}", status_code=303)


# --------------------------------------------------------------------------- library

def _library_url(**params):
    """A /library link with empty values dropped. The path is always included — see _wants_url."""
    kept = {k: v for k, v in params.items() if v not in ("", None)}
    return "/library" + (("?" + urllib.parse.urlencode(kept)) if kept else "")


@app.get("/library", response_class=HTMLResponse)
def library(request: Request, artist: str = "", q: str = "", msg: str = "",
            page_no: int = Query(1, alias="page"), per: int = WANTS_PER_PAGE):
    conn = db.connect()
    if artist or q:
        where, args = " WHERE 1=1", []
        if artist:
            # Filtered on the LEAD, the same key the Wanted tab groups on, so "this artist" means
            # one set of tracks on both pages.
            where += " AND artist_lead=?"
            args.append(artist)
        if q:
            where += " AND (norm_title LIKE ? OR norm_artist LIKE ?)"
            args += [f"%{db.norm(q)}%"] * 2
        # Counted, then paged. This page used to take 500 rows and print len(rows) as the total, so a
        # broad search over 2000-plus files reported 500 as if that were the whole answer.
        total = conn.execute(f"SELECT COUNT(*) n FROM files{where}", args).fetchone()["n"]
        asked_per = per
        per = max(WANTS_MIN_PER, min(per, WANTS_MAX_PER))
        pages = max(1, -(-total // per))
        page_no = max(1, min(page_no, pages))
        offset = (page_no - 1) * per
        rows = conn.execute(
            f"SELECT * FROM files{where} ORDER BY artist, album, track_no LIMIT ? OFFSET ?",
            args + [per, offset]).fetchall()
        first, last = (offset + 1 if rows else 0), offset + len(rows)
        trs = "".join(
            f"<tr><td>{esc(r['artist'])}</td><td>{esc(r['album'] or '')}</td>"
            f"<td>{esc(r['title'])}</td><td>{esc(r['codec'])}</td>"
            f"<td>{int((r['bitrate'] or 0) / 1000)}k</td>"
            f"<td>{'yes' if r['lossless'] else 'no'}</td></tr>" for r in rows)
        prev_link = next_link = ""
        if page_no > 1:
            prev_link = (f"<a href='{esc(_library_url(artist=artist, q=q, per=per if per != WANTS_PER_PAGE else None, page=page_no - 1))}'>"
                         f"Previous page, tracks {(page_no - 2) * per + 1} to {first - 1} of {total}</a>")
        if page_no < pages:
            next_link = (f"<a href='{esc(_library_url(artist=artist, q=q, per=per if per != WANTS_PER_PAGE else None, page=page_no + 1))}'>"
                         f"Next page, tracks {last + 1} to {min(total, last + per)} of {total}</a>")
        scope = f" by {esc(artist)}" if artist else ""
        nav = (f"<nav aria-label=\"Library pages\"><p>Showing {first} to {last} of {total} "
               f"track{'' if total == 1 else 's'}{scope} — page {page_no} of {pages}."
               + (f" A page size of {asked_per} was requested and adjusted to {per}."
                  if per != asked_per else "")
               + f"</p><p>{prev_link} {next_link}</p></nav>") if rows else ""
        listing = (nav + f"<table><caption>Tracks{scope} {first} to {last} of {total}.</caption>"
                   "<thead><tr>"
                   "<th scope='col'>Artist</th><th scope='col'>Album</th>"
                   "<th scope='col'>Title</th><th scope='col'>Codec</th>"
                   "<th scope='col'>Bitrate</th><th scope='col'>Lossless</th>"
                   f"</tr></thead><tbody>{trs}</tbody></table>"
                   + (f"<p class='muted'>{prev_link} {next_link}</p>"
                      if prev_link or next_link else "")
                   if rows else "<p>Nothing matched.</p>")
    else:
        arows = conn.execute(
            "SELECT artist_lead AS lead, COUNT(*) n, SUM(lossless) ll, SUM(size) bytes "
            "FROM files GROUP BY artist_lead ORDER BY artist_lead COLLATE NOCASE").fetchall()
        listing = ("<h3>Artists</h3>"
                   "<table><caption>One row per artist directory. Deleting is irreversible and "
                   "asks for confirmation, listing every file first.</caption>"
                   "<thead><tr><th scope='col'>Artist</th>"
                   "<th scope='col'>Tracks</th><th scope='col'>Lossless</th>"
                   "<th scope='col'>Size</th><th scope='col'>Action</th></tr></thead><tbody>"
                   + "".join(
                       f"<tr><td><a href='/library?artist="
                       f"{urllib.parse.quote(r['lead'] or '')}'>{esc(r['lead'] or 'Unknown')}</a></td>"
                       f"<td>{r['n']}</td><td>{r['ll'] or 0}</td>"
                       f"<td>{human_bytes(r['bytes'])}</td>"
                       f"<td><form method='post' action='/library/artist/delete' class='inline'>"
                       f"<input type='hidden' name='artist' value='{esc(r['lead'] or '')}'>"
                       f"<button type='submit'>Delete all {r['n']} track(s) by "
                       f"{esc(r['lead'] or 'Unknown')} from the library</button></form></td></tr>"
                       for r in arows)
                   + "</tbody></table>")
    body = f"""
<h2>Library</h2>
<form method="get" action="/library" role="search">
  <p><label for="lq">Search your library</label><br>
  <input type="text" id="lq" name="q" value="{esc(q)}" autocomplete="off">
  <button type="submit">Search</button></p>
</form>
{listing}
"""
    return page("Library", body, user_of(request), msg)


# --------------------------------------------------------------------------- activity

@app.get("/activity", response_class=HTMLResponse)
def activity(request: Request):
    conn = db.connect()
    total = conn.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]
    rows = conn.execute("SELECT * FROM events ORDER BY at DESC LIMIT ?",
                        (ACTIVITY_LIMIT,)).fetchall()
    trs = "".join(
        f"<tr><td>{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r['at']))}</td>"
        f"<td>{esc(r['kind'])}</td><td>{esc(r['subject'] or '')}</td>"
        f"<td>{esc((r['detail'] or '')[:120])}</td></tr>" for r in rows)
    body = ("<h2>Activity</h2>"
            + (f"<table><caption>The {len(rows)} most recent of {total} recorded event(s).</caption>"
               f"<thead><tr><th scope='col'>When</th><th scope='col'>Event</th>"
               "<th scope='col'>Subject</th><th scope='col'>Detail</th></tr></thead>"
               f"<tbody>{trs}</tbody></table>" if trs else "<p>Nothing recorded yet.</p>"))
    return page("Activity", body, user_of(request))


@app.get("/healthz")
def healthz():
    # connect(), not init(): a health probe fired every interval must not take the BEGIN IMMEDIATE
    # write lock that migrate() holds, nor re-run the backfills. The worker init()s the schema at
    # container start (and every page load does too), so a probe never needs to.
    conn = db.connect()
    try:
        return {"ok": True, "stats": db.library_stats(conn)}
    finally:
        conn.close()
