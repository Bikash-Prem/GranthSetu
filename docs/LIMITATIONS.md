# Limitations, incomplete features and manual configuration

## Manual configuration you must do

| What | Why | How |
|---|---|---|
| `POSTGRES_PASSWORD`, `REPLICATION_PASSWORD` | compose refuses to start without them | `.env` |
| First administrator | there is no default account | `make admin EMAIL=...` |
| `GEMINI_API_KEY` (or a local Gemma in Ollama) | query understanding, cross-language expansion, reranking, explanations, translation, Scan to Learn | Google AI Studio key in `.env`, or `ollama pull` a Gemma model and `LLM_PROVIDER=ollama` |
| EmbeddingGemma | real semantic and cross-language vector search (the fallback is lexical) | `ollama pull embeddinggemma` (or `make local-ai`), `EMBEDDING_PROVIDER=ollama` |
| Kannada/Hindi OCR outside Docker | Tesseract language data | `apt install tesseract-ocr-kan tesseract-ocr-hin`, `OCR_LANGS=eng+kan+hin` |
| HTTPS | secure cookies, privacy | TLS at a load balancer or the gateway; `COOKIE_SECURE=true` |
| `AUTOMATION_TOKEN` | only if n8n/cron triggers harvesting | ≥ 24 random characters |
| Internet access for the worker | harvesting open sources | allow outbound HTTPS to Wikipedia, Open Library, Gutendex, DOAJ, OpenAlex, arXiv, Europe PMC, Internet Archive, Google Books |

No external integration needs credentials from a publisher. EZproxy/OpenAthens prefixes are entered by readers in their own browser and never reach the server.

## Not implemented

- **IndicConformer** speech recognition and **IndicTrans2** translation. Voice uses the browser's Web Speech API (which may send audio to the browser vendor); translation is done by Gemma and labelled when it fails.
- **Malware scanning** of uploads (recorded as `not_configured`).
- **Object storage** (S3 / DigitalOcean Spaces) for files. Uploads need a filesystem shared by the API and the worker, so App Platform deployments cannot host uploaded books yet; use a Droplet with Docker Compose.
- **Reading lists** (bookmarks with notes exist; named lists do not), separate **authors/categories tables** (stored as text columns).
- **Password reset by email, email verification, MFA, SSO** (local accounts only).
- **EPUB / DOCX** uploads (PDF and UTF-8 text only).
- **Signed URLs** for files: files are streamed by the API after an authorisation check instead.
- **Search history**: deliberately not stored.
- Seed data for **OpenStax, NCERT, NPTEL, DIKSHA**: no public search API used.

## Verified only partly, or not at all

| Item | State |
|---|---|
| `docker compose up`, the Docker images, the nginx gateway config | compose file validated with `docker compose config`; images not built, gateway not run (no Docker daemon or nginx in our environment) |
| Gemma 4 (Gemini API or Ollama) | code paths tested with a scripted test double; **no live model call** was possible in our environment |
| EmbeddingGemma | not available; all live runs used the lexical hash embedder, so cross-language retrieval from a Kannada-only query did not work in our live run (acceptance F reports it as SKIP) |
| Live harvesting from open sources | adapters unit-tested on recorded response shapes; live calls were blocked in our environment (acceptance G shows the failure reported honestly) |
| Search relevance evaluation | harness tested; **no relevance numbers** produced (needs the seeded library and Gemma) |
| Kannada/Hindi OCR | Docker image installs the language data; only English OCR was run |
| Streaming replica failover, monitoring, alerting | not configured; the replica is for read load only |
| DigitalOcean deployment | spec written, not deployed |

## Behaviour to be aware of

- The lexical hash embedder (used when EmbeddingGemma is missing) is labelled as such and is **not** semantic.
- A resource whose licence forbids machine processing is not full-text searchable; it is discoverable only by its metadata.
- OCR pages may contain recognition errors; the reader says so on such pages.
- Explanations only ever cover the passages listed as evidence, never "the whole book".
- Search indexes are in memory per API replica; very large collections need a different vector store (e.g. pgvector or a FAISS IVF index on disk).
