"""Deterministic text utilities: script-based language detection, tokenisation, chunking."""
from __future__ import annotations

import re
import unicodedata

LANG_NAMES = {"en": "English", "hi": "Hindi", "kn": "Kannada"}

_WORD = re.compile(r"[\wऀ-ॿಀ-೿]+", re.UNICODE)
_SENT = re.compile(r"(?<=[.!?।॥])\s+")

STOP = {
    "en": set(
        "a an the of to in on for and or is are was were be been being by with as at from that this these those it its "
        "into than then which who whom what when where why how do does did can could should would may might will shall "
        "not no yes also such their there they them he she his her we our you your i me my about over under between "
        "more most other some any each all both one two very only own same so too just explain tell give notes".split()
    ),
    "hi": set(
        "का के की है हैं में से को पर और या यह वह ये वे एक भी तो ही था थे थी कर करना करते किया होता होती होते "
        "हो गया गई लिए साथ बारे क्या कैसे क्यों कौन जो इस उस इन उन नहीं बताइए बताओ समझाइए".split()
    ),
    "kn": set("ಮತ್ತು ಈ ಆ ಒಂದು ಇದು ಅದು ಏನು ಹೇಗೆ ಏಕೆ ಯಾವ ಬಗ್ಗೆ ಇದೆ ಅಥವಾ ಕುರಿತು ವಿವರಿಸಿ ತಿಳಿಸಿ".split()),
}
ALL_STOP = set().union(*STOP.values())


def detect_lang(text: str) -> str:
    """Script-based detection for the three MVP languages (Gemma refines this later)."""
    counts = {"kn": 0, "hi": 0, "en": 0}
    for ch in text:
        o = ord(ch)
        if 0x0C80 <= o <= 0x0CFF:
            counts["kn"] += 1
        elif 0x0900 <= o <= 0x097F:
            counts["hi"] += 1
        elif ch.isascii() and ch.isalpha():
            counts["en"] += 1
    best = max(counts, key=counts.get)
    return best if counts[best] else "en"


def normalise(text: str) -> str:
    return unicodedata.normalize("NFC", text).lower()


def tokens(text: str, drop_stop: bool = True) -> list[str]:
    toks = _WORD.findall(normalise(text))
    if drop_stop:
        toks = [t for t in toks if t not in ALL_STOP]
    return toks


def index_terms(text: str) -> list[str]:
    """BM25 terms. Kannada and Hindi are inflected (ದ್ಯುತಿಸಂಶ್ಲೇಷಣೆಯ / ದ್ಯುತಿಸಂಶ್ಲೇಷಣೆ),
    so long Indic tokens also emit a light-stemmed prefix."""
    out: list[str] = []
    for t in tokens(text):
        out.append(t)
        if not t.isascii() and len(t) >= 6:
            out.append(t[: max(4, len(t) - 3)] + "~")
        elif t.isascii() and len(t) > 4 and t.endswith("s"):
            out.append(t[:-1])
    return out


def numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:[.,]\d+)?", text))


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(text.strip()) if s.strip()]


def chunk(text: str, max_chars: int = 700, min_chars: int = 120) -> list[str]:
    """Paragraph-aware chunking so each passage is citable on its own."""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n(?==)", text) if p.strip()]
    out: list[str] = []
    buf = ""
    for p in paras:
        p = re.sub(r"^=+\s*(.*?)\s*=+$", r"\1:", p)  # wiki headings
        for s in split_sentences(p) or [p]:
            if len(buf) + len(s) + 1 > max_chars and len(buf) >= min_chars:
                out.append(buf.strip())
                buf = ""
            buf += (" " if buf else "") + s
        if len(buf) >= max_chars * 0.6:
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        if out and len(buf) < min_chars:
            out[-1] = out[-1] + " " + buf.strip()
        else:
            out.append(buf.strip())
    return out


def keyword_topic(text: str, max_words: int = 6) -> str:
    """Anonymised topic label used for the Gap Board when the LLM is unavailable."""
    return " ".join(tokens(text)[:max_words]) or "unknown"
