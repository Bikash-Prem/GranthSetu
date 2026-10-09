"""The ingestion pipeline for managed resources (job kind `process_resource`).

validate state and rights -> verify stored file (checksum) -> extract text
(OCR when permitted) -> chapters -> page-aware chunks -> embeddings ->
ONE transaction that replaces the resource's chapters and passages, publishes
it if that was requested and still allowed, and bumps the index version.

Because derived rows are replaced atomically, running the job twice gives the
same result as running it once, and a reader never sees a half-processed
resource: until the transaction commits, the previous content (or nothing)
is what exists.
"""
from __future__ import annotations

import hashlib
import logging

from . import audit, jobs, library
from .config import settings
from .db import get_db, now_iso
from .embeddings import get_embedder, to_bytes
from .extract import ExtractionError, extract
from .storage import StorageError, get_storage
from .text import chunk

log = logging.getLogger("granthsetu.processing")


def metadata_text(res: dict, categories: list[str]) -> str:
    """The catalogue record as one searchable passage. This is what makes catalogue-only and
    restricted resources findable without exposing any of their text."""
    parts = [res["title"]]
    for label, key in (("", "subtitle"), ("By", "author"), ("Publisher:", "publisher"), ("Subject:", "subject")):
        if res.get(key):
            parts.append(f"{label} {res[key]}".strip())
    if categories:
        parts.append("Categories: " + ", ".join(categories))
    if res.get("publication_year"):
        parts.append(f"Published {res['publication_year']}")
    if res.get("description"):
        parts.append(res["description"])
    return ". ".join(p.rstrip(".") for p in parts)[:3000]


