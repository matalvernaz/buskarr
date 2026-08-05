"""Candidate vetting — the gate that stops the wrong song entering the library.

Ported from soultube, where these three tests have a proven record: they correctly rejected a
kids' choir singing "Jellyfish" as an Arrogant Worms track, and correctly refused a 206-second
Tidal edit of a song whose local copy runs 255 seconds.

All three must pass. The duration test is the strongest signal and the title test the weakest, so
title similarity alone is never sufficient.
"""
import collections
import difflib
import re
import unicodedata

# Words that appear in upload titles but say nothing about which song it is.
NOISE = {"official", "audio", "video", "lyric", "lyrics", "hd", "hq", "mv", "ft", "feat",
         "featuring", "music", "visualizer", "explicit", "remaster", "remastered", "full",
         "album", "version"}
STOP = {"the", "a", "an", "of", "and", "to", "in", "on", "for"}

TITLE_COVERAGE_THRESHOLD = 0.7    # share of the WANTED title's words the candidate must contain
TITLE_PRECISION_THRESHOLD = 0.5   # applied only to short titles, where coverage proves little
SHORT_TITLE_TOKENS = 2            # at or below this, a title is too generic to trust coverage alone
TITLE_RATIO_THRESHOLD = 0.72      # difflib fallback for word-order differences
# ±15% of a four-minute song is ±36s, a window wide enough to hold a different song entirely, so an
# absolute cap applies as well. Both were needed: "A Moment Like This" (229s) satisfied a want for
# "Moment to Moment" (250s) at 8.5% off.
DURATION_TOLERANCE = 0.15
DURATION_TOLERANCE_ABS = 20.0
DURATION_TOLERANCE_TIGHT = 0.04  # ±4% when replacing a file whose exact length is known


def _fold(s):
    """``db.norm``'s folding, but script-preserving: non-Latin word characters survive.

    ``db.norm`` strips to ``[a-z0-9]``, so a CJK or Cyrillic string normalises to (almost) nothing.
    Fed through these gates that meant: ``tokens()`` came back empty, ``title_ok`` fell through to
    comparing two empty strings (ratio 1.0), and ``artist_ok`` waived the check — so a non-Latin
    want was gated on duration alone, and a duration-less one was not gated at all. Same lesson as
    ``credit._loose``, applied to matching. Accents still fold (NFKD + combining-mark strip), so
    "Beyoncé" and "Beyonce" stay the same token.
    """
    s = unicodedata.normalize("NFKD", s or "").casefold()
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("’", "'").replace("´", "'")
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", s)
    s = re.sub(r"\b(remaster(ed)?|explicit|album version|radio edit|single version)\b", " ", s)
    return " ".join(re.sub(r"[\W_]+", " ", s, flags=re.UNICODE).split())


def _squash(s):
    """Comparison of last resort for strings that fold to nothing — the band "!!!", say."""
    return "".join((s or "").casefold().split())


def tokens(s):
    """Content words as a MULTISET.

    A set loses repetition, and repetition is sometimes the entire title: "Moment to Moment" reduced
    to the single token {moment}, which then matched any candidate containing the word "moment" with
    a perfect score. Counting occurrences keeps "Moment to Moment" two tokens long.
    """
    return collections.Counter(w for w in _fold(s).split()
                               if w not in STOP and w not in NOISE)


def title_ok(wanted, candidate, candidate_artist="", wanted_artist=""):
    """True if the candidate's title plausibly IS the wanted song.

    Measured as coverage of the *wanted* title rather than overlap divided by the shorter of the
    two. Provider titles legitimately carry extra words — "Terry Kelly - A Pittance of Time
    (Official Video)" is the right song — so extra words in the candidate must not count against it.
    The old symmetric ratio also let a one-word wanted title match anything.

    Artist words are removed from the candidate first: their presence there is a naming convention,
    not evidence about which song this is, and leaving them in distorts the short-title check.
    """
    wt = tokens(wanted)
    if not wt:
        # Every word was a stopword or noise word ("The", "Of Us"). Returning True here skipped the
        # title gate entirely, so any same-artist candidate of roughly the right length was accepted.
        # Compare the raw strings instead of waiving the check.
        fw, fc = _fold(wanted), _fold(candidate)
        if not fw and not fc:
            # Both fold to nothing (punctuation-only titles). Two empty strings ratio to 1.0,
            # which would accept "???" for "!!!" — compare the raw characters instead.
            return _squash(wanted) == _squash(candidate)
        return difflib.SequenceMatcher(None, fw, fc).ratio() >= TITLE_RATIO_THRESHOLD
    ct = tokens(candidate) - tokens(wanted_artist) - tokens(candidate_artist)
    if not ct:
        ct = tokens(candidate)
    if not ct:
        return False
    shared = sum((wt & ct).values())
    coverage = shared / sum(wt.values())
    if coverage < TITLE_COVERAGE_THRESHOLD:
        return difflib.SequenceMatcher(None, _fold(wanted), _fold(candidate)).ratio() \
            >= TITLE_RATIO_THRESHOLD
    # A short title is generic enough that containing it proves little — "Mercy" is inside
    # "Mercy Mercy Me". Require the candidate not to be mostly other words as well.
    if len(wt) <= SHORT_TITLE_TOKENS:
        precision = shared / sum(ct.values())
        if precision < TITLE_PRECISION_THRESHOLD:
            return difflib.SequenceMatcher(None, _fold(wanted), _fold(candidate)).ratio() \
                >= TITLE_RATIO_THRESHOLD
    return True


