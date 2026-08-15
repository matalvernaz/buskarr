"""Acquisition providers, tried in quality order.

Each provider implements ``search(want) -> [candidate]`` and ``fetch(candidate, dest) -> bool``.
A candidate is a plain dict with at least ``title``, ``artist``, ``duration`` and whatever the
provider needs to fetch it, plus ``codec``/``bitrate`` estimates so the caller can decide whether
it beats what's already held.

Order matters and is not arbitrary:
  Tidal    — FLAC 16/44, but only carries commercially released material
  Soulseek — variable, often FLAC, the only source for niche and out-of-print work
  YouTube  — 256k AAC, the only source for web-native artists who never had a release

Nothing here writes to the library. Providers fetch into a staging path; placement is the
caller's job, so file placement stays in one auditable place.
"""
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

TIDDL = os.environ.get("TIDDL_BIN", "/opt/tiddl-venv/bin/tiddl")
TIDDL_AUTH = os.environ.get("TIDDL_AUTH", "")
TIDAL_AUTH_JSON = os.environ.get("TIDAL_AUTH_JSON", "/state/tidal_auth.json")
YTDLP = os.environ.get("YTDLP_BIN", "yt-dlp")
COOKIES = os.environ.get("YT_COOKIES", "/state/cookies.txt")
DENO = "deno:" + os.environ.get("DENO_PATH", "/usr/local/bin/deno")
SLSKD_URL = os.environ.get("SLSKD_URL", "")
SLSKD_KEY = os.environ.get("SLSKD_API_KEY", "")

SEARCH_N = int(os.environ.get("SEARCH_N", "5"))
# Pace outbound requests: bursts are what provoke YouTube session throttling and Soulseek
# per-user rejections. Sequential and unhurried beats parallel and blocked.
YT_PACING = ["--sleep-requests", "2", "--sleep-interval", "3"]


class RateLimited(Exception):
    """A provider signalled session-wide throttling — abandon the run, don't blame the track."""


def _log(msg):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}", flush=True)


# --------------------------------------------------------------------------- Tidal

