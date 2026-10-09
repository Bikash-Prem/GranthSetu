# Setup, configuration and operations

Two ways to run GranthSetu:

| | Lite (development) | Full stack (supported) |
|---|---|---|
| Database | SQLite file | PostgreSQL 16 primary + streaming read replica |
| Queue, cache, pub/sub | in-process | Redis 7 |
| Worker | thread inside the API | separate `worker` service |
| Start | `make dev` | `docker compose up --build -d` |

PostgreSQL is the authoritative store. FAISS and BM25 are in-memory indexes rebuilt from it.

## 1. Full stack with Docker Compose

```bash
cp .env.example .env
# Required: POSTGRES_PASSWORD and REPLICATION_PASSWORD (compose refuses to start without them)
python3 -c "import secrets; print(secrets.token_urlsafe(24))"     # use for each secret
# Recommended: GEMINI_API_KEY (Gemma 4), and EmbeddingGemma via Ollama (see README)
docker compose up --build -d
make admin EMAIL=you@college.edu        # first administrator; prompts for a password (min 10 chars)
open http://localhost:8080
```

Start order (enforced by `depends_on`): Postgres primary → `migrate` (one-shot, applies versioned migrations under an advisory lock) → replica, Redis → API replicas and worker → gateway.

> We validated `docker-compose.yml` with `docker compose config`, but could not build or run the images in our development environment (no Docker daemon). The same services were run as native processes; see [TESTING.md](TESTING.md).

### Everyday commands

| Task | Command |
|---|---|
| Start | `docker compose up -d` |
| Stop (keeps data) | `docker compose down` (never add `-v` unless you mean to delete every volume) |
| Logs | `make logs` or `docker compose logs -f api worker` |
| Run migrations | `make migrate` (also runs automatically on start) |
| Status (migrations, jobs, outbox) | `make status` |
| Create the first admin | `make admin EMAIL=...` |
| Grant a role from the shell | `docker compose run --rm migrate python -m granthsetu.manage grant-role --email x@y --role librarian` |
| Inspect tables | `make psql`, then `\dt`, `SELECT status, count(*) FROM resources GROUP BY 1;` |
| Rebuild search indexes | `make reindex` (every API replica rebuilds from PostgreSQL) |
| Re-embed everything after changing the embedding model | `curl -X POST -H "X-Admin-Token: $AUTOMATION_TOKEN" localhost:8080/api/admin/reembed` |
| Retry failed processing jobs | `make retry-failed`, or *Manage → Processing jobs → Retry* |
| Back up / restore | `make backup` / `make restore DIR=backups/<stamp>` ([BACKUP_RECOVERY.md](BACKUP_RECOVERY.md)) |
| Run tests | `make test` (SQLite) · `make test-pg PG=postgresql://...` (wipes that database) |
| Acceptance workflows A–G | `make acceptance` with `GS_ADMIN_EMAIL` / `GS_ADMIN_PASSWORD` set ([TESTING.md](TESTING.md)) |
| Scale the API | `make scale N=4` |

## 2. Lite mode (no Docker)

```bash
make install
cp .env.example .env
make dev                                  # API on :8000, web on :5173 (Vite proxies /api)
cd backend && python -m granthsetu.manage create-admin --email you@college.edu
```
Uploaded files go to `STORAGE_DIR` (default `backend/data/private_files`, mode 0700). OCR needs `tesseract-ocr` installed on the machine (`apt install tesseract-ocr tesseract-ocr-kan tesseract-ocr-hin`), then set `OCR_LANGS=eng+kan+hin`.

## 3. Native processes against PostgreSQL and Redis (what we used for verification)