def artist_ok(wanted_artist, candidate_title, candidate_artist=""):
    """True if the candidate is by the right artist.

    Lenient on purpose: channel and credit strings vary wildly ('ParryGripp',
    'alyankovicVEVO', 'Kaylee Rose - Topic'), so this only rejects when the artist is absent
    altogether — which is what catches a same-title song by somebody else.
    """
    at = tokens(wanted_artist)
    if not at:
        return True
    # Folded the same way the tokens are, THEN squashed for the substring test. The old
    # ``[^a-z0-9]`` strip deleted every non-ASCII letter from the haystack, so an accented or
    # non-Latin credit could never contain its own artist's name — "Beyoncé" in the candidate
    # became "beyonc" and failed to contain the token "beyonce". Parentheticals are deliberately
    # NOT stripped here (unlike _fold): "(feat. X)" is real artist evidence.
    hay = unicodedata.normalize("NFKD", f"{candidate_title} {candidate_artist}").casefold()
    hay = "".join(c for c in hay if not unicodedata.combining(c))
    hay = re.sub(r"[\W_]+", "", hay, flags=re.UNICODE)
    distinctive = [w for w in at if len(w) >= 4] or list(at)
    return any(w in hay for w in distinctive)


def duration_ok(wanted_secs, candidate_secs, tight=False):
    """True if lengths agree. Unknown wanted length passes — the other gates still apply."""
    if not wanted_secs or not candidate_secs:
        return True
    if tight:
        return abs(candidate_secs - wanted_secs) / wanted_secs <= DURATION_TOLERANCE_TIGHT
    allowed = min(wanted_secs * DURATION_TOLERANCE, DURATION_TOLERANCE_ABS)
    return abs(candidate_secs - wanted_secs) <= allowed


def acceptable_delta(wanted_secs):
    """Length difference this project treats as still-the-same recording, in seconds.

    Shared so that "would the gate accept this?" and "does this file satisfy this want?" cannot
    disagree. They must not: a stricter reconciliation than acquisition flips a correctly-acquired
    want back to pending, and it is re-fetched forever.
    """
    if not wanted_secs:
        return None
    return min(wanted_secs * DURATION_TOLERANCE, DURATION_TOLERANCE_ABS)


def vet(want, candidate, tight=False):
    """Return (ok, reason). Reason names the failing gate, for the UI and the event log."""
    if not duration_ok(want.get("duration"), candidate.get("duration"), tight):
        return False, (f"duration {candidate.get('duration')}s vs wanted "
                       f"{want.get('duration')}s")
    if not title_ok(want["title"], candidate.get("title") or "",
                    candidate.get("artist") or "", want.get("artist") or ""):
        return False, f"title mismatch: {(candidate.get('title') or '')[:60]!r}"
    if not artist_ok(want["artist"], candidate.get("title") or "",
                     candidate.get("artist") or ""):
        return False, f"wrong artist: {(candidate.get('artist') or '')[:40]!r}"
    return True, "ok"


# Quality ranking used to decide whether a candidate beats what's already held. Deliberately
# ranks by codec class first and bitrate second: AAC-256 and MP3-320 are perceptually a wash, so
# swapping one for the other is churn, not an upgrade.
def quality_score(codec, bitrate, sample_rate=None, bit_depth=None):
    codec = (codec or "").lower()
    if codec in {"flac", "alac", "wav", "pcm_s16le", "pcm_s24le"}:
        base = 1000
        if (bit_depth or 16) > 16 or (sample_rate or 44100) > 48000:
            base += 100          # hi-res masters outrank CD-quality lossless
        return base
    return min(int((bitrate or 0) / 1000), 400)


def is_upgrade(existing, candidate, min_gain=48):
    """True only when the candidate is meaningfully better, not merely different.

    ``min_gain`` exists because replacing a 256k AAC with a 320k MP3 gains nothing audible while
    costing disk and provenance — exactly the churn that happened when Lidarr's own ranking was
    trusted blindly.
    """
    have = quality_score(existing.get("codec"), existing.get("bitrate"),
                         existing.get("sample_rate"), existing.get("bit_depth"))
    new = quality_score(candidate.get("codec"), candidate.get("bitrate"),
                        candidate.get("sample_rate"), candidate.get("bit_depth"))
    return new - have >= min_gain
