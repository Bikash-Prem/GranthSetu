# Evaluation methodology

**Question:** does each part of the agent earn its place?

## Test set
`data/eval_queries.json` holds 30 queries: 10 Kannada, 10 Hindi and 10 English, phrased the way students ask. Each is labelled with
the **English Wikipedia title** of the relevant topic (`relevant_topics`). Every ingested Wikipedia page in Hindi or Kannada stores
the English title as `topic_key` (through langlinks), so a Kannada page about photosynthesis counts as relevant for an English
query and the other way round.

To make it stronger: replace or extend these with real questions collected from students, and add photo queries.

## Conditions (ablation)
1. **Keyword**: BM25 only, original query
2. **Meaning**: FAISS over embeddings only, original query
3. **Hybrid**: BM25 + FAISS fused with RRF (k = 60), original query
4. **Agent**: Gemma query understanding with kn/hi/en expansion → hybrid → **Gemma rerank**, plus a grounded explanation and the citation verifier

## Metrics
* **Hit@5**: share of queries with at least one relevant resource in the top 5
* **MRR@5**: mean reciprocal rank of the first relevant result
* **Precision@5**: relevant results / 5
* **Cross-language hit@5** (Hindi/Kannada queries): a relevant resource in a *different* language from the query appears in the top 5
* **Latency p50/p95** per condition
* **Verifier pass rate**: share of generated explanation sentences that pass the citation verifier (Agent only)

Queries whose labelled topic is not in the index yet are reported as *unanswerable* and excluded from the metrics, so a half-seeded
library does not distort the comparison.

## Integrity rules
* Numbers come only from runs against the live index (`/api/eval/run` → worker job → `eval_runs` table). The table stores the
  embedder, model, index version and passage count with each run.
* Caching and Gap Board recording are switched off during evaluation.
* Report whatever comes out, including when reranking helps less than expected.
* The *hash* embedder is a lexical fallback; when it is active, *Meaning* results reflect character overlap, not semantics. The
  page says so. Use EmbeddingGemma (Ollama) for the real comparison.

---

# Measured results (9 October 2026)

Everything below was **measured** on our development machine (native processes: PostgreSQL 16, Redis 7, one API process, one worker), not estimated. It describes this deployment and a small test corpus. Targets are not results.

## Search relevance (the ablation above): **not run yet**

It needs the seeded library (live internet to Wikipedia and the other sources) and a Gemma provider. Neither was reachable from our build environment: outbound requests to the sources were blocked and no `GEMINI_API_KEY` was available. The harness is tested (`test_evaluation_runs_on_real_query_file` runs it end to end on the test fixture), but we report **no hit@5, MRR or cross-language numbers**. Run it from the *Evaluation* page after seeding.

## Access-control correctness

| Check | Size | Result |
|---|---|---|
| Policy matrix: 9 published resources (one per policy, plus a private-catalogue variant) × 7 kinds of user (anonymous, reader, institution member, group member, entitled, expired entitlement, revoked entitlement) × 5 surfaces (detail, page, in-book search, catalogue listing, download) | 315 assertions | all pass, on SQLite and PostgreSQL |
| Search and AI leakage: the agent run as each of 7 principals, every prompt sent to the (test) model inspected for restricted text | 7 runs × 9 resources | no leak |
| Mutation check: `INSTITUTION_ONLY` deliberately broken to allow everyone | — | both access tests fail, as they should |
| Acceptance workflows A–G over HTTP against API + worker + PostgreSQL + Redis, plus a restart | 36 checks | 33 pass, 0 fail, 3 skipped (need Gemma); see [TESTING.md](TESTING.md) |

## Latency (live stack, 25 indexed passages, hash embedder, no LLM)

| Path | n | p50 | p95 | Failures |
|---|---|---|---|---|
| Hybrid search, unique queries (no cache) | 60 | 6.1 ms | 8.5 ms | 0 |
| Hybrid search, repeated query (cache) | 60 | 4.1 ms | 5.4 ms | 0 |
| Catalogue page (SQL with policy predicate) | 60 | 3.8 ms | 5.1 ms | 0 |
| Reader page | 60 | 3.6 ms | 5.1 ms | 0 |
| Unknown id (must be 404) | 30 | 2.6 ms | 4.3 ms | 30 × 404, as required |

The corpus is tiny, so these numbers say the access checks add little overhead; they say nothing about latency at 100k documents. With Gemma enabled, model calls dominate (seconds); not measured here.

## Ingestion

| Measurement | Result |
|---|---|
| Book processing job, 1–2 page PDFs (15 jobs, end to end incl. spawning the isolated extractor) | p50 577 ms, max 964 ms |
| Text extraction, 200-page text PDF (76 KB) | 1.2 s (≈160 pages/s), 10 chapters detected, 200 chunks |
| Hash embedder throughput | ≈235–310 chunks/s (EmbeddingGemma not measured: not available here) |
| OCR of one scanned page (Tesseract 5, English) | 1.4 s, text read correctly |
| Kannada/Hindi OCR | **not measured**: the `kan`/`hin` traineddata were not installed in our environment (the Docker image installs them) |

Speech recognition and translation quality were not measured (IndicConformer and IndicTrans2 are not integrated).

Reproduce: `python backend/scripts/measure.py` against a running server (lift `RATE_SEARCH_PER_MIN` for the run) and `pytest` for the access checks.