class Tidal:
    name = "tidal"
    quality_hint = "FLAC 16/44"

    def available(self):
        return self.status()[0]

    def status(self):
        """``(usable, why)`` — the credential must PARSE, not merely exist.

        Testing existence alone let a zero-byte ``auth.json`` report the provider available while
        every search returned nothing, so acquisition fell through to a lossy provider for as long
        as it took someone to notice a codec. The file is the thing that rots here, so the check
        has to read it.
        """
        if not TIDDL_AUTH:
            return False, "TIDDL_AUTH is not set"
        try:
            self._read_auth()
        except OSError as e:
            return False, (f"no readable auth file at {self._auth_path()} ({type(e).__name__}) — "
                           "re-authenticate with: tiddl auth login")
        except (json.JSONDecodeError, KeyError):
            return False, (f"auth file at {self._auth_path()} is not a usable session — "
                           "re-authenticate with: tiddl auth login")
        return True, "authenticated"

    @staticmethod
    def _auth_path():
        """Path to the session tiddl actually maintains.

        ``TIDAL_AUTH_JSON`` is a first-run bootstrap only — the entrypoint copies it into place once
        and nothing ever writes it again. tiddl rewrites ``$HOME/.tiddl/auth.json`` on every
        refresh, so reading the seed means presenting a token that expires ~4h after the container
        was first built and never recovers: searches 401 forever and every acquisition silently
        falls back to a lossy provider.

        Size, not existence: a truncated live file is worth less than the seed, whose refresh token
        may well still be good. tiddl rewrites this file in place, so an interrupted write leaves
        zero bytes and the old ``os.path.exists`` test preferred that over a working credential.
        """
        live = os.path.join(os.path.expanduser("~"), ".tiddl", "auth.json")
        try:
            if os.path.getsize(live) > 0:
                return live
        except OSError:
            pass
        return TIDAL_AUTH_JSON

    def _read_auth(self):
        """The parsed session. Raises rather than returning a dict that cannot authenticate."""
        with open(self._auth_path()) as fh:
            auth = json.load(fh)
        if not auth.get("token"):
            raise KeyError("token")
        return auth

    def refresh(self):
        """Tidal access tokens last ~4h, so every run refreshes before doing anything.

        Success is the exit code. The previous test asked whether the output contained the word
        "refresh", which the FAILURE path also satisfies — a dead credential prints a pydantic
        trace mentioning it twice — so a broken token reported a successful refresh and the cycle
        carried on believing Tidal was live.
        """
        env = dict(os.environ, TIDDL_AUTH=TIDDL_AUTH)
        r = subprocess.run([TIDDL, "auth", "refresh", "--force"],
                           capture_output=True, text=True, env=env, timeout=120)
        if r.returncode != 0:
            _log(f"tidal: token refresh FAILED (exit {r.returncode}) — Tidal will return nothing "
                 f"until re-authenticated: {(r.stdout + r.stderr).strip()[-200:]}")
            return False
        return True

    def search(self, want):
        try:
            auth = self._read_auth()
        except (OSError, json.JSONDecodeError, KeyError) as e:
            # Never silent. An empty return here is indistinguishable from "Tidal does not have
            # this song", which is exactly how a 12-hour credential outage went unnoticed while
            # every acquisition quietly downgraded to YouTube.
            _log(f"tidal: cannot read auth ({type(e).__name__}) at {self._auth_path()} — "
                 "returning no results until re-authenticated")
            return []
        q = urllib.parse.urlencode({"query": f"{want['artist']} {want['title']}",
                                    "limit": SEARCH_N, "types": "TRACKS",
                                    "countryCode": auth.get("country_code", "US")})
        req = urllib.request.Request(
            f"https://api.tidal.com/v1/search?{q}",
            headers={"Authorization": f"Bearer {auth['token']}", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=45) as fh:
                data = json.load(fh)
        except urllib.error.HTTPError as e:
            # Logged with the status because 401 (dead token, needs re-auth) and 429 (throttled,
            # retry later) demand opposite responses and are indistinguishable otherwise. Not
            # raised: the remaining providers should still get a chance at this want.
            _log(f"tidal search failed: HTTP {e.code} {e.reason} (auth {self._auth_path()})")
            return []
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            _log(f"tidal search failed: {type(e).__name__}")
            return []
        out = []
        for t in (data.get("tracks") or {}).get("items", []):
            out.append({
                "provider": self.name,
                "id": t["id"],
                "title": t.get("title") or "",
                "artist": ", ".join(a.get("name", "") for a in (t.get("artists") or [])),
                "duration": float(t.get("duration") or 0),
                "codec": "flac", "bitrate": 1000000,
                "sample_rate": 44100, "bit_depth": 16,
            })
        return out

    def fetch(self, candidate, dest_dir):
        env = dict(os.environ, TIDDL_AUTH=TIDDL_AUTH)
        shutil.rmtree(dest_dir, ignore_errors=True)
        os.makedirs(dest_dir, exist_ok=True)
        r = subprocess.run(
            [TIDDL, "download", "-q", "high", "-p", dest_dir, "-t", "1", "-err",
             "url", f"https://tidal.com/browse/track/{candidate['id']}"],
            capture_output=True, text=True, env=env, timeout=900)
        if r.returncode != 0:
            # A non-zero exit means the file on disk, if any, is partial or unrelated. Returning it
            # anyway sent unverified bytes into the library.
            _log(f"tidal fetch exited {r.returncode}: {(r.stdout + r.stderr).strip()[-160:]}")
            return None
        got = [os.path.join(dp, f) for dp, _, fs in os.walk(dest_dir)
               for f in fs if f.lower().endswith(".flac")]
        if not got:
            _log(f"tidal fetch produced nothing: {(r.stdout + r.stderr).strip()[-160:]}")
        return got[0] if got else None


# --------------------------------------------------------------------------- Soulseek

class Soulseek:
    name = "soulseek"
    quality_hint = "varies, often FLAC"
    # Soulseek matches on file and folder names, never metadata, so the query is a plain string.
    GOOD_EXT = (".flac", ".m4a", ".mp3", ".ogg", ".opus")

    def available(self):
        return self.status()[0]

    def status(self):
        missing = [n for n, v in (("SLSKD_URL", SLSKD_URL), ("SLSKD_API_KEY", SLSKD_KEY)) if not v]
        if missing:
            return False, f"{' and '.join(missing)} not set"
        return True, "configured"

    def _api(self, method, path, body=None):
        req = urllib.request.Request(
            f"{SLSKD_URL.rstrip('/')}/api/v0/{path}", method=method,
            headers={"X-API-Key": SLSKD_KEY, "Content-Type": "application/json"},
            data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req, timeout=90) as fh:
            raw = fh.read()
            return json.loads(raw) if raw else None

    def search(self, want):
        text = f"{want['artist']} {want['title']}"
        try:
            started = self._api("POST", "searches", {"searchText": text})
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as e:
            _log(f"slskd search failed: {type(e).__name__}")
            return []
        sid = (started or {}).get("id")
        if not sid:
            return []
        # slskd searches are asynchronous; poll briefly rather than guessing a fixed sleep.
        responses = []
        for _ in range(20):
            time.sleep(1.5)
            try:
                st = self._api("GET", f"searches/{sid}")
            except Exception:
                break
            if st and st.get("state", "").lower().startswith("completed"):
                responses = st.get("responses") or []
                break
            if st and (st.get("fileCount") or 0) > 0:
                responses = st.get("responses") or []
        out = []
        for resp in responses:
            for f in (resp.get("files") or []):
                name = f.get("filename") or ""
                if not name.lower().endswith(self.GOOD_EXT):
                    continue
                ext = os.path.splitext(name)[1].lower().lstrip(".")
                out.append({
                    "provider": self.name,
                    "username": resp.get("username"),
                    "filename": name,
                    "size": f.get("size"),
                    "title": os.path.splitext(os.path.basename(name.replace("\\", "/")))[0],
                    # Soulseek has no credit field, so the shared path IS the only artist
                    # evidence. Substituting want["artist"] here (as this once did) fed the gate
                    # its own answer and made artist_ok a no-op on this provider — a filename
                    # match alone could then place a stranger's recording in the library.
                    "artist": name.replace("\\", "/"),
                    "duration": float(f.get("length") or 0) or None,
                    "codec": "flac" if ext == "flac" else ext,
                    "bitrate": (f.get("bitRate") or 0) * 1000 or (1000000 if ext == "flac" else 0),
                    "queue_length": resp.get("queueLength") or 0,
                })
        # Prefer lossless, then shorter peer queues — a 40-deep queue rarely completes.
        out.sort(key=lambda c: (c["codec"] != "flac", c["queue_length"]))
        return out[:SEARCH_N]

    def fetch(self, candidate, dest_dir):
        try:
            self._api("POST", f"transfers/downloads/{candidate['username']}",
                      [{"filename": candidate["filename"], "size": candidate["size"]}])
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
            _log(f"slskd enqueue failed: {type(e).__name__}")
            return None
        # slskd writes into its own download tree; the caller locates the finished file there.
        return "QUEUED"


# --------------------------------------------------------------------------- YouTube

class YouTube:
    name = "youtube"
    quality_hint = "256k AAC with cookies, else ~130k"

    def available(self):
        return self.status()[0]

    def status(self):
        if shutil.which(YTDLP) is None and not os.path.exists(YTDLP):
            return False, f"{YTDLP} not found"
        # Not fatal — public videos download without them — but the age-restricted and
        # region-locked material is exactly what YouTube is the last resort for.
        return True, ("configured" if os.path.exists(COOKIES) else
                      f"configured; no cookies at {COOKIES}, so age-restricted videos will fail")

    def _cookie_args(self):
        return ["--cookies", COOKIES] if os.path.exists(COOKIES) else []

    def search(self, want):
        cmd = [YTDLP, *self._cookie_args(), "--js-runtimes", DENO,
               "--skip-download", "--no-playlist", "--no-warnings", *YT_PACING,
               "--match-filter", "duration>20 & duration<3600",
               "--print", "%(id)s\t%(duration)s\t%(channel)s\t%(title)s",
               f"ytsearch{SEARCH_N}:{want['artist']} {want['title']}"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        self._check_throttle(r.stderr)
        out = []
        for line in r.stdout.strip().splitlines():
            if line.count("\t") < 3:
                continue
            vid, dur, channel, title = line.split("\t", 3)
            out.append({
                "provider": self.name, "id": vid, "title": title, "artist": channel,
                "duration": float(dur) if dur and dur not in ("NA", "") else None,
                # 141 (256k AAC) exists only on official art-tracks; assume the common case.
                "codec": "aac", "bitrate": 256000 if os.path.exists(COOKIES) else 130000,
            })
        return out

    def fetch(self, candidate, dest_dir):
        shutil.rmtree(dest_dir, ignore_errors=True)
        os.makedirs(dest_dir, exist_ok=True)
        out = os.path.join(dest_dir, "dl.%(ext)s")
        r = subprocess.run(
            [YTDLP, *self._cookie_args(), "--js-runtimes", DENO, "-f", "141/bestaudio",
             "-x", "--audio-format", "m4a", "--no-playlist", *YT_PACING, "-o", out,
             f"https://www.youtube.com/watch?v={candidate['id']}"],
            capture_output=True, text=True, timeout=1200)
        self._check_throttle(r.stderr)
        if r.returncode != 0:
            # Throttling is raised above; anything else non-zero means whatever landed in the
            # staging directory is partial or unrelated and must not be offered as the download.
            _log(f"youtube fetch exited {r.returncode}: {(r.stderr or '').strip()[-160:]}")
            return None
        got = [os.path.join(dest_dir, f) for f in os.listdir(dest_dir)
               if f.lower().endswith((".m4a", ".opus", ".mp3"))]
        return got[0] if got else None

    @staticmethod
    def _check_throttle(stderr):
        for line in (stderr or "").splitlines():
            low = line.lower()
            if "rate-limited" in low or "429" in low or "too many requests" in low:
                raise RateLimited(line.strip()[:160])


ALL = [Tidal(), Soulseek(), YouTube()]


def enabled():
    """Providers usable right now, in preference order, with why each is or isn't available.

    ``detail`` is the reason in words. A provider dropping off this list used to show only as a
    shorter log line, which reads the same as a provider that was never configured.
    """
    out = []
    for p in ALL:
        ok, detail = p.status()
        out.append({"name": p.name, "hint": p.quality_hint, "available": ok,
                    "detail": detail, "provider": p})
    return out
