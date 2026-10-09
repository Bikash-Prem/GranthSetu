# System design

Each principle below points to the code or config that implements it, and to how you can see it working.

| Principle | Where it lives | See it |
|---|---|---|
| **Client–server** | React SPA (`frontend/`) talks only to the HTTP API (`backend/granthsetu/api.py`). The server holds all state. | Browser devtools → Network |
| **API gateway** | `infra/nginx/gateway.conf`: one entry point for `/api/*` and the static app. Adds request IDs, security headers, body-size limits, SSE handling and retries to another replica on 502/503. | Response header `X-Request-Id` |
| **Load balancer** | nginx `upstream api_pool { least_conn; server api:8000 resolve; }`. `resolve` re-reads Docker DNS, so `--scale api=N` is picked up without restarting nginx. | *System → Send 12 requests* shows the spread across replicas (`X-Served-By`) |
| **Microservices** | `gateway`, `api` (stateless, N replicas), `worker` (ingestion and evaluation jobs), `postgres-primary`, `postgres-replica`, `redis`, optional `ollama` and `n8n`. Each scales and fails independently. | `docker compose ps` |
| **Horizontal vs vertical scaling** | API is stateless (index rebuilt from DB, state in Postgres/Redis), so it scales **horizontally**: `make scale N=4`. Postgres scales **vertically** (bigger node) for writes and **horizontally for reads** via replicas. The worker scales horizontally through the consumer group. | `docker compose up -d --scale api=4` |
| **Caching** | Redis (`bus.py`): search results (TTL 10 min, key includes the **index version**, so new data invalidates automatically), Gemma outputs (TTL 24 h, prompt hash; never for photos), job status. nginx micro-caches public lists for 3 s. Cache errors **fail open**. | *System* shows the hit rate; repeat a search → `from cache` |
| **CDN** | Vite emits content-hashed assets; the gateway serves `/assets/*` with `Cache-Control: public, max-age=31536000, immutable` and `index.html` with `no-cache`. That is exactly what a CDN needs. On DigitalOcean App Platform the static site is served from its global CDN (`.do/app.yaml`). | `curl -I /assets/…` |
| **Database indexing** | `resources(key)` unique, `resources(lang)`, `resources(topic_key)`, `resources(fetched_at DESC)`, `passages(resource_id)`, `gaps(status, hits DESC)`. | `EXPLAIN SELECT * FROM resources WHERE lang='kn'` → `Index Scan using ix_resources_lang` |
| **Sharding / partitioning** | `passages` is `PARTITION BY LIST (lang)` with `passages_en`, `passages_hi`, `passages_kn` and a default partition. Queries for one language touch one partition; a language can move to its own node later (e.g. Citus or foreign tables) without app changes. | `SELECT tableoid::regclass, count(*) FROM passages GROUP BY 1` |
| **Replication** | Postgres streaming replication: the replica is cloned with `pg_basebackup -R` (`infra/postgres/replica-entrypoint.sh`) and runs as a hot standby. **Read/write split** in `db.py`: `query()` → replica, `execute()` → primary. | *System → Database* shows replica lag (seconds and bytes) |
| **Message queue** | Redis Streams consumer group `workers` on `gs:jobs` (`bus.py`): at-least-once delivery, `XAUTOCLAIM` re-delivers jobs from crashed workers after 120 s, and after 3 deliveries a job goes to the dead-letter stream `gs:jobs:dead`. Job types: `seed`, `refresh`, `ingest_topic`, `ingest_query`, `evaluate`, `reembed`. | *Live library* feed |
| **Pub/sub and events** | Worker publishes `index_updated` on `gs:events`; every API replica reloads its BM25/FAISS index; browsers get the same events over **SSE** (`/api/events`). | Toast "Live update: N new resources" |
| **Rate limiting** | Two layers. Edge: nginx `limit_req` per IP (20 r/s; uploads 1 r/s). App: Redis fixed-window per client and route (search 30/min, scan 8/min, ingest 4/min), **shared across replicas**, returning `429` with `Retry-After`. | Hammer `/api/search` → 429 |
| **Health checks and self-healing** | `/api/health` (liveness), `/api/ready` (DB reachable), Docker healthchecks, `depends_on: service_healthy`, `restart: unless-stopped`, retries on startup while the primary or replica boots. | `docker compose ps` |
| **Graceful degradation** | No Gemma → retrieval-only with a clear banner. No Ollama → labelled lexical embeddings. Gemini 429/5xx → backoff retry, then fallback. Malformed JSON → one stricter retry, then fallback. Sources down → job reports errors and the gap is marked `fetch_failed` and retried. | *Agent trace* shows `fallback` steps |

## CAP theorem: the choices we made

A network partition between the primary and the replica forces a choice between consistency and availability.

* **Search reads choose availability (AP).** API replicas read from the streaming replica and keep serving from their in-memory index even if the primary is unreachable. The cost is **bounded staleness**: a resource ingested a moment ago may not be searchable on every replica yet. That is fine for a library.
* **Writes choose consistency (CP).** Ingestion and Gap Board writes go only to the primary inside transactions (`upsert_resource` replaces a resource and its passages atomically). If the primary is down, writes fail loudly; they are never accepted somewhere else and merged later.
* **Read-your-writes where it matters.** The Gap Board and evaluation pages read from the primary (`primary=True`), because the user just caused the write.
* **Replica-lag-aware index reload.** `index_updated` carries the new version number. A replica that has not replayed it yet waits up to ~5 s, then rebuilds from the **primary** instead of serving a stale index (`retrieval.py: ensure_fresh`).
* **Cache and rate limits fail open.** If Redis is down, searches still work (no cache) and limits are not enforced. Availability wins for non-critical state.

## Where the limits are (and the next step)
| Today | Next step at scale |
|---|---|
| Each API replica keeps the full BM25/FAISS index in memory (fine to around 10^5 passages) | Move vectors to pgvector/Qdrant, or shard the FAISS index by language partition |
| One streaming replica | More replicas behind a read pool (PgBouncer / HAProxy) |
| Redis single node | Redis Sentinel/Cluster or DigitalOcean Managed Valkey |
| SSE holds one worker thread per open tab | Async SSE with `redis.asyncio` |