```bash
export DATABASE_URL=postgresql://gs@localhost:5432/granthsetu REDIS_URL=redis://localhost:6379/0 STORAGE_DIR=/srv/granthsetu/files
cd backend
python -m granthsetu.manage migrate
python -m granthsetu.manage create-admin --email you@college.edu
uvicorn granthsetu.api:app --port 8000          # API (run several on different ports behind a proxy to scale)
python -m granthsetu.ingest                      # worker: processing jobs, outbox, harvesting
cd ../frontend && npm ci && npm run build && npx vite preview --port 4173   # serves the UI and proxies /api
```

## 4. Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | empty (SQLite) | PostgreSQL primary (writes, access decisions) |
| `DATABASE_READ_URL` | = primary | read replica for analytics and index builds |
| `SQLITE_PATH` | `data/granthsetu.db` | lite mode only |
| `REDIS_URL` | empty (in-process) | cache, rate limits, job stream, pub/sub |
| `STORAGE_DIR` | `data/private_files` (`/data/files` in Docker) | private book files; never inside the web root |
| `MAX_BOOK_BYTES` | 52428800 | upload limit (the gateway allows 55 MB on `/api/manage/`) |
| `MAX_PDF_PAGES` | 2000 | larger PDFs are rejected during processing |
| `OCR_LANGS` | `eng` (`eng+kan+hin` in Docker) | Tesseract languages; the traineddata must be installed |
| `SESSION_TTL_HOURS` | 12 | absolute session lifetime |
| `COOKIE_SECURE` | false | set **true** behind HTTPS |
| `REGISTRATION_OPEN` | true | false: only administrators create accounts |
| `REQUIRE_SEPARATE_REVIEWER` | false | true: a submitter cannot review or verify rights for their own resource |
| `LIBRARIAN_CAN_VERIFY_RIGHTS` | true | false: only rights managers verify rights |
| `RATE_LOGIN_PER_MIN` | 10 | per IP and per email |
| `RATE_SEARCH_PER_MIN`, `RATE_SCAN_PER_MIN`, `RATE_INGEST_PER_MIN` | 30, 8, 4 | per client IP |
| `JOB_STALE_AFTER_S` | 900 | a `running` job older than this is treated as a crashed worker and re-queued |
| `CORS_ORIGINS` | empty | same-origin only; list origins only if the UI is served elsewhere |
| `AUTOMATION_TOKEN` (`ADMIN_TOKEN` accepted) | empty | n8n/cron harvesting token; ≥ 24 characters, example values are refused |
| `GEMINI_API_KEY`, `GEMMA_MODEL`, `GEMMA_VISION_MODEL`, `LLM_PROVIDER` | | Gemma 4 (see README) |
| `EMBEDDING_PROVIDER`, `OLLAMA_URL`, `OLLAMA_EMBED_MODEL`, `ST_EMBED_MODEL` | `auto` | EmbeddingGemma; `hash` is a labelled lexical fallback |
| `POSTGRES_PASSWORD`, `REPLICATION_PASSWORD` | **required** in compose | database secrets |
| `AUTO_SEED`, `REFRESH_INTERVAL_S` | true, 0 | harvesting of open sources |

## 5. Troubleshooting startup

| Symptom | Likely cause and fix |
|---|---|
| `required variable POSTGRES_PASSWORD is missing` | set it in `.env` |
| `migrate` exits non-zero | `docker compose logs migrate`; the primary may not be healthy yet: `docker compose up -d migrate` again |
| API healthy but uploads stay `queued` | the worker is down: `docker compose logs worker`; jobs are recovered automatically when it returns (outbox + sweeper) |
| Processing fails with "OCR is not installed" | install Tesseract (Docker image has it) or upload a PDF with a text layer |
| Processing fails with "encrypted or DRM-protected" | by design: GranthSetu never removes protection; upload an unprotected copy you may use |
| Login returns 429 | login rate limit; wait for `Retry-After` |
| Every write from the browser returns 403 "Missing CSRF header" | a proxy is stripping `X-GranthSetu-CSRF`; allow the header through |
| Search shows "AI off · retrieval only" | no Gemma provider; set `GEMINI_API_KEY` or run Gemma in Ollama |
