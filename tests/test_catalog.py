"""Regression cases for cross-source search merging.

What must hold: two catalogues listing the SAME recording collapse to one row that credits both,
and two catalogues listing DIFFERENT artists never do — including when the credits are non-Latin,
where ``db.norm`` folds every name to the empty string. A wrongly merged row is worse than a
duplicate one: it hides an artist from the results and claims agreement that never happened.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from buskarr import catalog  # noqa: E402

bad = 0


def check(label, ok, detail=""):
    global bad
    if not ok:
        bad += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{('  — ' + detail) if detail else ''}")


def track(source, artist, title, duration, **kw):
    return dict({"source": source, "artist": artist, "title": title,
                 "duration": duration}, **kw)


def merge(*rows):
    return catalog.merge_tracks([[r] for r in rows])


print("merge_tracks — same recording from several catalogues")
m = merge(track("deezer", "Amanda McBroom", "The Rose", 197.0),
          track("itunes", "amanda mcbroom", "The Rose", 197.03))
check("one row for one recording", len(m) == 1, f"{len(m)}")
check("both catalogues credited", len(m) == 1 and m[0]["sources"] == ["deezer", "itunes"])

m = merge(track("deezer", "Beyoncé", "Halo", 261.0),
          track("itunes", "Beyonce", "Halo", 261.5))
check("accents fold, so one row", len(m) == 1, f"{len(m)}")

m = merge(track("deezer", "Amanda McBroom", "The Rose", 197.0),
          track("itunes", "Amanda McBroom", "The Rose", 289.3))
check("a different length is a different recording", len(m) == 2, f"{len(m)}")

m = merge(track("deezer", "The Longest Johns", "Bones in the Ocean", 200.0),
          track("itunes", "The Longest Johns", "Bones in the Ocean (live)", 200.0))
check("a parenthetical is a different recording", len(m) == 2, f"{len(m)}")

print("merge_tracks — distinct artists must never collapse")
# db.norm strips to [a-z0-9], so both of these normalised to "" and merged into one row that
# reported two catalogues as agreeing on a song neither pair had in common.
m = merge(track("deezer", "坂本龍一", "Merry Christmas", 300.0),
          track("itunes", "宇多田ヒカル", "Merry Christmas", 301.0))
check("non-Latin credits stay separate", len(m) == 2, f"{len(m)}")
check("neither row claims the other's catalogue",
      len(m) == 2 and all(len(r["sources"]) == 1 for r in m))

m = merge(track("deezer", "!!!", "Untitled", 200.0),
          track("itunes", "???", "Untitled", 201.0))
check("credits that fold to nothing stay separate", len(m) == 2, f"{len(m)}")

m = merge(track("deezer", "Bette Midler", "The Rose", 217.0),
          track("itunes", "Conway Twitty", "The Rose", 218.0))
check("two covers of one song stay separate", len(m) == 2, f"{len(m)}")

print("merge_tracks — ordering and hygiene")
# Relevance order is the most valuable thing a catalogue reports; agreement only breaks ties.
m = catalog.merge_tracks([
    [track("deezer", "Filler", "Filler", 100.0),
     track("deezer", "Amanda McBroom", "The Rose", 197.0)],
    [track("itunes", "Amanda McBroom", "The Rose", 197.0)]])
check("best rank in any source wins", m[0]["title"] == "The Rose", m[0]["title"])

m = merge(track("deezer", "", "The Rose", 197.0),
          track("itunes", "Amanda McBroom", "", 197.0),
          track("musicbrainz", "Amanda McBroom", "The Rose", 197.0))
check("rows missing an artist or a title are dropped", len(m) == 1, f"{len(m)}")

m = merge(track("deezer", "Amanda McBroom", "The Rose", 197.0, album=None, year=None),
          track("itunes", "Amanda McBroom", "The Rose", 197.0, album="Portraits", year="1994"))
check("the copy with more metadata fills the gaps",
      len(m) == 1 and m[0]["album"] == "Portraits" and m[0]["year"] == "1994")

print(f"\n{bad} failure(s)")
sys.exit(1 if bad else 0)
