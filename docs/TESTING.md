# Testing

## Automated tests

```bash
cd backend
pytest -q                                                   # SQLite, in-process queue
PG_TEST_URL=postgresql://gs@localhost:5432/gs_test pytest -q   # the same tests on PostgreSQL (wipes that database)
```

Unit tests need no API key: Gemma is replaced by `FakeGemma`, a clearly labelled scripted test double (`tests/conftest.py`). OCR tests are skipped automatically when Tesseract is not installed.

| File | Covers |
|---|---|
| `tests/test_core.py` | language detection, chunking, BM25/FAISS/RRF, agent workflow, reranker id validation, verifier, gap board, cache, API validation, scan validation, rate limits, adapters on recorded response shapes, evaluation harness |
| `tests/test_platform.py` | migrations (fresh and upgrade of a pre-platform database); registration, login, logout, cookie CSRF, invalid credentials, weak passwords, session expiry, role revocation, last-admin guard, deactivation; the full workflow (workflow A); illegal transitions and permissions; separate-reviewer rule; upload validation, duplicates, path traversal; **the 9-policy × 7-user access matrix on every surface**; search snippets and AI prompts never containing restricted text; lesson packs; entitlement grant/revoke (workflow B); membership expiry; withdrawal propagation through search, cache and reader (workflow D); policy change on a published book; ingestion failure → retry without duplicates → reprocess (workflow E); crashed-worker recovery; files without text when OCR is not allowed; real OCR of a scanned page; text uploads and chapter detection; running chapter headers; catalogue-only/external records; prompt injection; untranslated sentences; model outage; personal data deletion |

### Results (9 October 2026)

| Run | Result |
|---|---|
| SQLite | **58 passed** |
| PostgreSQL 16 (`PG_TEST_URL`) | **58 passed** |
| Frontend `tsc --noEmit` and `vite build` | pass |

## Acceptance workflows A–G (live stack)

`backend/scripts/acceptance.py` drives a **running** deployment over HTTP only (no direct database access), as an administrator who creates a librarian and readers.

```bash
GS_URL=http://localhost:8080 GS_ADMIN_EMAIL=... GS_ADMIN_PASSWORD=... python scripts/acceptance.py setup
# restart the API and the worker, then:
GS_URL=http://localhost:8080 GS_ADMIN_EMAIL=... GS_ADMIN_PASSWORD=... python scripts/acceptance.py after-restart
```

Our run: PostgreSQL 16 + Redis 7 + one API process + one separate worker process (native, not Docker), hash embedder, no LLM.

| Workflow | Checks | Result |
|---|---|---|
| Stack | API ready; persistent PostgreSQL | pass |
| A. Open-access book | register, upload, review, rights, approve, publish, processed; in catalogue; found by hybrid search with a page reference; readable; downloadable per licence | pass · **grounded explanation: SKIP** (no Gemma on the server) |
| B. Restricted book | processed; metadata visible to an outsider; outsider denied pages, in-book search, download (403); no protected text in search snippets; lesson pack refused; entitled reader can read; denied after revocation | pass |
| C. Private catalogue | detail and pages return 404 exactly like a missing id; not listed | pass |
| D. Withdrawal | detail, pages, download 404; search and cache no longer return it; audit history kept | pass |
| E. Ingestion failure | scanned PDF with OCR not permitted → `PROCESSING_FAILED` with the real error; not public; **after restarting API and worker**, retry → published; exactly one succeeded job; the failed attempt remains visible | pass |
| F. Kannada query | detected as Kannada; no restricted content | pass · **English book from a Kannada-only query: SKIP** (needs Gemma expansion or EmbeddingGemma; neither available; it was not found) · **translated explanation: SKIP** |
| G. Provider unavailable | live harvest with sources unreachable reports `added=0` with 10 errors (no fake success); catalogue and search keep working; no explanation fabricated without a model | pass |
| Restart | API back; withdrawn book still withdrawn; restricted book persists; open book still readable | pass |

Total: **36 checks, 33 passed, 0 failed, 3 skipped**.

## Browser walkthrough

A Playwright script (Chromium) signed in as the administrator, added a book through **Manage → Add resource**, uploaded a PDF, submitted, started review, verified rights, approved, published, opened it in the reader, found it in the catalogue; as an anonymous visitor searched and opened a restricted book (correct "You cannot read the full text here" state) and was refused the management portal; it also checked the admin and audit pages and the reader at 390 px width. The only console error was a blocked Google Fonts request in our sandbox.

## Could not run

- Docker images / `docker compose up` / nginx gateway (no Docker daemon).
- Any live Gemma or EmbeddingGemma call; live open-source harvesting (outbound network blocked).
- Kannada/Hindi OCR (language data not installed locally).
- Search relevance evaluation (needs the seeded library and Gemma).
