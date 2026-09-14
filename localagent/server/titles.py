"""Short chat titles from the first message, without waiting for the model."""
from __future__ import annotations

import re

_LEAD_INS = re.compile(
    r"^(?:(?:hey|hi|hello|ok(?:ay)?|so|um+)\b[\s,!.]*"
    r"|please\s+"
    r"|(?:can|could|would|will)\s+you\s+(?:please\s+)?"
    r"|i\s+(?:want|need|would\s+like|'d\s+like)\s+(?:you\s+)?to\s+"
    r"|i'?m\s+(?:wondering|trying\s+to\s+figure\s+out)\s+(?:if|whether|how)?\s*"
    r"|help\s+me\s+(?:to\s+)?"
    r"|let'?s\s+)",
    re.IGNORECASE)


def make_title(text: str, max_len: int = 48) -> str:
    t = " ".join(text.split())
    t = re.split(r"(?<=[.?!])\s", t, maxsplit=1)[0]          # first sentence
    previous = None
    while previous != t:                                        # strip stacked lead-ins: "Hi, can you please…"
        previous = t
        t = _LEAD_INS.sub("", t).strip()
    t = t.rstrip(".?!,;: ")
    if len(t) > max_len:
        cut = t[:max_len].rsplit(" ", 1)[0]
        t = (cut if len(cut) > max_len // 2 else t[:max_len]).rstrip(",;:- ") + "…"
    return t[:1].upper() + t[1:] if t else "New chat"
