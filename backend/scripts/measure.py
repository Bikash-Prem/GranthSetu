"""Measure latency of the main read paths on a RUNNING server, and processing times from the database.

    GS_URL=http://localhost:8000 DATABASE_URL=postgresql://... python scripts/measure.py

Prints JSON. These are measurements of *this* deployment and corpus, not general claims.
"""
from __future__ import annotations

import json
import os
import statistics
import time
import uuid

import httpx

URL = os.getenv("GS_URL", "http://localhost:8000").rstrip("/") + "/api"
WORDS = ["photosynthesis", "glucose", "rivers", "water", "chloroplasts", "oxygen", "lakes", "evaporation", "cells", "energy",
         "sunlight", "carbon", "plants", "sea", "leaves", "respiration", "mitochondria", "rain", "soil", "minerals"]


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))], 1)


def timed(fn, n: int) -> dict:
    ms, fails = [], 0
    for i in range(n):
        t = time.perf_counter()
        try:
            r = fn(i)
            if r.status_code >= 400:
                fails += 1
        except Exception:
            fails += 1
        ms.append((time.perf_counter() - t) * 1000)
    return {"n": n, "p50_ms": pct(ms, 50), "p95_ms": pct(ms, 95), "max_ms": round(max(ms), 1), "failures": fails}


def main() -> None:
    c = httpx.Client(timeout=60)
    out: dict = {"server": c.get(URL + "/system").json() | {}}
    sysinfo = out.pop("server")
    out["environment"] = {"db": sysinfo["database"]["engine"], "bus": sysinfo["bus"]["engine"], "ai": sysinfo["ai"]["provider"],
                          "embedder": sysinfo["embedder"]["name"], "indexed_passages": sysinfo["index"]["passages"]}
    # unique queries so the result cache is not hit
    out["search_hybrid_uncached"] = timed(lambda i: c.post(URL + "/search", json={
        "query": f"{WORDS[i % 20]} {WORDS[(i * 7) % 20]} {uuid.uuid4().hex[:4]}", "mode": "hybrid"}), 60)
    out["search_hybrid_cached"] = timed(lambda i: c.post(URL + "/search", json={"query": "glucose chloroplasts", "mode": "hybrid"}), 60)
    out["catalogue_page"] = timed(lambda i: c.get(URL + "/catalogue", params={"q": WORDS[i % 20][:4], "origin": ""}), 60)
    books = c.get(URL + "/catalogue", params={"per_page": 50}).json()["items"]
    readable = [b["id"] for b in books if b["access"]["can_read"]] or [0]
    out["reader_page"] = timed(lambda i: c.get(URL + f"/books/{readable[i % len(readable)]}/pages/1"), 60)
    d = timed(lambda i: c.get(URL + "/books/999999/pages/1"), 30)
    d["note"] = "unknown id: every response must be 404; failures counts them"
    out["denied_page_404"] = d
    if os.getenv("DATABASE_URL"):
        import psycopg

        with psycopg.connect(os.environ["DATABASE_URL"]) as db:
            rows = db.execute("SELECT EXTRACT(EPOCH FROM finished_at - started_at) * 1000, result FROM processing_jobs "
                              "WHERE status='succeeded' AND finished_at IS NOT NULL").fetchall()
            ms = [float(r[0]) for r in rows]
            pages = [json.loads(r[1]).get("pages", 0) for r in rows if r[1]]
            out["book_processing"] = {"jobs": len(ms), "p50_ms": pct(ms, 50) if ms else None, "max_ms": round(max(ms), 1) if ms else None,
                                      "pages_per_book_max": max(pages) if pages else None,
                                      "note": "small test PDFs (1-2 pages); includes spawning the isolated extraction process"}
            out["jobs_by_status"] = dict(db.execute("SELECT status, COUNT(*) FROM processing_jobs GROUP BY status").fetchall())
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
