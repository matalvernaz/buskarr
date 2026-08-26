"""Every audio format buskarr can place must come out of ``worker.tag`` tagged.

Two defects are locked down here.

``.mp3`` and ``.opus`` fell off the end of ``tag`` and returned False, so the file kept whatever
tags the source shipped — or none. That was invisible while downloads were Tidal FLAC and YouTube
M4A, and stopped being invisible once ``grab.py`` started importing torrent and usenet releases,
which are overwhelmingly MP3: ``harvest.py`` places the file and then calls this, so those landed in
the library untagged. ``.opus`` additionally needs ``OggOpus`` rather than ``OggVorbis``, which
rejects it outright — the same trap that left 18 files unrepairable in ``repair.py``.

The track number now follows ``destination``'s rule exactly: written only when the want has a real
album, because an album-less want files into "Singles", which is a bucket and not a release. The old
``int(track or 1)`` default stamped every song in that bucket "01", so a directory holding unrelated
singles read as one broken album — the filename side already knew better.

The fixtures are a twentieth of a second of silence in each container, base64'd rather than
generated, because CI installs mutagen and nothing else.
"""
import base64
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mutagen.flac import FLAC  # noqa: E402
from mutagen.id3 import ID3  # noqa: E402
from mutagen.mp4 import MP4  # noqa: E402
from mutagen.oggopus import OggOpus  # noqa: E402

from buskarr import worker  # noqa: E402

SILENCE = {
    ".flac": (
        "ZkxhQwAAACICQAJAAAANAAANAfQA8AAAAZBrQxvy2nyTErPlwh5n9lkbBAAALAwAAABMYXZmNjEuNy4xMDMB"
        "AAAAFAAAAGVuY29kZXI9TGF2ZjYxLjcuMTAzgQAAAP/4dAgAAY8kAAAAfV8="
    ),
    ".opus": (
        "T2dnUwACAAAAAAAAAABwQDnOAAAAAK0Uwv0BE09wdXNIZWFkAQE4AUAfAAAAAABPZ2dTAAAAAAAAAAAAAHBA"
        "Oc4BAAAAdqyV3QE9T3B1c1RhZ3MMAAAATGF2ZjYxLjcuMTAzAQAAAB0AAABlbmNvZGVyPUxhdmM2MS4xOS4x"
        "MDEgbGlib3B1c09nZ1MABJgKAAAAAAAAcEA5zgIAAABQ5V87AwMDA5j//pj//pj//g=="
    ),
    ".m4a": (
        "AAAAHGZ0eXBNNEEgAAACAE00QSBpc29taXNvMgAAAAhmcmVlAAAAIW1kYXTeAgBMYXZjNjEuMTkuMTAxAAIw"
        "QA4BGCAHAAADAm1vb3YAAABsbXZoZAAAAAAAAAAAAAAAAAAAA+gAAAAyAAEAAAEAAAAAAAAAAAAAAAABAAAA"
        "AAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIAAAIt"
        "dHJhawAAAFx0a2hkAAAAAwAAAAAAAAAAAAAAAQAAAAAAAAAyAAAAAAAAAAAAAAABAQAAAAABAAAAAAAAAAAA"
        "AAAAAAAAAQAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAJGVkdHMAAAAcZWxzdAAAAAAAAAABAAAAMgAA"
        "BAAAAQAAAAABpW1kaWEAAAAgbWRoZAAAAAAAAAAAAAAAAAAAH0AAAAWQVcQAAAAAAC1oZGxyAAAAAAAAAABz"
        "b3VuAAAAAAAAAAAAAAAAU291bmRIYW5kbGVyAAAAAVBtaW5mAAAAEHNtaGQAAAAAAAAAAAAAACRkaW5mAAAA"
        "HGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAARRzdGJsAAAAanN0c2QAAAAAAAAAAQAAAFptcDRhAAAAAAAA"
        "AAEAAAAAAAAAAAABABAAAAAAH0AAAAAAADZlc2RzAAAAAAOAgIAlAAEABICAgBdAFQAAAAAAu4AAAARjBYCA"
        "gAUViFblAAaAgIABAgAAACBzdHRzAAAAAAAAAAIAAAABAAAEAAAAAAEAAAGQAAAAHHN0c2MAAAAAAAAAAQAA"
        "AAEAAAACAAAAAQAAABxzdHN6AAAAAAAAAAAAAAACAAAAFQAAAAQAAAAUc3RjbwAAAAAAAAABAAAALAAAABpz"
        "Z3BkAQAAAHJvbGwAAAACAAAAAf//AAAAHHNiZ3AAAAAAcm9sbAAAAAEAAAACAAAAAQAAAGF1ZHRhAAAAWW1l"
        "dGEAAAAAAAAAIWhkbHIAAAAAAAAAAG1kaXJhcHBsAAAAAAAAAAAAAAAALGlsc3QAAAAkqXRvbwAAABxkYXRh"
        "AAAAAQAAAABMYXZmNjEuNy4xMDM="
    ),
    ".mp3": (
        "SUQzBAAAAAAAIlRTU0UAAAAOAAADTGF2ZjYxLjcuMTAzAAAAAAAAAAAAAAD/4zjAAAAAAAAAAAAASW5mbwAA"
        "AA8AAAADAAABsACqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqrV1dXV1dXV1dXV1dXV1dXV1dXV"
        "1dXV1dXV1dXV1dXV1dX///////////////////////////////////////////8AAAAATGF2YzYxLjE5AAAA"
        "AAAAAAAAAAAAJALwAAAAAAAAAbD3CmUrAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
        "AAAAAAAAAAD/4xjEAAAAA0gAAAAATEFNRTMuMTAwVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
        "VVVVVVVVVVVVVVVVVVVVVVX/4xjEOwAAA0gAAAAAVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
        "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/4xjEdgAAA0gAAAAAVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
        "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVU="
    ),
}

