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
import contextlib
import fcntl
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
# Rewritten after every verified refresh, so recovery uses a credential minutes old rather than the
# bootstrap seed, which was twelve days stale the one time it was needed. Kept separate from the
# seed: a snapshot is only ever as good as the last refresh, and the seed is the floor to fall back
# to if a snapshot is itself revoked.
TIDAL_AUTH_BACKUP = os.environ.get("TIDAL_AUTH_BACKUP", "/state/tidal_auth.backup.json")
TIDAL_REFRESH_LOCK = os.environ.get("TIDAL_REFRESH_LOCK", "/state/tidal-refresh.lock")
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


def _atomic_copy(src, dest, mode=0o600):
    """Copy through a temp file and rename, so an interrupted copy never publishes a partial one.

    The whole reason the Tidal credential is fragile is that its writer truncates in place; a
    recovery mechanism that copied the same way would inherit the same failure.
    """
    tmp = f"{dest}.tmp{os.getpid()}"
    try:
        shutil.copyfile(src, tmp)
        os.chmod(tmp, mode)
        os.replace(tmp, dest)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


@contextlib.contextmanager
def _flock(path):
    """Hold an exclusive lock across a block. Yields True if the lock was actually taken.

    The worker's cycle and a maintenance sweep are separate processes sharing one ``$HOME``, and
    both refresh the Tidal token. tiddl's writer truncates before it serialises, so two overlapping
    refreshes have a window where the file is empty. Refusing to refresh at all would be worse than
    refreshing unlocked — the token expires in ~4h either way — so a lock failure is not fatal.

    But the caller has to KNOW, because the two things done under this lock carry different risks.
    Refreshing unlocked risks the live file, which ``heal`` repairs from a copy. Snapshotting
    unlocked risks the copy itself: a concurrent refresh can truncate the live file between the
    check and the copy, publishing an empty snapshot over the one credential recovery depends on,
    and turning a transient overlap into a real re-authentication.
    """
    fh = None
    try:
        fh = open(path, "w")
        fcntl.flock(fh, fcntl.LOCK_EX)
    except OSError:
        if fh:
            fh.close()
        fh = None
    try:
        yield fh is not None
    finally:
        if fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            finally:
                fh.close()


# --------------------------------------------------------------------------- Tidal

