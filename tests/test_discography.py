"""What an artist add is allowed to take from MusicBrainz, and what it must leave behind.

The failure this locks down: Taylor Swift has 2436 MusicBrainz releases against a 500 cap, so the
walk covered a fifth of the discography — and the fifth it covered was demo CDs, radio promos and
music-video releases, because those are simply what the browse returned first. Every album from 2013
on arrived instead through the recording pass, which carries no release title, so 256 of 569 wants
had no album, no track number, and filed into "Singles".

Three rules follow from that, and each is a SKIP rather than a rank. Ranking only decides which
release claims a title that two releases share; a promo interview has a title nothing else carries,
so no amount of demotion stops it becoming a want.

  * only released-to-listeners statuses count (Official; absent is not rejected),
  * release groups that are not music are dropped whole (interview, spokenword, audiobook, …),
  * video recordings are dropped — the audio twin is a separate MusicBrainz entity, so nothing is
    lost, and leaving them in is what let a want for "“Tim McGraw” Video" be satisfied by the audio
    track of the same length.

Live and remix releases are deliberately NOT skipped, only ranked below the studio original: they
carry songs that exist nowhere else, and this library holds live albums on purpose.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from buskarr import bulk, catalog  # noqa: E402

bad = 0


def check(label, ok, detail=""):
    global bad
    if not ok:
        bad += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{('  — ' + detail) if detail else ''}")


def rec(title, video=False, length=200000):
    return {"id": f"rec-{title}", "title": title, "length": length, "video": video,
            "artist-credit": [{"name": "Taylor Swift",
                               "artist": {"id": "ts", "name": "Taylor Swift"}}]}


def release(title, status="Official", primary="Album", secondary=(), tracks=(), date="2010"):
    return {
        "id": f"rel-{title}", "title": title, "status": status, "date": date,
        "artist-credit": [{"name": "Taylor Swift", "artist": {"id": "ts", "name": "Taylor Swift"}}],
        "release-group": {"primary-type": primary, "secondary-types": list(secondary)},
        "media": [{"tracks": [{"title": r["title"], "length": r["length"], "recording": r}
                              for r in tracks]}],
    }


def fake_mb(releases, recordings):
    """Stand in for ``catalog._mb_get``, paging both browse endpoints over fixed lists."""
    def get(url):
        if url.startswith("artist/"):
            return {"name": "Taylor Swift"}
        rows, key, count_key = (releases, "releases", "release-count") if url.startswith("release?") \
            else (recordings, "recordings", "recording-count")
        offset = int(url.split("&offset=")[1])
        limit = int(url.split("&limit=")[1].split("&")[0])
        return {key: rows[offset:offset + limit], count_key: len(rows)}
    return get


def catalogue(releases, recordings, **env):
    """Run one MusicBrainz artist_catalogue against fixed data. Returns (tracks, meta)."""
    saved_get, saved = catalog._mb_get, {k: getattr(catalog, k) for k in env}
    catalog._mb_get = fake_mb(releases, recordings)
    for k, v in env.items():
        setattr(catalog, k, v)
    try:
        _, tracks, meta = catalog.MusicBrainz().artist_catalogue("ts")
        return tracks, meta
    finally:
        catalog._mb_get = saved_get
        for k, v in saved.items():
            setattr(catalog, k, v)


def titles(tracks):
    return {t["title"] for t in tracks}


print("release status — only what was released to listeners")
tracks, meta = catalogue([
    release("Speak Now", status="Official", tracks=[rec("Mine")]),
    release("2004 Demo CD", status="Promotion", tracks=[rec("Never Mind")]),
    release("Teardrops on My Guitar (remixes)", status="Bootleg", tracks=[rec("Teardrops")]),
    release("Taylor Swift (deluxe)", status="Withdrawn", tracks=[rec("Invisible")]),
    release("Untyped Release", status=None, tracks=[rec("Ronan")]),
], [])
check("an Official release is kept", "Mine" in titles(tracks))
check("a Promotion release is skipped", "Never Mind" not in titles(tracks))
check("a Bootleg release is skipped", "Teardrops" not in titles(tracks))
check("a Withdrawn release is skipped", "Invisible" not in titles(tracks))
check("a release with no status at all is kept — absent is not rejected",
      "Ronan" in titles(tracks), str(sorted(titles(tracks))))

tracks, _ = catalogue([release("2004 Demo CD", status="Promotion", tracks=[rec("Never Mind")])],
                      [], MB_STATUSES=frozenset({"official", "promotion"}))
check("MB_STATUSES widens the filter", "Never Mind" in titles(tracks))

print("\nrelease groups that are not music")
tracks, _ = catalogue([
    release("Generic Interview", secondary=["Interview"], tracks=[rec("Generic Interview")]),
    release("The Audiobook", secondary=["Audiobook"], tracks=[rec("Chapter One")]),
    release("Speak Now: World Tour Live", secondary=["Live"], tracks=[rec("Sparks Fly (live)")]),
    release("Red", tracks=[rec("All Too Well")]),
], [])
check("an interview release is skipped whole", "Generic Interview" not in titles(tracks))
check("an audiobook release is skipped whole", "Chapter One" not in titles(tracks))
check("a LIVE release is kept — demoted later, never dropped",
      "Sparks Fly (live)" in titles(tracks))
check("an ordinary album is untouched", "All Too Well" in titles(tracks))

print("\nvideo recordings")
tracks, _ = catalogue(
    [release("Taylor Swift (deluxe)", tracks=[rec("Tim McGraw"),
                                              rec("“Tim McGraw” Video", video=True)])],
    [rec("A Look Behind the Curtain", video=True), rec("Ronan")])
check("the audio recording is kept", "Tim McGraw" in titles(tracks))
check("its video twin is dropped from the release pass",
      "“Tim McGraw” Video" not in titles(tracks))
check("a video-only recording is dropped from the recording pass",
      "A Look Behind the Curtain" not in titles(tracks))
check("a normal album-less recording still comes through", "Ronan" in titles(tracks))

print("\nthe recording pass still fills genuine gaps, and album stays None")
tracks, _ = catalogue([release("Red", tracks=[rec("All Too Well")])],
                      [rec("All Too Well"), rec("Ronan")])
allt = [t for t in tracks if t["title"] == "All Too Well"]
check("a recording already on a release is not duplicated", len(allt) == 1, str(len(allt)))
check("the release copy keeps its album", allt and allt[0]["album"] == "Red")
gap = [t for t in tracks if t["title"] == "Ronan"]
check("a release-less song is kept with no invented album",
      len(gap) == 1 and gap[0]["album"] is None and gap[0]["release_type"] == "unreleased")

print("\ntruncation is still reported honestly")
# 150 releases against a 100 cap, because the cap is tested once per PAGE and a page is 100 rows —
# a cap below the page size never trips, which is fine for a runaway guard but makes a smaller
# fixture prove nothing.
many = [release(f"Album {i}", tracks=[rec(f"Song {i}")]) for i in range(150)]
_, meta = catalogue(many, [], MB_MAX_RELEASES=100)
check("hitting the cap reports the catalogue incomplete", meta["complete"] is False)
_, meta = catalogue(many, [], MB_MAX_RELEASES=4000)
check("a walk that finishes reports complete", meta["complete"] is True)
# The cap is a runaway guard, not a budget: it must sit far above any real discography. Taylor
# Swift is the worst case in this library at 2436 releases / 2856 recordings.
check("the release cap clears the largest real discography", catalog.MB_MAX_RELEASES >= 2436,
      str(catalog.MB_MAX_RELEASES))
check("the recording cap clears it too", catalog.MB_MAX_RECORDINGS >= 2856,
      str(catalog.MB_MAX_RECORDINGS))

print("\nrelease_order — derivative releases rank below the studio original")


def rank(primary, secondary=()):
    return bulk.release_order([{"release_type": primary, "release_secondary": list(secondary),
                                "year": "2010", "release_title": "x"}])[0]


check("a live album ranks below a plain single",
      rank("Album", ["Live"]) > rank("Single"), f"{rank('Album', ['Live'])} vs {rank('Single')}")
check("a remix album ranks below a plain album",
      rank("Album", ["Remix"]) > rank("Album"))
check("a demo album ranks below a plain album", rank("Album", ["Demo"]) > rank("Album"))
check("a live album still ranks above a compilation",
      rank("Album", ["Live"]) < rank("Album", ["Compilation"]))
check("a plain album is unchanged", rank("Album") == bulk.RELEASE_RANK["album"])
check("an unreleased live take is NOT promoted to the derivative rank",
      rank("unreleased", ["Live"]) == bulk.RELEASE_RANK["unreleased"],
      str(rank("unreleased", ["Live"])))

print(f"\n{bad} failure(s)")
sys.exit(1 if bad else 0)
