# Pre-change audit (before the digital-library release)

What the repository contained when the platform specification arrived, and how each area was handled.

| Area | Before | Gap | Done |
|---|---|---|---|
| Database | PostgreSQL primary + replica (or SQLite); tables `resources`, `passages` (partitioned by language), `gaps`, `eval_runs`, `meta`; created by idempotent startup DDL | no migrations, no users, rights, workflow, files, jobs or audit | versioned migrations (`0001` baseline = the old schema, `0002` platform); old databases upgrade in place (tested) |
| Accounts | none | everything | local accounts, roles, sessions, CSRF, admin CLI |
| Authorization | a three-value `access` column (`open` / `authorised` / `paid`) used to split search results; `/api/admin/*` behind one shared token | no policy model, no per-user decisions | `policy.py`: nine policies, catalogue setting, entitlements, memberships, licence flags; old `access` values mapped to `OPEN_ACCESS` / `EXTERNAL_PROVIDER_ACCESS` |
| Library management | none (only a "fetch topic" box) | portal, workflow, uploads | management portal, state machine, uploads, rights review, entitlements, jobs, audit |
| Reader | none: results linked out | reader | detail page, chapters, pages, in-book search, progress, bookmarks, text size, downloads by policy |
| Ingestion | worker harvesting open APIs; chunk + embed + upsert; Redis Streams with retries and dead-letter | uploads, OCR, idempotent jobs, failure visibility | processing jobs with one-active-job constraint, outbox, sweeper, isolated extraction, OCR |
| Search | BM25 + FAISS + RRF, Gemma rerank, two columns by `access` | access-aware retrieval | access mask before ranking (FAISS id selector), per-candidate DB re-check, principal-aware cache keys |
| AI | Gemma understand / rerank / explain / page reading; citation verifier; lesson packs only from `open` | prompt-injection handling, per-user AI eligibility, translation honesty | untrusted-document prompts, delimiter stripping, `ai` decision per resource, untranslated sentences labelled |
| Multilingual / voice | script-based language detection; Gemma translation; Web Speech API | IndicConformer, IndicTrans2 | **not integrated** (documented) |
| Existing features | Scan to Learn, Gap Board, lesson pack, live library, evaluation, system status | — | all kept; live library and system stats now count only publicly discoverable records |
| Security issues found | `CORS allow_origins=["*"]`; `.env.example` shipped `ADMIN_TOKEN=change-me` and compose had default DB passwords; nginx micro-cache would have cached per-user responses; the lesson pack trusted a client-chosen list | — | CORS closed by default; example tokens refused and DB passwords required; cache bypass for sessions; lesson pack checks each id |
| Tests | 27 unit tests (SQLite) | integration on PostgreSQL, access, workflow, ingestion | 58 tests on SQLite and PostgreSQL; HTTP acceptance script for workflows A–G |
