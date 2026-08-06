"""Regression cases for deducing missing track positions.

What must hold: only FORCED pairings are assigned, both directions; a want with two possible
positions gets none; nothing already claimed in the directory is reused; and elimination iterates,
because committing one pair can force the next.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from buskarr import renumber  # noqa: E402

bad = 0


def check(label, ok, detail=""):
    global bad
    if not ok:
        bad += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{('  — ' + detail) if detail else ''}")


print("=== single gap: the only file left takes the only slot left ===")
got = renumber.deduce([(1, "Song", 200.0)], [(7, "Song", 200.0)])
check("one want, one free position", got == {1: 7}, str(got))

print("\n=== durations separate several gaps at once ===")
got = renumber.deduce(
    [(1, "A", 100.0), (2, "B", 200.0), (3, "C", 300.0)],
    [(4, "listing A", 100.0), (5, "listing B", 200.0), (6, "listing C", 300.0)])
check("each want takes the position its length matches", got == {1: 4, 2: 5, 3: 6}, str(got))

print("\n=== ambiguity assigns nothing ===")
# Two files of the same length competing for two slots of that length: the SET is known but not
# which file is which, so neither is written. Inventing the order would put a meaningless number
# on a file, indistinguishable from a real position.
got = renumber.deduce([(1, "A", 100.0), (2, "B", 100.0)],
                      [(4, "x", 100.0), (5, "y", 100.0)])
check("two identical-length wants over two slots -> nothing", got == {}, str(got))
got = renumber.deduce([(1, "A", 100.0)], [(4, "x", 100.0), (5, "y", 100.5)])
check("one want that fits two slots -> nothing", got == {}, str(got))

print("\n=== titles outrank equal durations ===")
# Both wants are 100s and both slots are 100s, so duration alone decides nothing. The listing
# names them, and that is enough: each want matches one slot's title.
got = renumber.deduce([(1, "Alpha", 100.0), (2, "Beta", 100.0)],
                      [(4, "Beta", 100.0), (5, "Alpha", 100.0)])
check("equal-length wants separated by title", got == {1: 5, 2: 4}, str(got))

print("\n=== elimination iterates ===")
# Want 1 is named on the listing so it is forced to slot 4 immediately. Want 2's title is absent,
# leaving it duration-compatible with both slots — until slot 4 goes, which forces slot 5.
got = renumber.deduce([(1, "Alpha", 100.0), (2, "Unlisted", 100.0)],
                      [(4, "Alpha", 100.0), (5, "Beta", 100.0)])
check("the named pair goes first, then the leftover is forced", got == {1: 4, 2: 5}, str(got))
got = renumber.deduce([(1, "A", 100.0), (2, "B", 500.0)],
                      [(4, "x", 100.0), (5, "y", 500.0), (6, "z", 900.0)])
check("both forced pairs found, spare slot ignored", got == {1: 4, 2: 5}, str(got))

print("\n=== nothing to work with ===")
check("no free positions -> nothing", renumber.deduce([(1, "A", 100.0)], []) == {})
check("no unnumbered wants -> nothing", renumber.deduce([], [(4, "x", 100.0)]) == {})
check("more wants than slots leaves the extras alone",
      renumber.deduce([(1, "A", 100.0), (2, "B", 100.0)], [(4, "x", 100.0)]) == {})
check("a want with no duration is not paired blindly",
      renumber.deduce([(1, "A", None)], [(4, "x", 100.0), (5, "y", 200.0)]) == {})

print(f"\n{bad} failure(s)")
sys.exit(1 if bad else 0)
