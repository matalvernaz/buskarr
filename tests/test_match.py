"""Regression cases for the candidate gate.

Every case here is a real accept/reject decision taken from acquisition logs, including two the gate
originally got wrong. A wrong file entering the library is the most expensive failure this project
has, and it is silent — the file is named and tagged from the *want*, so a mismatched download looks
correct in the library and only an audio-duration probe reveals it. Hence a fixed test set rather
than spot checks.
"""
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from buskarr import match  # noqa: E402

Case = collections.namedtuple(
    "Case", "want_title want_dur want_artist cand_title cand_dur cand_artist accept label")

CASES = [
    # --- must accept: providers add words, punctuation and credits of their own ---------------
    Case("A Pittance of Time", None, "Terry Kelly",
         "Terry Kelly - A Pittance of Time     (Official Ver", 308.0, "Terry Kelly", True,
         "youtube title carries the artist plus junk"),
    Case("Heart Set On You", 283.266, "Terry Kelly", "Heart Set On You - Terry Kelly", 284.0,
         "Terry Kelly", True, "youtube artist suffix"),
    Case("Fish Licence", None, "Monty Python", "Fish Licence", 120.0, "Monty Python", True,
         "exact match, no duration known"),
    Case("Fade Away", 293.0, "Kaylee Rose", "Fade Away", 293.0, "Kaylee Rose", True,
         "exact match with duration"),
    Case("Pick Her Up At 8", None, "Kaylee Rose", "Kaylee Rose - Pick Her Up At 8", 200.0,
         "Kaylee Rose - Topic", True, "auto-generated topic channel"),
    Case("Eric The Half A Bee", None, "Monty Python", "Eric the Half-a-Bee", 90.0, "Monty Python",
         True, "punctuation and hyphenation differ"),

    # --- must reject ---------------------------------------------------------------------------
    Case("Moment to Moment", 250.2, "Terry Kelly", "A Moment Like This", 229.0, "Terry Kelly",
         False, "different song: set-collapsed title matched on one shared word"),
    Case("Mercy", 200.0, "Terry Kelly", "Mercy Mercy Me", 205.0, "Terry Kelly", False,
         "short wanted title sits inside a longer different one"),
    Case("Beachy Song", 235.0, "Kaylee Rose", "California Gurls", 200.0, "Katy Perry", False,
         "unrelated title"),
    Case("Beachy Song", 235.0, "Kaylee Rose", "Beachy Song", 166.0, "Kaylee Rose", False,
         "right title, wrong length"),
    Case("Stupid Girl", 149.0, "Kaylee Rose", "STUPID (feat. Yung Baby Tate)", 150.0, "Ashnikko",
         False, "partial title by a different artist"),
    Case("Jellyfish", None, "Arrogant Worms", "Jellyfish", 100.0, "Kids Choir Ensemble", False,
         "same title, wrong artist"),
    Case("Round and Round", 240.0, "Some Artist", "Round", 238.0, "Some Artist", False,
         "repetition is part of the title"),

    # --- non-Latin scripts. db.norm strips to [a-z0-9], so these titles used to normalise to
    # nothing: the title gate compared two empty strings (ratio 1.0) and the artist gate waived
    # itself, leaving a CJK want gated on duration alone — or on nothing, without a duration. ------
    Case("戦場のメリークリスマス", None, "坂本龍一", "花", 275.0, "宇多田ヒカル", False,
         "different CJK song — the collapsed gate accepted this"),
    Case("戦場のメリークリスマス", 273.0, "坂本龍一", "戦場のメリークリスマス", 274.0,
         "坂本龍一", True, "CJK exact match"),
    Case("Молитва", 210.0, "Би-2", "Молитва", 211.0, "Би-2", True, "Cyrillic exact match"),
    Case("Молитва", 210.0, "Би-2", "Серебро", 211.0, "Би-2", False, "different Cyrillic song"),
    Case("Halo", 261.0, "Beyoncé", "Beyoncé - Halo", 262.0, "BeyoncéVEVO", True,
         "accented artist is the only haystack evidence — the ASCII strip deleted it"),
    Case("!!!", 200.0, "!!!", "???", 201.0, "!!!", False,
         "punctuation-only titles are not all the same song"),
]


def run():
    failures = []
    for c in CASES:
        ok, why = match.vet({"title": c.want_title, "duration": c.want_dur,
                             "artist": c.want_artist},
                            {"title": c.cand_title, "duration": c.cand_dur,
                             "artist": c.cand_artist})
        if ok != c.accept:
            failures.append((c, ok, why))
        print(f"  {'ok  ' if ok == c.accept else 'FAIL'} "
              f"{'accept' if c.accept else 'reject'}  {c.label}")
    print(f"\n{len(CASES) - len(failures)}/{len(CASES)} correct")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
