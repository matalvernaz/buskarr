"""Regression cases for the artist-credit filter, the lead-artist split, and path safety.

`credited_to` exists to keep other people's recordings out of a discography add. Three classes of bug
have been found in it: it accepted any credit merely *beginning* with the wanted name
("Queen Tribute Band" for "Queen"); it rejected every artist whose name has no ASCII letters
("坂本龍一", "!!!") because the normaliser emptied the string; and it accepted GUEST credits, so
asking for one band queued ten tracks belonging to nine others.

`lead_artist` is the opposite question — which name the file is stored under — and is tested here
because getting the two confused is what the split was for. It must never break a band name apart.

`worker.safe`/`destination` are here because a catalogue value of ".." produced a path resolving
outside the library root, which is the one thing this project must never permit.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from buskarr import worker  # noqa: E402
from buskarr.credit import credited_to, credited_by_id, lead_artist  # noqa: E402

CASES = [
    ("Tom Lehrer", "Tom Lehrer", True, "exact"),
    ("The Lehrer's Band", "Tom Lehrer", False, "tribute act (the original case)"),
    ("Queen Tribute Band", "Queen", False, "prefix leak the panel found"),
    ("Beatles Karaoke Ensemble", "Beatles", False, "prefix leak"),
    ("Tom Lehrer feat. Someone", "Tom Lehrer", True, "feat. credit"),
    ("Tom Lehrer & His Orchestra", "Tom Lehrer", True, "ampersand credit"),
    # Real credits from the artist add that prompted the guest rule. Each of these is somebody
    # else's release, and every one of them was accepted and downloaded.
    ("Celtic Woman feat. The Longest Johns", "The Longest Johns", False, "guest: feat."),
    ("Loveridge Feat. The Longest Johns", "The Longest Johns", False, "guest: capitalised Feat."),
    ("The Yogscast & The Longest Johns", "The Longest Johns", False, "guest: second-billed"),
    ("Natalie Holmes x The Longest Johns", "The Longest Johns", False, "guest: x"),
    ("The Dreadnoughts and the Longest Johns", "The Longest Johns", False, "guest: and"),
    ("The Longest Johns & Lucy Humphris", "The Longest Johns", True, "they lead the collaboration"),
    ("坂本龍一", "坂本龍一", True, "CJK exact — was rejected outright"),
    ("!!!", "!!!", True, "symbol-only name — was rejected outright"),
    ("Би-2", "Би-2", True, "Cyrillic exact"),
    ("宇多田ヒカル", "坂本龍一", False, "different CJK artists"),
    ("", "Tom Lehrer", False, "no credit at all"),
    ("Weird Al Yankovic", "Tom Lehrer", False, "unrelated"),
]
bad = 0
for cand, want, expect, label in CASES:
    got = credited_to(cand, want)
    if got != expect:
        bad += 1
    print(f"  {'ok  ' if got == expect else 'FAIL'} {label[:38]:<40} {cand[:34]!r:<36} -> {got}")
print()
print("=== id filter: ordered position, not membership ===")
ID_CASES = [
    (["123"], "123", True, "sole credit"),
    (["123", "999"], "123", True, "lead of two"),
    (["999", "123"], "123", False, "guest of two — membership used to accept this"),
    (["999"], "123", False, "not credited"),
    ([], "123", None, "no ids: defer to the name check"),
]
for ids, ref, expect, label in ID_CASES:
    got = credited_by_id(ids, ref)
    if got != expect:
        bad += 1
    print(f"  {'ok  ' if got == expect else 'FAIL'} {label[:44]:<46} {ids} -> {got}")
print()
print("=== lead artist: which folder the file goes in ===")
LEAD_CASES = [
    ("The Longest Johns feat. SKÁLD", None, "The Longest Johns", "feat."),
    ("The Longest Johns ft. SKÁLD", None, "The Longest Johns", "ft."),
    ("The Longest Johns (feat. London Symphony Orchestra)", None, "The Longest Johns",
     "parenthesised feat."),
    ("Loveridge Feat. The Longest Johns", None, "Loveridge", "guest trimmed off, lead kept"),
    ("The Longest Johns & Lucy Humphris", "The Longest Johns", "The Longest Johns",
     "ampersand needs the catalogue's answer"),
    ("Simon & Garfunkel", None, "Simon & Garfunkel", "MUST NOT split a band name on &"),
    ("Florence + the Machine", None, "Florence + the Machine", "nor on +"),
    ("Earth, Wind & Fire", None, "Earth, Wind & Fire", "nor on a comma"),
    ("AC/DC", None, "AC/DC", "nor on a slash"),
    ("Ft. Lauderdale Band", None, "Ft. Lauderdale Band", "a name that starts with a marker word"),
    ("", None, "", "empty credit"),
]
for full, known, expect, label in LEAD_CASES:
    got = lead_artist(full, known)
    if got != expect:
        bad += 1
    print(f"  {'ok  ' if got == expect else 'FAIL'} {label[:40]:<42} {full[:34]!r:<36} -> {got!r}")
print()
print("=== path safety ===")
# ":" and "?" are NTFS-illegal, so a component carrying one is unreachable over the SMB share of
# this same tree — and ":" is what split one album into "Genuine: …" and "Genuine_ …" spellings.
PATHS = [("..", "Unknown"), (".", "Unknown"), ("", "Unknown"), ("AC/DC", "AC_DC"),
         ("坂本龍一", "坂本龍一"), ("../../etc", "_.._etc"),
         ("Genuine: The Alan Jackson Story", "Genuine_ The Alan Jackson Story"),
         ("Beer:10", "Beer_10"), ("Are You Washed in the Blood?", "Are You Washed in the Blood_"),
         ('He said "no"', "He said _no_"), ("a*b<c>d|e", "a_b_c_d_e")]
for raw, expect in PATHS:
    got = worker.safe(raw)
    if got != expect:
        bad += 1
    print(f"  {'ok  ' if got == expect else 'FAIL'} safe({raw!r}) = {got!r}")
for artist, album in ((("..", "..")), ("../..", "x")):
    d = worker.destination({"artist": artist, "album": album, "title": "S", "year": None}, ".flac")
    inside = worker.within_library(d)
    if not inside:
        bad += 1
    print(f"  {'ok  ' if inside else 'FAIL'} destination(artist={artist!r}) stays inside: {d!r}")
for y, want_dir in (("1971", "Alb (1971)"), ("1971-10-30 00:00:00", "Alb (1971)"),
                    ("0000", "Alb"), ("abcd", "Alb")):
    d = worker.destination({"artist": "A", "album": "Alb", "title": "S", "year": y}, ".flac")
    got = os.path.basename(os.path.dirname(d))
    if got != want_dir:
        bad += 1
    print(f"  {'ok  ' if got == want_dir else 'FAIL'} year={y!r} -> {got!r}")

print()
print("=== the artist directory is the lead credit ===")
# A want row carries artist_lead; a hand-typed one does not, and the credit is trimmed instead.
DIRS = [
    ({"artist": "The Longest Johns & Lucy Humphris", "artist_lead": "The Longest Johns"},
     "The Longest Johns", "stored lead wins"),
    ({"artist": "The Longest Johns feat. SKÁLD"}, "The Longest Johns", "no stored lead, trimmed"),
    ({"artist": "Simon & Garfunkel"}, "Simon & Garfunkel", "band name left whole"),
]
for extra, want_dir, label in DIRS:
    d = worker.destination({"album": "Alb", "title": "S", "year": None, **extra}, ".flac")
    got = os.path.basename(os.path.dirname(os.path.dirname(d)))
    if got != want_dir:
        bad += 1
    print(f"  {'ok  ' if got == want_dir else 'FAIL'} {label[:34]:<36} -> {got!r}")

print(f"\n{bad} failure(s)")
sys.exit(1 if bad else 0)
