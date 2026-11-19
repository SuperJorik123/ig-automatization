"""Words out of a post — for grouping posts into incidents and for spikes."""
import re

_URL = re.compile(r"https?://\S+|t\.me/\S+|www\.\S+")
_HANDLE = re.compile(r"@\w+")
_WORD = re.compile(r"[^\W\d_]{3,}", re.UNICODE)

# Words that say nothing about WHICH event it is. News filler matters most:
# "breaking" in every BNO post would otherwise glue unrelated stories together.
STOPWORDS = set("""
the and for are but not you all any can had her was one our out has have his how
its may new now old see two way who did get got him let say she too use with that
this from they will would there their what when where which while been were than
then them these those into over after before about against between during under
again also just more most some such only very even back down here off once onto
upon your yours ours mine because could should being does doing done each few
other same both through until above below why whom whose itself himself herself
breaking news update updates video videos watch footage live report reports
reported reportedly says said saying according latest today tonight yesterday
people person man woman men women officials official source sources local
happening happened moment moments shows showing shown seen scene clip clips
via join follow subscribe channel telegram twitter reddit post posted
это как что так все или его она они при для был была были будет уже еще
""".split())


def clean(text: str) -> str:
    return _HANDLE.sub(" ", _URL.sub(" ", text or ""))


def tokens(text: str) -> set:
    """Lower-case content words (3+ letters, any alphabet), stopwords out."""
    return {w for w in (m.lower() for m in _WORD.findall(clean(text)))
            if w not in STOPWORDS}


def spike_terms(text: str) -> set:
    """What a spike is counted on: content words of 4+ letters. Short words
    collide across languages and topics too often to mean anything."""
    return {w for w in tokens(text) if len(w) >= 4}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def overlap(a: set, b: set) -> float:
    """Share of the SMALLER set found in the other — a short headline inside a
    long post about the same event still matches."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))