def process_resource(job: dict) -> dict:
    db = get_db()
    rid, jid = job["resource_id"], job["id"]
    q = lambda s, p=(): db.query(s, p, primary=True)  # noqa: E731
    if rid is None:
        raise jobs.PermanentJobError("the resource no longer exists")
    jobs.heartbeat(jid, "validate")
    res = library._load(q, rid)
    if res["status"] not in ("PROCESSING", "PUBLISHED"):
        raise jobs.PermanentJobError(f"resource is {res['status']}; nothing to process")
    rights = library._rights(q, rid)
    file = library._current_file(q, rid)
    if res["status"] == "PROCESSING":
        blockers = library._blockers(q, res)
        if blockers:
            raise jobs.PermanentJobError("publication conditions are not met: " + " ".join(blockers))
    categories = [r["name"] for r in q("SELECT name FROM resource_categories WHERE resource_id = %s ORDER BY name", (rid,))]

    chapters, chunks, stats = [], [], {"pages": 0, "ocr_pages": 0, "notes": []}
    if file:
        jobs.heartbeat(jid, "extract")
        try:
            data = get_storage().read(file["storage_key"])
        except StorageError as exc:
            raise jobs.PermanentJobError(f"stored file unavailable: {exc}") from None
        if hashlib.sha256(data).hexdigest() != file["sha256"]:
            raise jobs.PermanentJobError("stored file failed its integrity check (checksum mismatch)")
        try:
            ex = extract(data, file["media_type"], allow_ocr=bool(rights and rights.get("allow_ocr")))
        except ExtractionError as exc:
            raise jobs.PermanentJobError(str(exc)) from None
        del data
        stats.update(pages=ex.page_count, ocr_pages=ex.ocr_pages, notes=ex.notes)
        chapters = ex.chapters
        jobs.heartbeat(jid, "chunk")
        for page in ex.pages:
            pos = next((i for i, c in enumerate(chapters) if c.page_start <= page.number <= c.page_end), 0)
            for text in chunk(page.text, max_chars=700):
                chunks.append((text, page.number, pos))
        if len(chunks) > settings.max_chunks_per_resource:
            raise jobs.PermanentJobError(f"the document produces {len(chunks)} passages; the limit is "
                                         f"{settings.max_chunks_per_resource} (MAX_CHUNKS_PER_RESOURCE)")
        if not chunks:
            raise jobs.PermanentJobError("no text passages could be produced from the file")

    jobs.heartbeat(jid, "embed")
    emb = get_embedder()
    index_text = bool(rights and rights.get("allow_indexing"))
    meta = metadata_text(res, categories)
    meta_vec = emb.embed([meta], kind="doc")[0]
    vectors: list = [None] * len(chunks)
    if index_text:
        for i in range(0, len(chunks), 64):
            part = chunks[i:i + 64]
            for j, v in enumerate(emb.embed([f'{res["title"]}. {t}' for t, _, _ in part], kind="doc")):
                vectors[i + j] = v
            jobs.heartbeat(jid, "embed")

    jobs.heartbeat(jid, "index")
    published = False
    with db.tx() as tx:
        cur = library._load(tx.execute, rid)
        if cur["status"] not in ("PROCESSING", "PUBLISHED"):  # withdrawn or archived while we worked: write nothing
            raise jobs.PermanentJobError(f"resource became {cur['status']} during processing; results discarded")
        now_file = library._current_file(tx.execute, rid)
        if (now_file or {}).get("id") != (file or {}).get("id"):
            raise RuntimeError("the file was replaced during processing; retrying with the new file")
        tx.execute("DELETE FROM passages WHERE resource_id = %s", (rid,))
        tx.execute("DELETE FROM resource_chapters WHERE resource_id = %s", (rid,))
        chapter_ids = []
        for pos, c in enumerate(chapters):
            chapter_ids.append(int(tx.execute(
                "INSERT INTO resource_chapters(resource_id, position, title, page_start, page_end) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (rid, pos, c.title, c.page_start, c.page_end))[0]["id"]))
        tx.execute("INSERT INTO passages(resource_id, lang, chunk_no, text, embedding, embed_model, kind) VALUES (%s,%s,0,%s,%s,%s,'metadata')",
                   (rid, cur["lang"], meta, to_bytes(meta_vec), emb.name))
        for n, ((text, page_no, pos), vec) in enumerate(zip(chunks, vectors), 1):
            tx.execute("INSERT INTO passages(resource_id, lang, chunk_no, text, embedding, embed_model, kind, page_no, chapter_id) "
                       "VALUES (%s,%s,%s,%s,%s,%s,'fulltext',%s,%s)",
                       (rid, cur["lang"], n, text, to_bytes(vec) if vec is not None else None, emb.name if vec is not None else None,
                        page_no, chapter_ids[pos] if chapter_ids else None))
        now = now_iso()
        tx.execute("UPDATE resources SET content_version = content_version + 1, updated_at = %s WHERE id = %s", (now, rid))
        detail = {"job_id": jid, "passages": len(chunks), "chapters": len(chapters), "pages": stats["pages"],
                  "ocr_pages": stats["ocr_pages"], "indexed_fulltext": index_text, "embedder": emb.name}
        if cur["status"] == "PROCESSING":
            blockers = library._blockers(tx.execute, cur)  # rights may have changed while the job ran
            if blockers:
                raise jobs.PermanentJobError("publication conditions are no longer met: " + " ".join(blockers))
            tx.execute("UPDATE resources SET status = 'PUBLISHED', published_at = %s, withdrawn_at = NULL WHERE id = %s", (now, rid))
            library._event(tx, rid, None, "published", "PROCESSING", "PUBLISHED", detail=detail)
            audit.record("workflow.published", "system", "resource", rid, detail=detail, tx=tx)
            published = True
        else:
            library._event(tx, rid, None, "reprocessed", detail=detail)
        db.bump_access_epoch(tx)
        version = db.bump_index_version(f"resource {rid} processed", tx)
    library._announce()
    log.info("resource %s processed: %d passages, %d chapters, index v%s%s", rid, len(chunks), len(chapters), version,
             ", published" if published else "")
    return {**detail, "published": published, "index_version": version, "notes": stats["notes"]}


@jobs.on_final_failure
def _mark_failed(tx, job: dict, message: str) -> None:
    """A publication job that failed for good leaves the resource visibly PROCESSING_FAILED, never PUBLISHED."""
    if job["kind"] != "process_resource" or job["resource_id"] is None:
        return
    rows = tx.execute("UPDATE resources SET status = 'PROCESSING_FAILED', updated_at = %s WHERE id = %s AND status = 'PROCESSING' "
                      "RETURNING id", (now_iso(), job["resource_id"]))
    if rows:
        library._event(tx, job["resource_id"], None, "processing_failed", "PROCESSING", "PROCESSING_FAILED", reason=message,
                       detail={"job_id": job["id"]})
        audit.record("workflow.processing_failed", "system", "resource", job["resource_id"], "failed", {"job_id": job["id"]}, tx=tx)
    else:
        library._event(tx, job["resource_id"], None, "reprocess_failed", reason=message, detail={"job_id": job["id"]})


HANDLERS = {"process_resource": process_resource}
