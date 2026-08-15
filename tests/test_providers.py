"""Availability reporting must distinguish "not set up" from "set up and broken".

Written after a real outage: tiddl truncated ``auth.json`` to zero bytes, ``available()`` tested
only ``os.path.exists``, and ``search()`` swallowed the resulting JSONDecodeError and returned an
empty list. Every layer above read that as "Tidal does not have this song", so acquisition ran for
twelve hours downgrading FLAC to YouTube AAC while the cycle log kept printing ``providers: tidal``.

The empty-file case is the whole point, so it is exercised against real files on disk rather than a
mock — an in-memory fake would have to reproduce the exact behaviour that was missed.
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from buskarr import providers  # noqa: E402

GOOD = {"token": "t", "refresh_token": "r", "expires_at": "1786828177",
        "user_id": "1", "country_code": "CA"}

failures = []


def check(label, got, expect):
    ok = got == expect
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f"  got {got!r}"))
    if not ok:
        failures.append(label)


def with_home(home, seed, fn, backup=None):
    """Run fn with $HOME, the snapshot and the bootstrap seed pointed at a scratch tree."""
    saved = (os.environ.get("HOME"), providers.TIDAL_AUTH_JSON, providers.TIDDL_AUTH,
             providers.TIDAL_AUTH_BACKUP)
    os.environ["HOME"] = home
    providers.TIDAL_AUTH_JSON, providers.TIDDL_AUTH = seed, "set"
    providers.TIDAL_AUTH_BACKUP = backup or os.path.join(home, "unused-backup.json")
    try:
        return fn()
    finally:
        if saved[0] is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved[0]
        (providers.TIDAL_AUTH_JSON, providers.TIDDL_AUTH,
         providers.TIDAL_AUTH_BACKUP) = saved[1], saved[2], saved[3]


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def run():
    tmp = tempfile.mkdtemp(prefix="buskarr-prov-")
    try:
        home = os.path.join(tmp, "home")
        live = os.path.join(home, ".tiddl", "auth.json")
        seed = os.path.join(tmp, "seed.json")
        t = providers.Tidal()

        print("a parseable live session is usable")
        write(live, json.dumps(GOOD))
        write(seed, json.dumps(GOOD))
        ok, _healthy, why = with_home(home, seed, t.status)
        check("status() accepts a good live file", ok, True)
        check("live file is preferred over the seed",
              with_home(home, seed, t._auth_path), live)

        print("\nthe outage: a truncated live session, good seed — usable but not healthy")
        write(live, "")
        # The seed is the surviving credential. Preferring an empty live file over it is what made
        # the real outage need a hand-copy; falling back keeps the provider alive by itself.
        check("falls back to the seed when live is empty",
              with_home(home, seed, t._auth_path), seed)
        ok, healthy, why = with_home(home, seed, t.status)
        check("still usable, because the seed parses", ok, True)
        check("but reported as not healthy", healthy, False)
        check("the reason says it is rebuilt automatically", "rebuilt" in why.lower(), True)

        print("\nheal() repairs the file tiddl itself reads")
        # Reading around the damage is not enough: fetch shells out to tiddl, which opens its own
        # $HOME copy. If heal stops writing `live`, downloads break while search still works —
        # the hardest version of this bug to diagnose.
        write(live, "")
        used = with_home(home, seed, t.heal)
        check("heal reports the source it restored from", used, seed)
        check("the live file is usable again", providers.Tidal._usable(live), True)
        check("heal is a no-op once the live file is good",
              with_home(home, seed, t.heal), None)

        print("\nsnapshots keep the recovery copy current, and never overwrite good with bad")
        backup = os.path.join(tmp, "backup.json")
        write(live, json.dumps(dict(GOOD, token="fresh")))
        check("snapshot taken from a good live file",
              with_home(home, seed, t._snapshot, backup=backup), True)
        check("the snapshot holds the fresh token",
              json.load(open(backup))["token"], "fresh")
        write(live, "")
        check("no snapshot taken from a damaged live file",
              with_home(home, seed, t._snapshot, backup=backup), False)
        check("the good snapshot survived", json.load(open(backup))["token"], "fresh")
        # Preference order matters: a snapshot minutes old beats a seed twelve days old.
        check("snapshot is preferred over the seed",
              with_home(home, seed, t._auth_path, backup=backup), backup)
        check("heal prefers the snapshot",
              with_home(home, seed, t.heal, backup=backup), backup)

        print("\nthe outage: nothing parseable anywhere — must be loud")
        os.remove(seed)
        os.remove(backup)
        write(live, "")
        ok, healthy, why = with_home(home, seed, t.status, backup=backup)
        check("status() rejects a zero-byte auth file with no copies", ok, False)
        check("and is not healthy either", healthy, False)
        check("the reason names re-authentication", "re-auth" in why.lower(), True)

        print("\nsearch must not answer 'no results' when it means 'no credential'")
        res = with_home(home, seed, lambda: t.search({"artist": "Ed Sheeran", "title": "Perfect"}))
        check("search returns empty rather than raising", res, [])
        # Proving the test: a corrupt file must be as unusable as a missing one. If status() ever
        # regresses to an existence check, THIS is the case that fails first.
        write(live, "{not json")
        ok, _, _ = with_home(home, seed, t.status)
        check("status() rejects unparseable JSON", ok, False)
        write(live, json.dumps({"refresh_token": "r"}))
        ok, _, _ = with_home(home, seed, t.status)
        check("status() rejects a session with no access token", ok, False)

        print("\nevery provider reports a reason, usable or not")
        for entry in providers.enabled():
            check(f"{entry['name']} carries a non-empty detail",
                  bool(entry["detail"].strip()), True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all checks passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
