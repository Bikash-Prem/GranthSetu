# Deploying on DigitalOcean

Two options. Check current DigitalOcean docs and pricing before you create resources.

## A. One Droplet + Docker Compose (fastest for a demo)
1. Create an Ubuntu Droplet (4 GB RAM or more if you also run Ollama) in BLR1.
2. `apt install docker.io docker-compose-v2 git`, then `git clone` your repo.
3. `cp .env.example .env`, set `POSTGRES_PASSWORD`, `REPLICATION_PASSWORD` (required), `GEMINI_API_KEY`, and `COOKIE_SECURE=true` once HTTPS is in front.
4. Optional local open-weight models: `docker compose --profile local-ai up -d ollama && docker compose exec ollama ollama pull embeddinggemma`, then set `DOCKER_OLLAMA_URL=http://ollama:11434` and `EMBEDDING_PROVIDER=ollama`.
5. `docker compose up --build -d`, then `make admin EMAIL=you@college.edu`, and open `http://<droplet-ip>:8080`.
7. Back up the database and the `book-files` volume off the Droplet ([BACKUP_RECOVERY.md](BACKUP_RECOVERY.md)).
6. For a CDN, put the Droplet behind a DigitalOcean load balancer or Cloudflare; hashed assets are already marked `immutable`.

## B. App Platform + managed databases (production shape)
`.do/app.yaml` describes:
* **web**: static site from `frontend/`, served from App Platform's CDN
* **api**: `backend/Dockerfile`, 2 instances behind App Platform's load balancer (raise `instance_count` to scale out)
* **ingest**: a worker running `python -m granthsetu.ingest`
* **db**: Managed PostgreSQL. Add a **read-only node** in the database console and put its URL in `DATABASE_READ_URL`.
* **cache**: a managed Valkey (Redis-compatible) store for cache, rate limits, queue and pub/sub

```bash
doctl apps create --spec .do/app.yaml
```
Then set `GEMINI_API_KEY` and `AUTOMATION_TOKEN` as encrypted secrets in the app settings.

**Limitation:** App Platform services and workers do not share a filesystem, and object-storage support for uploaded books is not implemented yet. On App Platform, search and harvested open resources work; uploading books does not. Use option A for the digital library.

Note: on App Platform the static site and the API share one domain through routes (`/` and `/api`), so no CORS setup is needed.
