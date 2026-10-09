# 3-minute demo script

**Before judging:** `make smoke` passes · library seeded (Live library shows hundreds of resources) · one evaluation run saved ·
the phone on the same wifi with the app open · a printed Kannada or Hindi textbook page on the table.

| Time | Show | Say |
|---|---|---|
| 0:00 | Search page | "Kavya in Mysuru asks in Kannada. Library search is English keywords, so she finds nothing." |
| 0:20 | Type `ದ್ಯುತಿಸಂಶ್ಲೇಷಣೆ ಎಂದರೇನು?` → Search | "Gemma understands it and expands it into three languages. BM25 and EmbeddingGemma search; Gemma reranks with a reason." |
| 0:45 | Explanation card, then open the struck-out sentence | "Every sentence carries an exact quote. Our own code checks it. This one failed, so it's not shown as fact." |
| 1:05 | A result with **cross-language match**, licence line | "An English source, explained in Kannada. Licence and attribution on every card." |
| 1:10 | Point at the second column | "Papers and books behind a login or a price are listed too, in their own column. With a college login they open at the publisher; we never see credentials." |
| 1:20 | Phone: *Scan to learn* → photo of the page → Find resources | "Gemma 4 reads the page, Kannada script included, and turns it into a search she can correct." |
| 1:50 | Search something missing (e.g. a niche local topic) | "Nothing good? It says so and doesn't invent anything. It logs the gap and fetches from open libraries live…" → wait for the toast → results appear |
| 2:20 | *System* → *Send 12 requests* | "Two API replicas behind a load balancer, Postgres primary plus read replica, partitions by language, Redis cache, queue and rate limits." |
| 2:40 | *Evaluation* table | "Keyword vs meaning vs hybrid vs hybrid with Gemma rerank, measured live on 30 labelled queries." |
| 2:55 | GitHub repo | "MIT licence, Gemma open weights, an agent skill in the open standard, and a Gap Board that turns failures into contribution tasks." |

**If the wifi dies:** `python -m granthsetu.snapshot import ../data/snapshot.jsonl.gz` and switch to offline mode (`LLM_PROVIDER=ollama`).
