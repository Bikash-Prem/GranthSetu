---
name: granthsetu-search
description: Find free, openly licensed learning resources (Wikipedia, Wikibooks, Open Library, Project Gutenberg, DOAJ) for a student's question in Kannada, Hindi or English, with source-verified explanations and per-resource licences. Use when someone asks for study material, notes, open textbooks or explanations of a school topic, especially in an Indian language.
license: MIT
compatibility: Needs network access to a running GranthSetu API (set GRANTHSETU_URL, default http://localhost:8080) and Python 3.9+.
metadata:
  project: GranthSetu
  version: "1.0"
---

# GranthSetu open-resource search

GranthSetu is an open-source agent that searches open digital libraries across languages,
reranks results with Gemma, and only shows explanation sentences that pass a deterministic
citation check. This skill lets any agent use it as a tool.

## When to use
- A learner asks "explain X", "notes on X", "where can I read about X", in English, Hindi (Devanagari or romanised) or Kannada.
- Someone needs a licensed, citable source rather than a generated answer.
- A teacher wants a short reading list or lesson pack on a school topic.

## How to use
1. Run the search script with the learner's words exactly as written (do not translate first; the agent handles cross-language search):
   `python scripts/search.py "ದ್ಯುತಿಸಂಶ್ಲೇಷಣೆ ಎಂದರೇನು?"`
2. Read the JSON it prints:
   - `confidence`: `high` means reranked and relevant; `medium` means retrieval only; `low` or `none` means nothing suitable was found.
   - `explanation`: only sentences with `"verified": true` may be repeated to the user, each with its numbered source.
   - `results`: each has `title`, `url`, `lang`, `licence` and `reason`. Always give the link and the licence.
3. If `confidence` is `low` or `none`, say so plainly. Do not invent material. GranthSetu has already added the topic to its Knowledge Gap Board and started a live fetch; suggest checking again in a minute.
4. To fetch a new topic into the library ahead of time: `python scripts/search.py --fetch "Tipu Sultan"`.

- `restricted` lists authorised or paid items (institution login, borrowing account or purchase). Offer them as links only, say what access they need, and never claim to have read them.

## Rules
- Never present an unverified sentence as fact.
- Never drop the licence or attribution of a resource you cite.
- Do not send personal information about the learner in the query.