class Tidal:
    name = "tidal"
    quality_hint = "FLAC 16/44"

    def available(self):
        return self.status()[0]

    def status(self):
        """``(usable, healthy, why)`` — the credential must PARSE, not merely exist.

        Testing existence alone let a zero-byte ``auth.json`` report the provider available while
        every search returned nothing, so acquisition fell through to a lossy provider for as long
        as it took someone to notice a codec. The file is the thing that rots here, so the check
        has to read it.

        Damaged-but-recoverable is deliberately still USABLE: ``refresh`` heals at the top of every
        cycle, before anything fetches, so refusing to use Tidal would cause the very outage this
        is meant to prevent. It is not HEALTHY, because a credential that had to be rebuilt from a
        copy is worth saying out loud.
        """
        if not TIDDL_AUTH:
            return False, False, "TIDDL_AUTH is not set"
        if self._usable(self._live_path()):
            return True, True, "authenticated"
        spare = next((p for p in (TIDAL_AUTH_BACKUP, TIDAL_AUTH_JSON) if self._usable(p)), None)
        if spare:
            return True, False, (f"the live session at {self._live_path()} is damaged; it is "
                                 f"rebuilt from {spare} at the start of every cycle, so downloads "
                                 "continue. Recurring means something is killing tiddl mid-write")
        return False, False, (f"no usable session at {self._live_path()} and no recoverable copy "
                              "— re-authenticate with: tiddl auth login")

    @staticmethod
    def _live_path():
        """The one file tiddl itself reads and writes. Everything else here exists to protect it."""
        return os.path.join(os.path.expanduser("~"), ".tiddl", "auth.json")

    @staticmethod
    def _usable(path):
        """True if PATH holds a session that could actually authenticate.

        Parsing is the test, not existence or size. tiddl's ``save_auth_data`` opens the file with
        mode ``"w"`` — which truncates BEFORE the JSON is serialised — so an interrupted refresh
        leaves a file that exists, is owned correctly, and authenticates nothing.
        """
        try:
            with open(path) as fh:
                return bool(json.load(fh).get("token"))
        except (OSError, json.JSONDecodeError, AttributeError, ValueError):
            return False

    @classmethod
    def _sources(cls):
        """Credentials in preference order: the live session, the snapshot, the bootstrap seed."""
        return [p for p in (cls._live_path(), TIDAL_AUTH_BACKUP, TIDAL_AUTH_JSON) if p]

    @classmethod
    def _auth_path(cls):
        """First credential that parses, falling back to the live path when none do.

        Returning the live path on total failure keeps error messages pointed at the file a human
        has to fix, rather than at whichever copy happened to be checked last.
        """
        for p in cls._sources():
            if cls._usable(p):
                return p
        return cls._live_path()

    @classmethod
    def heal(cls):
        """Put a usable session back where tiddl reads it. Returns the source used, or None.

        Reading around a damaged live file is not enough: ``search`` would recover but ``fetch``
        shells out to tiddl, which opens its own ``$HOME`` copy and would still fail. The file has
        to be repaired in place, and this runs at the top of every refresh so a truncation costs one
        cycle instead of however long it takes a human to notice a codec.
        """
        live = cls._live_path()
        if cls._usable(live):
            return None
        for src in (TIDAL_AUTH_BACKUP, TIDAL_AUTH_JSON):
            if cls._usable(src) and _atomic_copy(src, live):
                _log(f"tidal: live session at {live} was unusable; restored from {src}")
                return src
        return None

    @classmethod
    def _snapshot(cls):
        """Keep a known-good copy of the live session, written atomically. True if one was taken.

        Guarded on the live file parsing, so a damaged session can never overwrite the good copy —
        that would turn a recoverable truncation into a real re-authentication.
        """
        live = cls._live_path()
        return bool(cls._usable(live) and _atomic_copy(live, TIDAL_AUTH_BACKUP))

    def _read_auth(self):
        """The parsed session. Raises rather than returning a dict that cannot authenticate.

        Walks the sources rather than trusting ``_auth_path``: that call checks a file and this one
        reopens it, so a refresh truncating the live file in between would otherwise turn a search
        into "Tidal has no results for this song" — the exact silent downgrade the rest of this
        class exists to prevent. Falling through to the snapshot costs nothing when the live file
        is fine, because it is tried first.
        """
        last = None
        for path in self._sources():
            try:
                with open(path) as fh:
                    auth = json.load(fh)
                if not auth.get("token"):
                    raise KeyError("token")
                return auth
            except (OSError, json.JSONDecodeError, KeyError) as e:
                last = e
        raise last if last else OSError("no auth sources configured")

    def refresh(self):
        """Tidal access tokens last ~4h, so every run refreshes before doing anything.

        Repair, refresh, snapshot — in that order, under a lock:

        * A truncated session cannot be refreshed, so ``heal`` runs first or the refresh fails for
          a reason that has nothing to do with the token.
        * Success is the EXIT CODE. The previous test asked whether the output contained the word
          "refresh", which the failure path also satisfies — a dead credential prints a pydantic
          trace mentioning it twice — so a broken token reported success and the cycle carried on
          believing Tidal was live.
        * A failed run may have truncated the file on its way down, so it heals again rather than
          leaving the damage for the next cycle to find.
        * The snapshot is taken only after a verified refresh, which is what keeps the recovery
          copy current. Recovering from a twelve-day-old seed worked once; it is not a plan.

        The lock covers the whole sequence because an upgrade sweep refreshes too, in its own
        process, and two truncate-then-write cycles overlapping is a way to lose the credential
        that no amount of care inside one process prevents.
        """
        with _flock(TIDAL_REFRESH_LOCK) as locked:
            self.heal()
            env = dict(os.environ, TIDDL_AUTH=TIDDL_AUTH)
            try:
                r = subprocess.run([TIDDL, "auth", "refresh", "--force"],
                                   capture_output=True, text=True, env=env, timeout=120)
            except subprocess.TimeoutExpired:
                # The child is killed here, and tiddl has already truncated the file if it got that
                # far. This is the most likely way the credential was lost in the first place.
                _log("tidal: token refresh timed out and was killed; repairing the session")
                self.heal()
                return False
            if r.returncode != 0:
                _log(f"tidal: token refresh FAILED (exit {r.returncode}) — Tidal will return "
                     f"nothing until re-authenticated: {(r.stdout + r.stderr).strip()[-200:]}")
                self.heal()
                return False
            if locked:
                self._snapshot()
            else:
                # Without the lock a concurrent refresh can truncate the live file between the
                # check and the copy. Keeping the previous snapshot — stale by at most one refresh
                # — beats overwriting the recovery copy with an empty one.
                _log("tidal: refreshed without the lock; snapshot skipped to protect the copy "
                     "heal() restores from")
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
            return False, False, f"{' and '.join(missing)} not set"
        return True, True, "configured"

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
            return False, False, f"{YTDLP} not found"
        # Usable without cookies — public videos download fine — but not healthy: age-restricted
        # and region-locked material is exactly what YouTube is the last resort for.
        if not os.path.exists(COOKIES):
            return True, False, (f"no cookies at {COOKIES}, so age-restricted and region-locked "
                                 "videos will fail")
        return True, True, "configured"

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

    ``available`` is whether acquisition may use it; ``healthy`` is whether anything is wrong;
    ``detail`` is the reason in words. The two booleans differ for a provider that still works but
    is running on a repaired credential or without cookies — states that would otherwise show as
    "fine", which is how a degraded run goes unnoticed. A provider dropping off this list used to
    show only as a shorter log line, indistinguishable from one that was never configured.
    """
    out = []
    for p in ALL:
        ok, healthy, detail = p.status()
        out.append({"name": p.name, "hint": p.quality_hint, "available": ok,
                    "healthy": healthy, "detail": detail, "provider": p})
    return out
