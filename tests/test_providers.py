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
import time
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from buskarr import providers  # noqa: E402

# getattr, so this file still runs (and fails readably) against code without the check.
real_tidal_subscription = getattr(providers, "_tidal_subscription", None)

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


PAID = {"premiumAccess": True, "subscription": {"type": "HIFI"},
        "highestSoundQuality": "LOSSLESS", "status": "ACTIVE"}
# What the live account answered on 2026-10-08, a week after it lapsed.
FREE = {"startDate": "2026-10-01T18:28:36.038+0000", "status": "ACTIVE",
        "subscription": {"type": "FREE", "offlineGracePeriod": 0},
        "highestSoundQuality": None, "premiumAccess": False, "paymentOverdue": False}


class Subscription:
    """Stands in for the one network call. Counts calls; answers with a body or raises."""

    def __init__(self, answer=PAID):
        self.answer, self.calls = answer, 0

    def __call__(self, auth):
        self.calls += 1
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


def use_plan(answer):
    """Install a fresh fake and forget any cached answer, as a new process would."""
    fake = Subscription(answer)
    providers._tidal_subscription = fake
    providers.Tidal._plan = None
    return fake


def run():
    tmp = tempfile.mkdtemp(prefix="buskarr-prov-")
    real_subscription = real_tidal_subscription
    # Every status() below asks about the plan. Without a fake that is a real request to Tidal
    # carrying a made-up token, from a test run.
    use_plan(PAID)
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

        plan_checks(t, home, seed, live)
        ready_checks()
    finally:
        providers._tidal_subscription = real_subscription
        providers.Tidal._plan = None
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all checks passed'}")
    return 1 if failures else 0


def plan_checks(t, home, seed, live):
    print("\nthe 2026-10-08 outage: a working sign-in on a plan that cannot download")
    write(live, json.dumps(GOOD))
    write(seed, json.dumps(GOOD))
    use_plan(FREE)
    ok, healthy, why = with_home(home, seed, t.status)
    check("a free plan takes Tidal out of acquisition", ok, False)
    check("and is reported as not healthy", healthy, False)
    check("the reason names the plan", "free plan" in why, True)
    check("and says Tidal comes back by itself", "until the plan allows" in why, True)

    print("\na paid plan changes nothing")
    use_plan(PAID)
    check("a paid plan is usable and healthy", with_home(home, seed, t.status),
          (True, True, "authenticated"))

    print("\nno premium access is a refusal even without a plan name")
    use_plan({"premiumAccess": False, "subscription": {}})
    ok, _, _ = with_home(home, seed, t.status)
    check("premiumAccess false alone takes Tidal out", ok, False)

    print("\nan unanswered check is not a refusal")
    for label, err in (
            ("HTTP 401, an expired token", urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)),
            ("Tidal unreachable", urllib.error.URLError("no route to host")),
            ("a body that is not JSON", ValueError("Expecting value"))):
        use_plan(err)
        check(f"{label}: Tidal stays in", with_home(home, seed, t.status),
              (True, True, "authenticated"))
    use_plan(urllib.error.URLError("no route to host"))
    with_home(home, seed, t.plan)
    check("an unanswered check is asked again within TIDAL_PLAN_RETRY",
          providers.Tidal._plan[0] - time.time() <= providers.TIDAL_PLAN_RETRY, True)

    print("\nthe plan is asked once per TTL, not once per page load")
    fake = use_plan(FREE)
    for _ in range(5):
        with_home(home, seed, t.status)
    check("five status() calls cost one request", fake.calls, 1)
    with_home(home, seed, lambda: t.plan(force=True))
    check("force asks again", fake.calls, 2)
    check("an answer is kept for TIDAL_PLAN_TTL",
          providers.Tidal._plan[0] - time.time() > providers.TIDAL_PLAN_TTL - 60, True)

    print("\nthe plan applies to a session rebuilt from a copy as well")
    write(live, "")
    use_plan(FREE)
    ok, _, why = with_home(home, seed, t.status)
    check("damaged live session, good seed, free plan: refused", ok, False)
    check("and the reason is the plan, not the damage", "free plan" in why, True)
    write(live, json.dumps(GOOD))

    print("\nthe request names the account and carries its token")
    seen = []

    class Answer:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(PAID).encode()

    def fake_urlopen(req, timeout=None):
        seen.append(req)
        return Answer()

    if real_tidal_subscription is None:
        check("providers._tidal_subscription exists", False, True)
        return
    saved = providers.urllib.request.urlopen
    providers.urllib.request.urlopen = fake_urlopen
    try:
        body = real_tidal_subscription(dict(GOOD, user_id="208950845", token="tok"))
    finally:
        providers.urllib.request.urlopen = saved
    check("one request", len(seen), 1)
    check("to the account's subscription",
          seen[0].full_url.startswith("https://api.tidal.com/v1/users/208950845/subscription?"),
          True)
    check("for the account's country", "countryCode=CA" in seen[0].full_url, True)
    check("with its token", seen[0].get_header("Authorization"), "Bearer tok")
    check("and the body comes back parsed", body, PAID)


def ready_checks():
    from buskarr import worker

    print("\nworker.ready(): the plan is asked again AFTER the token refresh")
    if not hasattr(worker, "ready"):
        check("worker.ready exists", False, True)
        return
    order = []

    class FakeTidal:
        name = "tidal"

        def __init__(self, can):
            self.can = can

        def refresh(self):
            order.append("refresh")
            return True

        def plan(self, force=False):
            order.append(f"plan force={force}")
            return self.can, "the Tidal account is on the free plan"

    class Hung(FakeTidal):
        def refresh(self):
            order.append("refresh")
            raise TimeoutError

    youtube = {"name": "youtube", "provider": object()}
    names = lambda entries: [e["name"] for e in entries]  # noqa: E731

    got = worker.ready([{"name": "tidal", "provider": FakeTidal(False)}, youtube])
    check("a refusing plan drops tidal for the cycle", names(got), ["youtube"])
    check("refresh first, then a forced check", order, ["refresh", "plan force=True"])
    order.clear()
    got = worker.ready([{"name": "tidal", "provider": FakeTidal(True)}, youtube])
    check("an allowing plan keeps tidal first", names(got), ["tidal", "youtube"])
    got = worker.ready([{"name": "tidal", "provider": FakeTidal(None)}, youtube])
    check("an unanswered check keeps tidal", names(got), ["tidal", "youtube"])
    order.clear()
    got = worker.ready([{"name": "tidal", "provider": Hung(False)}, youtube])
    check("a failed refresh is still followed by the check", order,
          ["refresh", "plan force=True"])
    check("and a refusing plan still drops tidal", names(got), ["youtube"])
    check("nothing to drop when tidal is absent", names(worker.ready([youtube])), ["youtube"])


if __name__ == "__main__":
    sys.exit(run())
