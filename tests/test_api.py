"""The JSON API. Runs INSIDE the container — it imports fastapi, which is not
installed on the host:

    incus exec dockge -- bash -c 'docker cp /opt/stacks/buskarr/tests/test_api.py \
        buskarr:/tmp/ && docker exec -w /app -e PYTHONPATH=/app buskarr \
        python3 /tmp/test_api.py'

It builds its OWN database in a temporary directory and never touches the live
one. `BUSKARR_DB` is read at import time, so it is set before `buskarr.db` is
imported and there is no window in which the real path is in play. A suite that
writes to the production database has destroyed one before; a want left behind
in this one would also point the worker at an artist that does not exist.

The routes are called as functions rather than over HTTP. Starlette's test
client needs `httpx`, which this image does not carry and which would be a
dependency for the whole service bought for a test — and the transport is not
what is worth testing here. That the router mounts and answers is verified
against the running container with curl.
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_scratch = tempfile.mkdtemp(prefix="buskarr-api-test-")
os.environ["BUSKARR_DB"] = os.path.join(_scratch, "buskarr-test.db")
os.environ["NUDGE_FILE"] = os.path.join(_scratch, "nudge")
KEY = "test-key-not-a-real-one"
os.environ["BUSKARR_API_KEY"] = KEY

from fastapi import HTTPException                  # noqa: E402

from buskarr import api, db                        # noqa: E402

assert db.DB_PATH.startswith(_scratch), \
    f"refusing to run: db path is {db.DB_PATH!r}, not the scratch database"
db.init().close()

passed, failures = 0, []


def check(condition, description):
    global passed
    if condition:
        passed += 1
    else:
        failures.append(description)


def status_of(fn, *args, **kwargs):
    """The HTTP status a route raises, or 200 when it returns normally."""
    try:
        fn(*args, **kwargs)
    except HTTPException as exc:
        return exc.status_code
    return 200


# The gate. An API that mutates the library must not be reachable merely
# because the package was installed.
check(api.API_KEY == KEY, "the key is read from the environment")
check(status_of(api.capabilities, key=None) == 401, "no key is refused")
check(status_of(api.capabilities, key="wrong") == 401,
      "a wrong key is refused")
check(status_of(api.capabilities, key=KEY + "x") == 401,
      "a key with the right prefix is refused")

body = api.capabilities(key=KEY)
check(set(body["units"]) == {"artist", "album", "track"},
      "all three units are offered")

r = api.search(q="", unit="track", limit=20, key=KEY)
check(r["results"] == [],
      "an empty query returns nothing rather than searching")
check(status_of(api.search, q="x", unit="nonsense", limit=20, key=KEY) == 400,
      "an unknown unit is refused")
check(status_of(api.search, q="x", unit="track", limit=20, key=None) == 401,
      "search needs the key too")

check(status_of(api.add, unit="track", artist="", title="", key=KEY) == 400,
      "a track with no artist or title is refused")
check(status_of(api.add, unit="album", ref="", key=KEY) == 400,
      "an album with no catalogue ref is refused")
check(status_of(api.add, unit="nonsense", ref="1", key=KEY) == 400,
      "an unknown unit cannot be added")
check(status_of(api.add, unit="track", artist="a", title="b",
                key=None) == 401, "adding needs the key")

check(status_of(api.state, reference="nonsense", key=KEY) == 400,
      "a malformed reference is refused")
check(status_of(api.state, reference="want:99999999", key=KEY) == 404,
      "a reference to nothing is a 404")

# A track add, against the scratch database.
r = api.add(unit="track", artist="An Artist", title="A Song",
            requested_by="test", key=KEY)
reference = r["reference"]
check(reference.startswith("want:"), "a track add returns a want reference")
check(r["created"] is True, "and reports that it created one")

again = api.add(unit="track", artist="An Artist", title="A Song", key=KEY)
check(again["created"] is False, "adding the same track again creates nothing")
check(again["reference"] == reference, "and points at the same want")

r = api.state(reference=reference, key=KEY)
check(r["total"] == 1, "a track is one of one")
check(r["have"] == 0, "and is not in the library yet")

# A bulk add is a job, and a queued job has no batch to count yet.
r = api.add(unit="artist", ref="12345", source="deezer", artist="An Artist",
            key=KEY)
job_reference = r["reference"]
check(job_reference.startswith("job:"), "an artist add returns a job reference")
r = api.state(reference=job_reference, key=KEY)
check(r["state"] == db.JOB_QUEUED, "a fresh job reads as queued")
check(r["total"] is None,
      "and reports no total, rather than a zero that reads as failure")

check(api.cancel(reference=job_reference, key=KEY)["removed"] is True,
      "a queued job can be taken out of the queue")
check(api.cancel(reference=reference, key=KEY)["removed"] is True,
      "a pending want can be cancelled")

# Cancelling must never remove audio already downloaded.
conn = db.connect()
want_id, _ = db.add_want(conn, "Held Artist", "Held Song", requested_by="test")
conn.execute("UPDATE wants SET status=?, provider='tidal' WHERE id=?",
             (db.STATUS_HAVE, want_id))
conn.commit()
conn.close()
check(api.cancel(reference=f"want:{want_id}", key=KEY)["removed"] is False,
      "a want already downloaded is not deleted by a cancel")

shutil.rmtree(_scratch, ignore_errors=True)

for failure in failures:
    print(f"  FAIL {failure}")
print(f"api: {passed} passed, {len(failures)} failed")
sys.exit(1 if failures else 0)
