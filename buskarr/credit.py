"""Artist-credit reasoning — two questions that look like one.

  ``credited_to``  Is this track BY the artist asked for? Decides what a discography add queues.
                   A guest appearance is not: "Celtic Woman feat. The Longest Johns" is a Celtic
                   Woman release, and an artist add that accepts it fetches other people's songs.
  ``lead_artist``  Which single name does the file live under? "The Longest Johns feat. SKÁLD"
                   *is* a Longest Johns track and belongs in their folder, not a second one.

Conflating them is what produced the bug this module was extracted for: one credit rule was used
for both, so either guests leaked into the discography or genuine collaborations were scattered
across a folder each. The full credit still reaches the ``artist`` tag either way — nothing here
discards information, it only decides which name is the lead.
"""
import functools
import re
import unicodedata

# Words that legitimately follow a lead artist in a credit. Anything else after the artist's name
# means it is a DIFFERENT act whose name merely starts the same way — "Queen Tribute Band" was being
# accepted for "Queen", which is precisely what this exists to prevent.
CREDIT_JOINERS = {"feat", "featuring", "ft", "with", "and", "vs", "versus", "presents",
                  "meets", "duet", "x", "his", "her", "their"}

# Featuring markers, and only those. "&"/"and"/"x" are deliberately absent even though they are
# joiners above: they also occur inside real band names, so splitting on them would file half of
# "Simon & Garfunkel" under "Simon". An "&" collaboration folds correctly only when the catalogue
# tells us who the lead is — hence ``known_lead``, never a guess.
_FEAT = re.compile(r"[\s(\[]*\b(?:feat|feats|ft|featuring)\b\.?\s+", re.IGNORECASE)


def _loose(s):
    """Casefolded comparison form that survives non-Latin scripts.

    ``db.norm`` strips to ``[a-z0-9]``, so "坂本龍一" and "!!!" both normalise to the empty string —
    and an empty normalisation made ``credited_to`` reject every track by that artist. Letters are
    kept whatever script they are in.

    "&" and "+" become the word "and" rather than vanishing, because whether a separator follows the
    artist's name is exactly what distinguishes "Tom Lehrer & His Orchestra" (still him) from
    "Queen Tribute Band" (a different act).
    """
    s = unicodedata.normalize("NFKC", s or "").casefold()
    s = s.replace("&", " and ").replace("+", " and ")
    return " ".join(re.sub(r"[^\w]+", " ", s, flags=re.UNICODE).split())


@functools.lru_cache(maxsize=8192)
def spelling(name):
    """The form two directory names share when they are one artist spelled two ways.

    Case and punctuation only: "Said the Sky" and "Said The Sky", "Howlin’ Wolf" and "Howlin' Wolf",
    "AC_DC", which is how a directory has to spell "AC/DC", and "AC-DC". ``fold`` groups directories
    by it and ``db.spelled_lead`` files new wants by it, so the two agree on what is a twin.
    """
    return _loose((name or "").replace("_", " ")) or _fallback(name)


def _fallback(s):
    """Comparison form for names made entirely of punctuation, e.g. the band "!!!"."""
    return "".join((s or "").casefold().split())


def credited_to(track_artist, wanted):
    """Is this track by the wanted artist, as the LEAD credit?

    A catalogue's artist page lists contributions, not a discography, so four shapes of credit turn
    up under one name and only two of them are that artist's own release:

      "The Longest Johns"                       exact                          yes
      "The Longest Johns feat. SKÁLD"           they lead, a guest follows     yes
      "Celtic Woman feat. The Longest Johns"    they are the guest             NO
      "The Lehrer's Band"                       a tribute act                  NO

    The guest case was accepted until now, on the reasoning that such a track is "still by them in
    part". That is not what "everything by X" means: the recording is another artist's release,
    credited and filed under that artist's name. One artist add queued ten of them, nine of which
    were downloaded and each of which created an artist folder for a band nobody asked for.

    A credit that merely *begins* with the name is rejected too — only a recognised joiner may
    follow, or "Queen Tribute Band" passes for "Queen".
    """
    if not wanted:
        return True
    a, w = _loose(track_artist), _loose(wanted)
    if not a or not w:
        # Both sides are punctuation-only; compare them literally rather than rejecting outright.
        return bool(track_artist) and _fallback(track_artist) == _fallback(wanted)
    if a == w:
        return True
    if a.startswith(w + " "):
        return a[len(w) + 1:].split(" ", 1)[0] in CREDIT_JOINERS
    return False


def credited_by_id(track_ids, artist_ref):
    """Preferred check when the catalogue gives stable ids: is the artist the FIRST credit?

    Ordered position, not set membership. Membership answers "did they play on this", which is true
    of every guest spot — and because this is the *preferred* path, its permissiveness silently
    disabled the name-based check above rather than merely supplementing it.

    Returns None when the source supplied no ids, meaning "no opinion, ask ``credited_to``".
    """
    if not track_ids or not artist_ref:
        return None
    return str(track_ids[0]) == str(artist_ref)


def lead_artist(full_credit, known_lead=None):
    """The one artist name a track files under.

    ``known_lead`` is the catalogue's own answer and wins outright; it is the only way a credit
    joined by "&" can be resolved, since that separator is indistinguishable from one inside a band
    name. Failing that, the credit is trimmed at the first featuring marker, which is the only split
    that cannot damage a real name.
    """
    if known_lead:
        return known_lead
    head = _FEAT.split(full_credit or "", 1)[0].strip(" -–,([")
    # A credit that is *nothing but* a featuring marker ("Ft. Lauderdale Band") trims to empty.
    return head or (full_credit or "")


# A featuring segment inside a TITLE: "Good News (feat. Breagh Isabel)", "ECHO (feat. Gumi) [dj-Jo
# Remix]", "Song feat. X" at the end. Apple's catalogue names guests in the title where most others
# name them in the credit. A marker at the very start ("Ft. Worth Blues") is not one.
_TITLE_FEAT = re.compile(
    r"\s*[(\[]\s*(?:feat|feats|ft|featuring)\b\.?\s+([^)\]]+)[)\]]"
    r"|\s+(?:feat|feats|ft|featuring)\b\.?\s+([^(\[]+?)\s*$", re.IGNORECASE)
_GUESTS = re.compile(r"\s*(?:,|&|\+|\band\b)\s*", re.IGNORECASE)


def title_guests(title):
    """``title`` without its featuring segment, and the guests that segment named.

    "Good News (feat. Breagh Isabel)" gives ("Good News", ["Breagh Isabel"]). A title naming no
    guest comes back unchanged with an empty list.
    """
    title = title or ""
    m = _TITLE_FEAT.search(title)
    if not m:
        return title, []
    bare = (title[:m.start()] + title[m.end():]).strip()
    return bare, [g for g in _GUESTS.split(m.group(1) or m.group(2)) if g.strip()]


def credit_guests(full_credit):
    """The guests a credit names after its featuring marker: "Breagh Isabel" for "Classified feat.
    Breagh Isabel". None when no marker follows a lead ("Ft. Lauderdale Band" is a band)."""
    parts = _FEAT.split(full_credit or "", 1)
    if len(parts) < 2 or not parts[0].strip(" -–,(["):
        return []
    return [g for g in _GUESTS.split(parts[1].strip(" )]")) if g.strip()]
