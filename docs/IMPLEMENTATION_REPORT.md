# Implementation report: GranthSetu digital-library platform

Date: 9 October 2026. Evidence: [evidence/acceptance-2026-10-09.txt](evidence/acceptance-2026-10-09.txt), [evidence/measure-2026-10-09.json](evidence/measure-2026-10-09.json), [evidence/screenshots/](evidence/screenshots/).

## What existed before

A public knowledge-discovery system: FastAPI API (N replicas), ingestion worker harvesting open sources, PostgreSQL primary + replica (or SQLite), Redis, BM25 + FAISS + RRF retrieval, Gemma understanding/rerank/explanation with a citation verifier, Scan to Learn, Knowledge Gap Board, lesson packs, evaluation harness, React UI. It had **no accounts, no uploads, no management portal, no workflow, no policy model, no reader**, and schema changes were made by startup DDL. Full audit: [AUDIT.md](AUDIT.md).

## What changed

- `db.py`: startup DDL replaced by versioned migrations; transactions can require a row (optimistic state changes); harvested records get explicit policies; the index loads only published resources with their access attributes; public stats count only discoverable records.
- `retrieval.py`: access mask before ranking (BM25 zeroed, FAISS `IDSelectorBatch`); readable vs discover-only candidates.
- `agent.py`: principal-aware; candidates re-checked against the database; only readable + AI-permitted full text goes to the model; principal and `policy_version` in cache keys; untranslated sentences labelled; explanation scope and evidence listed.
- `ai_tasks.py`: documents marked untrusted in every prompt; delimiter tags stripped; no silent English fallback as "translation".
- `api.py`: CORS closed by default; `no-store` for signed-in responses; lesson pack checks `ai` per resource; `/api/resources` filtered by policy; harvesting endpoints accept an administrator session or a scoped automation token that refuses example values; generic 500s.
- `ingest.py`: processing jobs, outbox delivery and crash sweeper in the worker.
- Frontend: search columns now mean "you can read" vs "catalogue only for you"; cards link to the reader; translation status shown.
- Infra: `migrate` service, private `book-files` volume, Tesseract (eng/kan/hin) in the image, required DB secrets, gateway cache bypass for sessions and 55 MB uploads on `/api/manage/`, CI runs tests on PostgreSQL too.

## What was added

| Deliverable | Where |
|---|---|
| PostgreSQL schema and migrations | `backend/granthsetu/migrations.py` (0001 baseline, 0002 platform: 20 new tables + columns) |
| Authentication | `auth.py`: scrypt, server-side sessions, cookie + bearer, CSRF header, rate limits, password change, deactivation |
| Authorization service | `policy.py`: 7 roles → permissions, 9 policies, discoverable/private catalogue, memberships with expiry, entitlements (user/group/institution) with expiry and revocation, licence flags, `decide()` + matching SQL predicate |
| Workflow, rights, uploads, entitlements, jobs, outbox | `library.py` |
| Secure file storage | `storage.py` |
| Extraction, OCR, chapters | `extract.py` |
| HTTP API for all of the above | `api_platform.py` |
| Operator CLI | `manage.py` (migrate, create-admin, grant-role, reindex, retry-failed, drain-outbox, status) |
| Frontend | sign-in/registration, My library, Digital library (filters, sort, pagination), book detail + reader, management portal (dashboard, add/edit, review queue, workflow actions, rights, policy editor, upload, entitlements, jobs, audit), users & institutions |
| Tests | `tests/test_platform.py` (31 tests), PostgreSQL mode for the whole suite, `scripts/acceptance.py` (A–G), `scripts/measure.py` |
| Docs | SETUP (commands, env vars, troubleshooting), ADMIN_GUIDE, UPLOAD_AND_APPROVAL, RIGHTS_AND_ACCESS, ARCHITECTURE, BACKUP_RECOVERY, SECURITY_REVIEW, TESTING, EVALUATION (measured section), LIMITATIONS, AUDIT |

## What was tested, and the results

| Test | Passed | Failed | Could not run |
|---|---|---|---|
| Automated suite on SQLite | 58 | 0 | 0 |
| Same suite on PostgreSQL 16 | 58 | 0 | 0 |
| Acceptance A–G + restart, over HTTP against API + separate worker + PostgreSQL + Redis | 33 | 0 | 3 (grounded explanation; English book from a Kannada-only query; translated explanation: all need Gemma or EmbeddingGemma) |
| Backup → restore → serve from the restored copy | identical counts and file hashes; book page and download served | — | `make backup/restore` (Docker) |
| Browser walkthrough (Playwright, Chromium) | full librarian flow; anonymous restricted and denied states; mobile reader | — | — |
| Frontend type check and build | pass | — | — |
| Mutation check on the access tests | the tests fail when a policy rule is broken | — | — |

During testing we found and fixed: running chapter headers creating one chapter per page; a dead extraction process making the worker wait for the full timeout; an over-strict password rule; acceptance-script mistakes (approving before uploading, a mixed-language "Kannada" query, not honouring `Retry-After`). The server behaviour was correct in each script case and was left unchanged.

## Credentials and models needed

| Item | Required for | Status |
|---|---|---|
| `POSTGRES_PASSWORD`, `REPLICATION_PASSWORD` | Docker stack | must be set; no defaults |
| First administrator | management | create with `manage create-admin`; no default account |
| `GEMINI_API_KEY` **or** a Gemma model pulled in Ollama | understanding, cross-language expansion, rerank, explanations, translation, Scan to Learn | manual |
| EmbeddingGemma (`ollama pull embeddinggemma`) | semantic and cross-language vector search | manual download |
| Tesseract language data (kan, hin) | Kannada/Hindi OCR outside Docker | manual install |
| `AUTOMATION_TOKEN` | n8n/cron harvesting only | optional |

External integrations needing authorisation: none from publishers. Gemini API usage is subject to Google's terms; harvested sources are public APIs.

## Incomplete features

IndicConformer and IndicTrans2 (not integrated); malware scanning; object storage for multi-host and App Platform uploads; reading lists; password reset, email verification, MFA, SSO; EPUB/DOCX; relevance evaluation numbers. Details: [LIMITATIONS.md](LIMITATIONS.md).

## Before production

1. Build and run the Docker images and the gateway; run `scripts/acceptance.py` against them.
2. Configure Gemma and EmbeddingGemma; seed the library; run the relevance evaluation; re-run acceptance (the 3 skipped checks must pass).
3. HTTPS, `COOKIE_SECURE=true`, CSP and HSTS headers.
4. Malware scanning; encrypted storage; off-site backups with a tested restore drill; monitoring and alerting.
5. Object storage if API and worker run on different hosts.
6. Decide retention periods for audit logs; an external security test.
