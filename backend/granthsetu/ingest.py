"""Ingestion worker: consumes jobs from the queue, pulls LIVE data from open
libraries, chunks + embeds it, upserts into the primary DB, then bumps the
index version and broadcasts `index_updated` so every API replica reloads."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from .bus import get_bus
from .db import get_db
from .embeddings import get_embedder, to_bytes
from .text import chunk

log = logging.getLogger("granthsetu.ingest")


def data_dir() -> Path:
    if os.getenv("DATA_DIR"):
        return Path(os.environ["DATA_DIR"])
    here = Path(__file__).resolve()
    for cand in (here.parents[2] / "data", here.parents[1] / "data", Path("data")):
        if cand.exists():
            return cand
    return Path("data")


def ingest_records(records: list[dict]) -> list[dict]:
    """Chunk -> embed -> upsert. Returns [{id, title, lang, source}] of stored resources."""
    if not records:
        return []
    emb = get_embedder()
    db = get_db()
    stored = []
    for rec in records:
        max_chars = 700
        chunks = chunk(rec["text"], max_chars=max_chars)[:8]
        if not chunks:
            continue
        vecs = emb.embed([f'{rec["title"]}. {c}' for c in chunks], kind="doc")
        passages = [(c, to_bytes(v), emb.name) for c, v in zip(chunks, vecs)]
        meta = {k: v for k, v in rec.items() if k != "text"}
        rid = db.upsert_resource(meta, passages)
        stored.append({"id": rid, "title": rec["title"], "lang": rec["lang"], "source": rec["source"], "url": rec["url"]})
    return stored


def _commit(job_id: str, stored: list[dict], extra: dict | None = None) -> int:
    version = get_db().bump_index_version()
    get_bus().publish({"type": "index_updated", "version": version, "added": len(stored),
                       "resources": stored[:20], "job_id": job_id, **(extra or {})})
    return version


def _collect(topic: str, subject: str | None, sources: list[str], errors: list[str]) -> list[dict]:
    from . import sources as S

    recs: list[dict] = []
    if "wikipedia" in sources:
        try:
            recs += S.wikipedia_topic(topic, subject=subject)
        except Exception as exc:
            errors.append(f"wikipedia: {exc}")
    for name in sources:
        fn = S.ADAPTERS.get(name)
        if not fn:
            continue
        try:
            recs += fn(topic, subject=subject)
        except Exception as exc:
            errors.append(f"{name}: {str(exc)[:120]}")
    return recs


# ---------------------------------------------------------------------------
def handle(job: dict) -> None:
    bus = get_bus()
    kind, p, jid = job["kind"], job.get("payload", {}), job["id"]
    bus.set_job(jid, status="running", started=time.time())
    bus.publish({"type": "job", "job_id": jid, "kind": kind, "status": "running"})
    errors: list[str] = []
    stored: list[dict] = []

    if kind in ("seed", "refresh"):
        topics = json.loads((data_dir() / "seed_topics.json").read_text(encoding="utf-8"))["topics"]
        default_sources = ["wikipedia", "wikibooks", "openlibrary", "gutenberg", "doaj", "openalex"]
        for i, t in enumerate(topics):
            recs = _collect(t["title"], t.get("subject"), t.get("sources", default_sources), errors)
            got = ingest_records(recs)
            stored += got
            bus.set_job(jid, progress=f"{i + 1}/{len(topics)}", added=len(stored))
            bus.publish({"type": "job", "job_id": jid, "kind": kind, "status": "running",
                         "progress": f"{i + 1}/{len(topics)}", "topic": t["title"], "added": len(got)})
            if got and (i % 4 == 3 or i == len(topics) - 1):
                _commit(jid, got)  # make results searchable while seeding continues
        if not stored and errors:
            raise RuntimeError("no sources reachable: " + "; ".join(errors[:3]))
        get_db().set_meta("last_refresh", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    elif kind == "ingest_topic":
        recs = _collect(p["topic"], p.get("subject"), p.get("sources") or ["wikipedia", *__import__("granthsetu.sources", fromlist=["x"]).LIVE_SOURCES], errors)
        stored = ingest_records(recs)
        if stored:
            _commit(jid, stored, {"topic": p["topic"]})

    elif kind == "ingest_query":
        # Agent-triggered: a learner's search failed, so go fetch live material.
        from . import sources as S

        recs: list[dict] = []
        topic, lang, subject = p.get("topic") or "", p.get("lang", "en"), p.get("subject")
        seen = set()
        try:
            for title in (S.wikipedia_search(topic, "en", 2) if topic else []):
                for r in S.wikipedia_topic(title, subject=subject):
                    if r["key"] not in seen:
                        seen.add(r["key"]); recs.append(r)
            if lang != "en" and p.get("query"):
                for title in S.wikipedia_search(p["query"], lang, 1):
                    for r in S.wikipedia_page(title, lang, subject=subject):
                        if r["key"] not in seen:
                            seen.add(r["key"]); recs.append(r)
        except Exception as exc:
            errors.append(f"wikipedia: {exc}")
        if topic:
            for name in S.LIVE_SOURCES:
                try:
                    recs += S.ADAPTERS[name](topic, subject=subject)
                except Exception as exc:
                    errors.append(f"{name}: {str(exc)[:120]}")
        stored = ingest_records(recs)
        if stored:
            _commit(jid, stored, {"topic": topic, "query_job": True})
        if p.get("gap_id"):
            status = "resources_added" if stored else ("fetch_failed" if errors and not recs else "needs_contributors")
            get_db().update_gap(int(p["gap_id"]), status=status, added=len(stored))

    elif kind == "reembed":
        db = get_db()
        emb = get_embedder()
        rows = db.query("SELECT id, title, key FROM resources", primary=True)
        for r in rows:
            ps = db.query("SELECT id, lang, text FROM passages WHERE resource_id=%s ORDER BY chunk_no", (r["id"],), primary=True)
            vecs = emb.embed([f'{r["title"]}. {x["text"]}' for x in ps], kind="doc") if ps else []
            for x, v in zip(ps, vecs):
                db.execute("UPDATE passages SET embedding=%s, embed_model=%s WHERE id=%s AND lang=%s",
                           (to_bytes(v), emb.name, x["id"], x["lang"]))
        stored = [{"id": r["id"], "title": r["title"]} for r in rows]
        _commit(jid, [], {"reembedded": len(rows)})

    elif kind == "process_resource":
        from .library import process_resource

        info = process_resource(int(p["resource_id"]))
        status = "failed" if info.get("failed") else "done"
        bus.set_job(jid, status=status, result=info, finished=time.time())
        bus.publish({"type": "job", "job_id": jid, "kind": kind, "status": status, "resource_id": p["resource_id"]})
        return

    elif kind == "evaluate":
        from .evaluation import run_evaluation

        run_id = run_evaluation(job_id=jid, use_llm=p.get("use_llm", True))
        bus.set_job(jid, status="done", eval_run=run_id, finished=time.time())
        bus.publish({"type": "job", "job_id": jid, "kind": kind, "status": "done", "eval_run": run_id})
        return
    else:
        raise ValueError(f"unknown job kind {kind}")

    bus.set_job(jid, status="done", added=len(stored), errors=errors[:10], finished=time.time())
    bus.publish({"type": "job", "job_id": jid, "kind": kind, "status": "done", "added": len(stored),
                 "errors": errors[:5]})


def maintenance_loop(stop: threading.Event, every_s: float = 2.0) -> None:
    """Deliver the transactional outbox and recover stale or lost processing jobs."""
    from .library import dispatch_outbox, sweep_jobs

    last_sweep = 0.0
    while not stop.is_set():
        try:
            dispatch_outbox()
            if time.time() - last_sweep > 30:
                sweep_jobs()
                last_sweep = time.time()
        except Exception as exc:
            log.warning("maintenance loop: %s", exc)
        stop.wait(every_s)


def start_worker_thread(stop: threading.Event, name: str = "inline-worker") -> threading.Thread:
    t = threading.Thread(target=get_bus().consume, args=(name, handle, stop), daemon=True, name=name)
    t.start()
    threading.Thread(target=maintenance_loop, args=(stop,), daemon=True, name="maintenance").start()
    return t


def main() -> None:  # `python -m granthsetu.ingest` -> standalone worker service
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .config import settings

    db = get_db()
    log.info("worker %s up (db=%s, bus=%s, embedder=%s)", settings.instance_id, db.kind, get_bus().kind, get_embedder().name)
    if settings.auto_seed and not db.query("SELECT 1 FROM resources LIMIT 1", primary=True):
        if not db.get_meta("seed_enqueued", primary=True):
            db.set_meta("seed_enqueued", "1")
            get_bus().enqueue("seed", {})
            log.info("empty library: seed job enqueued")
    stop = threading.Event()
    if settings.refresh_interval_s > 0:
        def scheduler() -> None:
            while not stop.wait(settings.refresh_interval_s):
                get_bus().enqueue("refresh", {})
        threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=maintenance_loop, args=(stop,), daemon=True, name="maintenance").start()
    get_bus().consume(settings.instance_id, handle, stop)


if __name__ == "__main__":
    main()
