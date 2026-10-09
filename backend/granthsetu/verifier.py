"""Deterministic citation verifier. Our code, not the model, decides what is shown.

A generated sentence is kept only if ALL of these hold:
  1. quote check   - the model's quoted phrase occurs verbatim in the cited passage
  2. support check - lexical coverage of the sentence by the passage >= VERIFY_MIN_LEXICAL
                     OR embedding similarity >= VERIFY_MIN_SEMANTIC (semantic embedder only)
  3. number check  - every number in the sentence appears in the passage
The sentence that is checked is `source_text` (same language as the passage);
the learner sees its translation `display_text` only if the check passes.
"""
from __future__ import annotations

import re

import numpy as np

from .config import settings
from .embeddings import get_embedder
from .text import normalise, numbers, tokens


def _squash(s: str) -> str:
    s = normalise(s)
    s = re.sub(r"[\"'“”‘’`]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def quote_found(quote: str, passage: str) -> bool:
    q = _squash(quote).strip(" .,;:")
    return len(q) >= 12 and q in _squash(passage)


def lexical_support(sentence: str, passage: str) -> float:
    st = set(tokens(sentence))
    if not st:
        return 0.0
    pt = set(tokens(passage))
    # Count light-stemmed matches for inflected Indic words.
    hit = 0
    for t in st:
        if t in pt or (len(t) >= 6 and any(p.startswith(t[: len(t) - 3]) for p in pt)):
            hit += 1
    return hit / len(st)


def verify_sentence(sent: dict, passage_text: str) -> dict:
    src = sent.get("source_text", "")
    q_ok = quote_found(sent.get("quote", ""), passage_text)
    lex = lexical_support(src, passage_text)
    emb = get_embedder()
    sem = None
    if emb.semantic:
        v = emb.embed([src, passage_text], kind="doc")
        sem = float(np.dot(v[0], v[1]))
    missing_nums = sorted(numbers(src) - numbers(passage_text))
    support_ok = lex >= settings.verify_min_lexical or (sem is not None and sem >= settings.verify_min_semantic)
    passed = q_ok and support_ok and not missing_nums
    reasons = []
    if not q_ok:
        reasons.append("quoted phrase not found in the cited passage")
    if not support_ok:
        reasons.append(f"weak support (lexical {lex:.2f}" + (f", semantic {sem:.2f}" if sem is not None else "") + ")")
    if missing_nums:
        reasons.append("numbers not in passage: " + ", ".join(missing_nums))
    return {
        **sent,
        "verified": passed,
        "checks": {"quote_found": q_ok, "lexical": round(lex, 3), "semantic": None if sem is None else round(sem, 3),
                   "missing_numbers": missing_nums},
        "rejection_reason": "; ".join(reasons) or None,
    }


def verify_all(sentences: list[dict], passages_by_pid: dict[int, str]) -> tuple[list[dict], float]:
    checked = [verify_sentence(s, passages_by_pid.get(s["pid"], "")) for s in sentences]
    rate = (sum(c["verified"] for c in checked) / len(checked)) if checked else 0.0
    return checked, rate
