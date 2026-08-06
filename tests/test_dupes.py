"""Regression cases for audio-identical duplicate detection.

What must hold: identity is the DECODED audio (tags differ on every copy buskarr places, so file
checksums cannot be used); the candidate narrowing never puts genuinely different lengths in one
bucket; the survivor is the copy on the highest-ranked release under its canonical name; and a
file whose audio is unique is never touched.
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from buskarr import db, dupes  # noqa: E402

bad = 0


def check(label, ok, detail=""):
    global bad
    if not ok:
        bad += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{('  — ' + detail) if detail else ''}")


def have_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=30)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


print("=== survivor choice ===")
check("an original album beats a greatest-hits collection",
      dupes._rank("/music/A/First Album (2001)/01 - S.flac")
      < dupes._rank("/music/A/Greatest Hits (2010)/01 - S.flac"))
check("an original album beats a deluxe reissue",
      dupes._rank("/music/A/Ram (2023)/01 - S.flac")
      < dupes._rank("/music/A/Ram (Deluxe Edition) (2024)/01 - S.flac"))
check("a real album beats Singles",
      dupes._rank("/music/A/First Album (2001)/01 - S.flac")
      < dupes._rank("/music/A/Singles/01 - S.flac"))
check("earliest year breaks a tie between two albums",
      dupes._rank("/music/A/X (1990)/01 - S.flac") < dupes._rank("/music/A/Y (1999)/01 - S.flac"))
check("the plainer title wins inside one directory",
      dupes._plainness("/music/A/Alb (1999)/10 - The Blues Man.flac")
      < dupes._plainness("/music/A/Alb (1999)/10 - The Blues Man (A Tribute to Hank).flac"))
check("the NN prefix is not part of the title",
      dupes._title_of("/music/A/Alb (1999)/07 - South Australia.flac") == "South Australia")

print("\n=== candidate narrowing ===")
with tempfile.TemporaryDirectory() as d:
    conn = db.init(os.path.join(d, "t.db"))

    def add(path, artist_lead, duration):
        db.upsert_file(conn, {"path": path, "artist": artist_lead, "title": dupes._title_of(path),
                              "file_title": dupes._title_of(path), "artist_lead": artist_lead,
                              "duration": duration, "codec": "flac", "album": "X"})
        conn.commit()

    add("/music/A/Alb (2001)/01 - Song.flac", "A", 200.0)
    add("/music/A/Hits (2010)/01 - Song (2010 Remaster).flac", "A", 200.5)
    add("/music/A/Alb (2001)/02 - Song (Live at X).flac", "A", 265.0)
    add("/music/A/Alb (2001)/03 - Other.flac", "A", 200.0)
    add("/music/B/Alb (2001)/01 - Song.flac", "B", 200.0)
    groups = dupes.candidates(conn)
    flat = [sorted(os.path.basename(f["path"]) for f in g) for g in groups]
    check("the remaster groups with its original", any(
        len(g) == 2 and any("Remaster" in n for n in g) for g in flat), str(flat))
    check("a different-length take is not a candidate",
          not any(any("Live at X" in n for n in g) for g in flat), str(flat))
    check("a different song sharing a duration is not a candidate",
          not any(any(n.startswith("03") for n in g) for g in flat), str(flat))
    check("another artist's same-titled file is not a candidate",
          all(len(g) == 2 for g in flat), str(flat))

if not have_ffmpeg():
    print("\n=== audio identity: SKIPPED, no ffmpeg on this host ===")
else:
    print("\n=== audio identity beats tag differences ===")
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "tone.flac")
        subprocess.run(["ffmpeg", "-hide_banner", "-v", "quiet", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=3", "-y", src], timeout=120)
        # Same audio, different tags — the case file checksums get wrong.
        a, b = os.path.join(d, "a.flac"), os.path.join(d, "b.flac")
        for path, title in ((a, "Song"), (b, "Song (2016 Remaster)")):
            subprocess.run(["ffmpeg", "-hide_banner", "-v", "quiet", "-i", src, "-c", "copy",
                            "-metadata", f"title={title}", "-y", path], timeout=120)
        other = os.path.join(d, "c.flac")
        subprocess.run(["ffmpeg", "-hide_banner", "-v", "quiet", "-f", "lavfi", "-i",
                        "sine=frequency=880:duration=3", "-y", other], timeout=120)
        ha, hb, hc = dupes.audio_hash(a), dupes.audio_hash(b), dupes.audio_hash(other)
        check("retagged copies of one master hash the same", ha is not None and ha == hb,
              f"{ha} vs {hb}")
        check("different audio hashes differently", hc is not None and hc != ha, f"{hc}")
        check("file checksums would NOT have caught it",
              open(a, "rb").read() != open(b, "rb").read())
        check("an unreadable file yields None, not a crash",
              dupes.audio_hash(os.path.join(d, "missing.flac")) is None)

print(f"\n{bad} failure(s)")
sys.exit(1 if bad else 0)
