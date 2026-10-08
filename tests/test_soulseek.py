"""Soulseek has to produce candidates, and what it queues has to be picked up.

Found 2026-10-08: from August to October this provider produced not one candidate. slskd's
``GET /searches/{id}`` answers ``responses: []`` whatever the search found (the responses are their
own resource, ``/searches/{id}/responses``), so every want went on to YouTube while slskd logged
two hundred responses for the same search. And a file it did queue at a peer was only placed by the
harvest, which ran only while a torrent grab was outstanding.

The fake here answers exactly what slskd 0.26 answered on the live server: a completed search with
an empty ``responses`` list, and the files under ``/responses``.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from buskarr import db, providers, worker  # noqa: E402

failures = []


def check(label, got, expect):
    ok = got == expect
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f"  got {got!r}"))
    if not ok:
        failures.append(label)


WANT = {"artist": "Linkin Park", "title": "Lying from You", "duration": 175.0}
RESPONSES = [{
    "username": "peer-one", "queueLength": 0,
    "files": [{"filename": "@@music\\Linkin Park\\Meteora\\03 - Lying From You.flac",
               "size": 21000000, "length": 175},
              {"filename": "@@music\\Linkin Park\\Meteora\\cover.jpg", "size": 90000}],
}]


class Slskd:
    """The handful of slskd answers a search touches, with every call written down."""

    def __init__(self, responses=RESPONSES, fail_responses=False):
        self.responses, self.fail_responses, self.calls = responses, fail_responses, []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        if (method, path) == ("POST", "searches"):
            return {"id": "s-1", "state": "InProgress"}
        if (method, path) == ("GET", "searches/s-1"):
            # What the live server answers: complete, counted, and no responses in it.
            return {"id": "s-1", "state": "Completed, TimedOut", "responseCount": 1,
                    "fileCount": 2, "responses": []}
        if (method, path) == ("GET", "searches/s-1/responses"):
            if self.fail_responses:
                raise OSError("connection reset")
            return self.responses
        if (method, path) == ("DELETE", "searches/s-1"):
            return None
        raise AssertionError(f"unexpected slskd call {method} {path}")


def run():
    saved = (providers.SLSKD_URL, providers.SLSKD_KEY, providers.time.sleep)
    providers.SLSKD_URL, providers.SLSKD_KEY = "http://slskd.invalid", "k"
    providers.time.sleep = lambda seconds: None
    try:
        print("a search's candidates come from its responses")
        slsk = providers.Soulseek()
        api = Slskd()
        slsk._api = api
        found = slsk.search(dict(WANT))
        check("the audio file is a candidate, the cover is not",
              [c["filename"] for c in found], [RESPONSES[0]["files"][0]["filename"]])
        check("with its peer, size and length",
              (found[0]["username"], found[0]["size"], found[0]["duration"]),
              ("peer-one", 21000000, 175.0))
        check("read from the responses resource", ("GET", "searches/s-1/responses") in api.calls, True)
        check("and the search is deleted afterwards", api.calls[-1], ("DELETE", "searches/s-1"))

        print("\nresponses that cannot be read are no candidates, and the search still goes")
        api = Slskd(fail_responses=True)
        slsk._api = api
        check("nothing found", slsk.search(dict(WANT)), [])
        check("the search is deleted anyway", api.calls[-1], ("DELETE", "searches/s-1"))
    finally:
        providers.SLSKD_URL, providers.SLSKD_KEY, providers.time.sleep = saved

    tmp = tempfile.mkdtemp(prefix="buskarr-slsk-")
    conn = db.init(os.path.join(tmp, "b.db"))
    want_id, _ = db.add_want(conn, "Linkin Park", "Lying from You", duration=175.0,
                             requested_by="defender")

    print("\nharvest runs while a Soulseek download is outstanding, grab or no grab")
    ran = []
    from buskarr import harvest
    saved_harvest = harvest.harvest
    harvest.harvest = lambda conn, dry_run=False, log=None: ran.append(dry_run) or {}
    try:
        worker.run_harvest(conn)
        check("nothing queued anywhere: no harvest", ran, [])
        conn.execute("UPDATE wants SET status=?, provider=?, note=? WHERE id=?",
                     (db.STATUS_SEARCHING, "soulseek", "queued at peer", want_id))
        conn.commit()
        worker.run_harvest(conn)
        check("a want queued at a Soulseek peer: harvest runs, for real", ran, [False])
    finally:
        harvest.harvest = saved_harvest

    print("\na want whose peer never delivered goes to the next provider, once")
    asked = []

    class Fake:
        def __init__(self, name):
            self.name = name

        def search(self, want):
            asked.append(self.name)
            return []

    provs = [{"provider": Fake("soulseek")}, {"provider": Fake("youtube")}]
    conn.execute("UPDATE wants SET status=?, note=? WHERE id=?",
                 (db.STATUS_PENDING, "peer queue timed out; searching again", want_id))
    conn.commit()
    want = conn.execute("SELECT * FROM wants WHERE id=?", (want_id,)).fetchone()
    check("the reaper's want is recognised", worker._peer_never_delivered(want), True)
    worker.attempt(conn, want, provs)
    check("Soulseek is skipped, YouTube is asked", asked, ["youtube"])
    want = conn.execute("SELECT * FROM wants WHERE id=?", (want_id,)).fetchone()
    asked.clear()
    worker.attempt(conn, want, provs)
    check("and the next attempt asks Soulseek again, even after finding nothing",
          asked, ["soulseek", "youtube"])
    conn.close()

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all checks passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
