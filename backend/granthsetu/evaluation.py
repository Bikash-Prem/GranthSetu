"""Retrieval ablation on labelled queries: keyword vs semantic vs hybrid vs agent (hybrid + Gemma rerank).

Labels are canonical English Wikipedia topic titles (`relevant_topics`). Every
Wikipedia record ingested in hi/kn carries the English title as `topic_key`
(via langlinks), so a Kannada page about photosynthesis counts as relevant.
Numbers are computed from real runs only; nothing is precomputed or invented.
"""
from __future__ import annotations

import json
import statistics
import time

from . import agent
from .bus import get_bus
from .db import get_db
from .embeddings import get_embedder
from .ingest import data_dir
from .llm import get_llm
from .retrieval import get_index


def _relevant(result: dict, topics: set[str]) -> bool:
    tk = (result.get("topic_key") or "").lower()
    return tk in topics or (result.get("title") or "").lower() in topics


def load_queries() -> list[dict]:
    return json.loads((data_dir() / "eval_queries.json").read_text(encoding="utf-8"))["queries"]


def run_evaluation(job_id: str | None = None, use_llm: bool = True, modes: list[str] | None = None) -> int:
    queries = load_queries()
    llm_ok = use_llm and get_llm().available
    modes = modes or (["keyword", "semantic", "hybrid", "agent"] if llm_ok else ["keyword", "semantic", "hybrid"])
    index = get_index()
    index.ensure_fresh()
    indexed_topics = {(p.get("topic_key") or "").lower() for p in index.passages}
    bus = get_bus()
    details, per_mode = [], {m: [] for m in modes}
    total = len(queries) * len(modes)
    done = 0
    for q in queries:
        topics = {t.lower() for t in q["relevant_topics"]}
        answerable = bool(topics & indexed_topics)
        row = {"id": q["id"], "query": q["query"], "lang": q["lang"], "relevant_topics": q["relevant_topics"],
               "answerable": answerable, "modes": {}}
        for m in modes:
            t0 = time.time()
            try:
                out = agent.run(q["query"], mode=m, explain=(m == "agent"), use_cache=False, record_gaps=False,
                                use_llm=(m == "agent"))
                err = None
            except Exception as exc:  # report, never hide
                out, err = {"results": [], "explanation": {}}, str(exc)[:200]
            ms = int((time.time() - t0) * 1000)
            top = out["results"][:5]
            ranks = [i + 1 for i, r in enumerate(top) if _relevant(r, topics)]
            cross = any(_relevant(r, topics) and r["lang"] != q["lang"] for r in top)
            pr = (out.get("explanation") or {}).get("pass_rate")
            rec = {"hit": bool(ranks), "rr": 1 / ranks[0] if ranks else 0.0, "p5": len(ranks) / 5,
                   "cross_lingual_hit": cross, "ms": ms, "pass_rate": pr, "error": err,
                   "top": [f'{r["lang"]}:{r["title"]}' for r in top]}
            row["modes"][m] = rec
            if answerable:
                per_mode[m].append(rec)
            done += 1
            if job_id and done % 5 == 0:
                bus.set_job(job_id, progress=f"{done}/{total}")
                bus.publish({"type": "job", "job_id": job_id, "kind": "evaluate", "status": "running",
                             "progress": f"{done}/{total}"})
        details.append(row)

    summary = {}
    for m, recs in per_mode.items():
        if not recs:
            summary[m] = {"n": 0}
            continue
        lat = sorted(r["ms"] for r in recs)
        prs = [r["pass_rate"] for r in recs if r["pass_rate"] is not None]
        non_en = [r for r, d in zip(recs, [d for d in details if d["answerable"]]) if d["lang"] != "en"]
        summary[m] = {
            "n": len(recs),
            "hit_at_5": round(sum(r["hit"] for r in recs) / len(recs), 3),
            "mrr_at_5": round(sum(r["rr"] for r in recs) / len(recs), 3),
            "precision_at_5": round(sum(r["p5"] for r in recs) / len(recs), 3),
            "cross_lingual_hit_at_5": round(sum(r["cross_lingual_hit"] for r in non_en) / len(non_en), 3) if non_en else None,
            "latency_p50_ms": int(statistics.median(lat)),
            "latency_p95_ms": int(lat[min(len(lat) - 1, int(0.95 * len(lat)))]),
            "verifier_pass_rate": round(sum(prs) / len(prs), 3) if prs else None,
            "errors": sum(1 for r in recs if r["error"]),
        }
    config = {
        "queries": len(queries),
        "answerable": sum(d["answerable"] for d in details),
        "modes": modes,
        "embedder": get_embedder().name,
        "embedder_semantic": get_embedder().semantic,
        "llm": get_llm().info() if llm_ok else None,
        "index_version": index.version,
        "passages": len(index.passages),
        "note": "Queries whose labelled topic is not in the index are excluded from the metrics and listed as unanswerable.",
    }
    return get_db().save_eval(config, summary, details)