bad = 0


def check(label, ok, detail=""):
    global bad
    if not ok:
        bad += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{('  — ' + detail) if detail else ''}")


def want(album="Red", year="2012", track_no=3, title="All Too Well",
         artist="Taylor Swift feat. Ed Sheeran", lead="Taylor Swift"):
    return {"title": title, "artist": artist, "artist_lead": lead,
            "album": album, "year": year, "track_no": track_no}


def read_back(path):
    """(title, artist, albumartist, album, year, track) as the file now reports them."""
    ext = os.path.splitext(path)[1]
    if ext == ".flac":
        m = FLAC(path)
    elif ext == ".opus":
        m = OggOpus(path)
    elif ext == ".mp3":
        m = ID3(path)
        return (str(m["TIT2"]), str(m["TPE1"]), str(m["TPE2"]),
                str(m["TALB"]) if "TALB" in m else None,
                str(m["TDRC"]) if "TDRC" in m else None,
                str(m["TRCK"]) if "TRCK" in m else None)
    else:
        m = MP4(path)
        return (m["\xa9nam"][0], m["\xa9ART"][0], m["aART"][0],
                m.get("\xa9alb", [None])[0], m.get("\xa9day", [None])[0],
                str(m["trkn"][0][0]) if "trkn" in m else None)
    one = lambda k: m[k][0] if k in m else None  # noqa: E731
    return (one("title"), one("artist"), one("albumartist"), one("album"),
            one("date"), one("tracknumber"))


tmp = tempfile.mkdtemp(prefix="buskarr-tag-")
try:
    for ext in (".flac", ".opus", ".m4a", ".mp3"):
        print(f"{ext} — a want with a real album")
        path = os.path.join(tmp, f"album{ext}")
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(SILENCE[ext]))
        check("tag() reports success", worker.tag(path, want()) is True)
        title, artist, albumartist, album, year, track = read_back(path)
        check("title", title == "All Too Well", repr(title))
        check("artist is the full credit",
              artist == "Taylor Swift feat. Ed Sheeran", repr(artist))
        check("albumartist is the lead", albumartist == "Taylor Swift", repr(albumartist))
        check("album", album == "Red", repr(album))
        check("year", (year or "").startswith("2012"), repr(year))
        check("track number is the want's own", (track or "").startswith("3"), repr(track))

        print(f"{ext} — an album-less want files into Singles and carries no position")
        path = os.path.join(tmp, f"single{ext}")
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(SILENCE[ext]))
        check("tag() reports success", worker.tag(path, want(album=None, track_no=None)) is True)
        title, artist, albumartist, album, year, track = read_back(path)
        check("title still written", title == "All Too Well", repr(title))
        check("no album invented from the song title", album in (None, ""), repr(album))
        check("NO track number — Singles is a bucket, not a release",
              track in (None, ""), repr(track))
        # destination() writes no number into the filename for the same want; the two must agree.
        check("filename agrees: no leading position",
              not os.path.basename(
                  worker.destination(want(album=None, track_no=None), ext)).startswith("0"),
              worker.destination(want(album=None, track_no=None), ext))
        print()

    print("an unsupported container is still declined rather than guessed at")
    path = os.path.join(tmp, "thing.wav")
    with open(path, "wb") as fh:
        fh.write(b"RIFF????WAVE")
    check("tag() returns False", worker.tag(path, want()) is False)

    print("\na file with no tag block at all still gets one")
    path = os.path.join(tmp, "stripped.mp3")
    with open(path, "wb") as fh:
        fh.write(base64.b64decode(SILENCE[".mp3"]))
    ID3(path).delete(path)
    check("tag() writes a fresh ID3 header", worker.tag(path, want()) is True)
    check("and it reads back", read_back(path)[0] == "All Too Well")

    print("\ntagging never loses an acquired file")
    path = os.path.join(tmp, "corrupt.flac")
    with open(path, "wb") as fh:
        fh.write(b"not a flac at all")
    check("a broken container is reported, not raised", worker.tag(path, want()) is False)
    check("the file is left alone", os.path.getsize(path) == len(b"not a flac at all"))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n{bad} failure(s)")
sys.exit(1 if bad else 0)
