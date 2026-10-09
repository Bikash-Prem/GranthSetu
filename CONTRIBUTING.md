# Contributing to GranthSetu

Thank you for helping learners find open knowledge in their own language.

## Quick start
```bash
make install   # backend venv + frontend deps
make dev       # lite mode: API on :8000, web on :5173 (no Docker needed)
make test      # backend tests
```

## Good first contributions
- **Close a knowledge gap:** pick a topic on the Gap Board (or an issue labelled `knowledge-gap`), find openly licensed material, and add the topic to `data/seed_topics.json`, or write the article on Wikipedia in that language.
- **Add evaluation queries:** real questions from students in `data/eval_queries.json` (label them with the English Wikipedia title of the relevant topic).
- **Add a source adapter:** implement a function in `backend/granthsetu/sources.py` that returns records with a **per-record licence** taken from the source, then register it in `ADAPTERS`.
- **Add a paid or login-only source:** return records with `"access": "authorised"` or `"paid"` and only the abstract/description the API provides; link to the publisher. No scraping behind logins.
- **Add a language:** extend `LANG_NAMES`, script detection and stop-words in `backend/granthsetu/text.py`, and the partitions in `db.py`.

## Rules
- Never add hand-written "resources". Everything shown to learners must come from a real source with its licence.
- Keep test fixtures in `backend/tests/fixtures/` and clearly labelled.
- No secrets in commits. No personal data in logs.
- Run `make test` and `cd frontend && npm run build` before opening a PR.

Be kind. We follow the [MLH Code of Conduct](https://github.com/MLH/mlh-policies/blob/main/code-of-conduct.md).
