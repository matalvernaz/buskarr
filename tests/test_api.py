"""The JSON API. Runs INSIDE the container — it imports fastapi, which is not
installed on the host:

    docker exec -w /app -e PYTHONPATH=/app buskarr python3 tests/test_api.py

It builds its OWN database in a temporary directory and never touches the live
one. `BUSKARR_DB` is read at import time, so it is set before `buskarr.db` is
imported and there is no window in which the real path is in play. A suite that
writes to the production database has destroyed one before; a want left behind
in this one would also point the worker at an artist that does not exist.
"""
import os
import sys
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_scratch = tempfile.mkdtemp(prefix="buskarr-api-test-")
os.environ["BUSKARR_DB"] = os.path.join(_scratch, "buskarr-test.db")
os.environ["NUDGE_FILE"] = os.path.join(_scratch, "nudge")
KEY = "test-key-not-a-real-one"
os.environ["BUSKARR_API_KEY"] = KEY

from fastapi import FastAPI                        # noqa: E402
from fastapi.testclient import TestClient          # noqa: E402

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


# The gate. An API that mutates the library must not be reachable merely
# because the package was installed.
check(api.API_KEY == KEY, "the key is read from the environment")

app = FastAPI()
app.include_router(api.router)
client = TestClient(app, raise_server_exceptions=False)
auth = {"X-Api-Key": KEY}

check(client.get("/api/v1/capabilities").status_code == 401,
      "no key is refused")
check(client.get("/api/v1/capabilities",
                 headers={"X-Api-Key": "wrong"}).status_code == 401,
      "a wrong key is refused")

r = client.get("/api/v1/capabilities", headers=auth)
check(r.status_code == 200, "the right key is accepted")
check(set(r.json()["units"]) == {"artist", "album", "track"},
      "all three units are offered")

r = client.get("/api/v1/search", params={"q": "", "unit": "track"}, headers=auth)
check(r.status_code == 200 and r.json()["results"] == [],
      "an empty query returns nothing rather than searching")
check(client.get("/api/v1/search", params={"q": "x", "unit": "nonsense"},
                 headers=auth).status_code == 400,
      "an unknown unit is refused")

check(client.post("/api/v1/add", json={"unit": "track", "artist": "", "title": ""},
                  headers=auth).status_code == 400,
      "a track with no artist or title is refused")
check(client.post("/api/v1/add", json={"unit": "album", "ref": ""},
                  headers=auth).status_code == 400,
      "an album with no catalogue ref is refused")
check(client.post("/api/v1/add", json={"unit": "nonsense", "ref": "1"},
                  headers=auth).status_code == 400,
      "an unknown unit cannot be added")

check(client.get("/api/v1/state", params={"reference": "nonsense"},
                 headers=auth).status_code == 400,
      "a malformed reference is refused")
check(client.get("/api/v1/state", params={"reference": "want:99999999"},
                 headers=auth).status_code == 404,
      "a reference to nothing is a 404")

# A track add, against the scratch database.
r = client.post("/api/v1/add", headers=auth, json={
    "unit": "track", "artist": "An Artist", "title": "A Song",
    "requestedBy": "test"})
check(r.status_code == 200, "a well-formed track add is accepted")
reference = r.json().get("reference", "")
check(reference.startswith("want:"), "a track add returns a want reference")
check(r.json()["created"] is True, "and reports that it created one")

r = client.post("/api/v1/add", headers=auth, json={
    "unit": "track", "artist": "An Artist", "title": "A Song"})
check(r.json()["created"] is False, "adding the same track again creates nothing")
check(r.json()["reference"] == reference, "and points at the same want")

r = client.get("/api/v1/state", params={"reference": reference}, headers=auth)
check(r.status_code == 200, "its state can be read back")
check(r.json()["total"] == 1, "a track is one of one")
check(r.json()["have"] == 0, "and is not in the library yet")

# A bulk add is a job, and a queued job has no batch to count yet.
r = client.post("/api/v1/add", headers=auth, json={
    "unit": "artist", "ref": "12345", "source": "deezer", "artist": "An Artist"})
job_reference = r.json()["reference"]
check(job_reference.startswith("job:"), "an artist add returns a job reference")
r = client.get("/api/v1/state", params={"reference": job_reference}, headers=auth)
check(r.json()["state"] == db.JOB_QUEUED, "a fresh job reads as queued")
check(r.json()["total"] is None,
      "and reports no total, rather than a zero that reads as failure")

r = client.post("/api/v1/cancel", json={"reference": job_reference}, headers=auth)
check(r.json()["removed"] is True, "a queued job can be taken out of the queue")

r = client.post("/api/v1/cancel", json={"reference": reference}, headers=auth)
check(r.status_code == 200 and r.json()["removed"] is True,
      "a pending want can be cancelled")

# Cancelling must never remove audio already downloaded.
conn = db.connect()
want_id, _ = db.add_want(conn, "Held Artist", "Held Song", requested_by="test")
conn.execute("UPDATE wants SET status=?, provider='tidal' WHERE id=?",
             (db.STATUS_HAVE, want_id))
conn.commit()
conn.close()
r = client.post("/api/v1/cancel", json={"reference": f"want:{want_id}"},
                headers=auth)
check(r.json()["removed"] is False,
      "a want already downloaded is not deleted by a cancel")

shutil.rmtree(_scratch, ignore_errors=True)

for failure in failures:
    print(f"  FAIL {failure}")
print(f"api: {passed} passed, {len(failures)} failed")
sys.exit(1 if failures else 0)
