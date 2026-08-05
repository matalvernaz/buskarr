# buskarr

![tests](https://github.com/matalvernaz/buskarr/actions/workflows/tests.yml/badge.svg)

A track-first music acquisition manager. Built because Lidarr's atomic unit is the *release*, which
makes it structurally unable to handle a singles-heavy library: one artist here has 54 releases on
Deezer, 24 in MusicBrainz, and 22 that Lidarr could see.

Self-hosted, one Docker container, SQLite, no external database. It has no authentication of its
own: it trusts oauth2-proxy-style forwarded-identity headers, so it belongs on localhost or behind
a reverse proxy that handles login.

## What it does differently

**A want is a track, never an album.** There is no album atom in the schema. That single decision
is why singles, B-sides and YouTube-native songs work at all.

**Identity is (artist, title, duration)** — not MusicBrainz IDs. Keying on MusicBrainz caps a
library at whatever MusicBrainz happens to know, which for novelty and web-native artists is a
small fraction of reality. Deezer is used for catalogue search because it lists the singles
MusicBrainz omits.

**It knows the library.** Every file is scanned for codec, bitrate, sample rate and bit depth, so
"do I already have this, and is this candidate actually better?" are answered from data. It will
not re-fetch something already held.

**It owns every write.** Nothing deletes a file it did not just create, and no operation is
album-scoped. This is a direct response to two incidents on 2026-08-03 where Lidarr's
`ManualImport` with `replaceExistingFiles=True` deleted 124 files across five albums — including
24-bit/192kHz masters — because that flag replaces the whole matched *album*, not one track's file.

## Providers, in quality order

| Provider | Quality | Notes |
|---|---|---|
| Tidal | FLAC 16/44 | Commercially released material only. Needs `TIDDL_AUTH` + a device-flow login. |
| Soulseek | varies, often FLAC | Via slskd. Matches on filenames, so it finds things no database lists. Asynchronous. |
| YouTube | 256k AAC with cookies | The only source for artists who never had a release. |
| Prowlarr | whatever the release is | `buskarr.grab`: album-level torrent grabs for UNAVAILABLE tracks, sent to qBittorrent. Asynchronous; harvest imports. |
| harvest | whatever was downloaded | Not a search provider: mines completed downloads already on disk. |

The first candidate that clears vetting wins. `harvest` closes the loop for the two asynchronous
providers and recovers material already downloaded but never imported — there were ~694 such
directories holding ~106GB, rejected by Lidarr because it refuses a release unless 80% of an album
matches, discarding an 11-of-16-track download whole.

## Installing

    git clone https://github.com/matalvernaz/buskarr
    cd buskarr
    cp compose.example.yaml compose.yaml
    # edit compose.yaml: your music path, your downloads path, which providers you use
    docker compose up -d --build

Then open `http://localhost:8000`, hit "Rescan library from disk" on the Overview page, and add
music from the Add music page. `state/` holds the database, staging, cookies and Tidal auth, and
must persist. The worker and the web UI share one container because they share the SQLite file.

Provider setup, all optional — with none configured you can still scan, browse and manage the
library:

* **Tidal** — set `TIDDL_AUTH` in `.env` (a client credential pair for
  [tiddl](https://github.com/oskvr37/tiddl); see its documentation), then authenticate once:
  `docker exec -it buskarr /opt/tiddl-venv/bin/tiddl auth login`. The session refreshes itself
  under `state/home`.
* **YouTube** — export logged-in cookies (Netscape format) to `state/cookies.txt` for 256k AAC;
  without them fetches still work at ~130k.
* **Soulseek** — point `SLSKD_URL`/`SLSKD_API_KEY` at a running [slskd](https://github.com/slskd/slskd),
  and put its completed-downloads directory in `DOWNLOAD_DIRS`.
* **Prowlarr/qBittorrent** — for `buskarr.grab`. Give Prowlarr a qBittorrent download client
  with a dedicated category whose save path is inside `DOWNLOAD_DIRS`. Note qBittorrent applies a
  category's save path only under automatic torrent management; `grab` switches its torrents to
  AutoTMM itself.

Tests need only `mutagen` from pip: `for t in tests/test_*.py; do python3 "$t"; done`.

## Day to day

Adding and monitoring happen in the web UI. The maintenance modules run by hand, print what they
would do by default, and only act with `--apply`:

    docker exec buskarr python3 -m buskarr.harvest          # match completed downloads (dry-run)
    docker exec buskarr python3 -m buskarr.harvest --apply
    docker exec buskarr python3 -m buskarr.upgrade --apply  # replace lossy files, worst first
    docker exec buskarr python3 -m buskarr.grab --apply     # torrent grabs for unavailable albums
    docker exec buskarr python3 -m buskarr.dedupe           # what duplicates would be quarantined

After anything that moves files, rescan (the Overview button, or the same module pattern) so the
database matches the disk.

## Vetting

Three gates, all must pass. Ported from a predecessor script where they built a track record:
they rejected a kids' choir singing "Jellyfish" as an Arrogant Worms track, and refused a
206-second Tidal edit of a song whose local copy runs 255 seconds.

* **Duration** — ±15% normally, ±4% when replacing a file whose exact length is known.
* **Title** — token overlap ≥0.7, or difflib ratio ≥0.72 for word-order differences.
* **Artist** — must appear in the candidate's title or credit. Deliberately lenient, because
  channel names vary wildly (`ParryGripp`, `alyankovicVEVO`, `Kaylee Rose - Topic`); it only
  rejects when the artist is absent entirely.

Quality comparison ranks by codec class first and bitrate second, and requires a meaningful gain
before replacing anything. Swapping 256k AAC for 320k MP3 gains nothing audible while costing disk
and provenance — that exact churn happened once by trusting a bitrate number alone.

## Accessibility

The primary user is a screen-reader user, so this is a requirement rather than a nicety. Skip link
first in the DOM, landmark regions, `aria-live` status region, real `<button>` and `<label for>`
throughout, `scope` on every table header, and every page works with JavaScript disabled. No SPA,
no client framework, no build step.

## Layout

Files are placed as `<Artist>/<Album> (<Year>)/NN - Title.ext`, matching the existing library.

## Operating notes

* **`HOME` must stay on the persistent volume.** tiddl rewrites `$HOME/.tiddl/auth.json` when it
  refreshes, and Tidal access tokens last ~4h. Losing that file on restart silently reverts to
  unauthenticated — the same trap that had YouTube fetches quietly serving 128k Opus for hours.
* **`tiddl` is pinned to 3.4.4**, which requires Python ≥3.13. Unpinned installs on 3.12 silently
  resolve down to 2.8.0, which has an incompatible CLI and fails at fetch time rather than install
  time. The pin makes that loud.
* **The downloads mount is read-only** so the harvester cannot disturb what qBittorrent and
  slskd are still seeding.
* Escalating backoff on failures: 24h doubling to a 30-day cap. An *empty* search result gets only
  1h, because a throttled session is indistinguishable from a song nobody has, and treating one as
  the other buried 273 tracks for a day.

## Layout of the code

    buskarr/db.py         SQLite schema, normalisation, want/file lookups
    buskarr/scan.py       library scanner (tags + ffprobe), want reconciliation
    buskarr/match.py      the three vetting gates and quality scoring
    buskarr/providers.py  Tidal, Soulseek, YouTube
    buskarr/harvest.py    import from completed downloads, best copy wins
    buskarr/worker.py     acquisition loop, file placement, tagging
    buskarr/web.py        FastAPI UI

Maintenance modules, all dry-run by default (`--apply` to act):

    buskarr/fold.py       collapse collaboration folders into the lead artist's
    buskarr/refile.py     move placed files to the current layout rules
    buskarr/repair.py     fix demonstrably wrong tags
    buskarr/dedupe.py     quarantine duplicate recordings, best copy kept
    buskarr/upgrade.py    replace held lossy files with better copies (originals quarantined)
    buskarr/grab.py       album-level torrent grabs via Prowlarr for unavailable tracks

## Not done yet

* Usenet grabs. `buskarr.grab` filters to torrent releases because Prowlarr's only configured
  download client is qBittorrent; adding NZBGet as a second client there would open the usenet
  results with no buskarr change beyond dropping the protocol filter.
* Multi-user. `requested_by` is recorded and the UI shows it; approval workflows are not built.
