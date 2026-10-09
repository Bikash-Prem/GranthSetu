"""HTTP API (stateless; run N replicas behind the gateway/load balancer)."""
from __future__ import annotations

import io
import json
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import agent, ai_tasks, auth
from .api_platform import router as platform_router
from .library import WorkflowError
from .policy import Principal, decide, discover_sql
from .bus import get_bus
from .config import settings
from .db import get_db
from .embeddings import get_embedder
from .llm import LLMError, get_llm
from .retrieval import get_index
from .verifier import quote_found

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("granthsetu.api")
_stop = threading.Event()
STARTED = time.time()


def _index_listener() -> None:
    """Reload the in-memory index when any worker publishes index_updated."""
    for ev in get_bus().subscribe(_stop, timeout=30):
        try:
            if ev is None:  # heartbeat: catch anything we missed
                get_index().ensure_fresh()
            elif ev.get("type") == "index_updated":
                get_index().ensure_fresh(wanted_version=int(ev["version"]))
        except Exception as exc:
            log.warning("index refresh failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = get_db()
    get_bus()
    get_embedder()
    get_llm()
    for attempt in range(20):  # the read replica may still be catching up with the schema
        try:
            get_index().ensure_fresh()
            break
        except Exception as exc:
            log.warning("index not ready yet (%s), retry %d", exc, attempt)
            time.sleep(1.5)
    threading.Thread(target=_index_listener, daemon=True, name="index-listener").start()
    inline = settings.run_worker_inline and not settings.redis_url  # lite mode: worker in-process
    if inline:
        from .ingest import start_worker_thread

        start_worker_thread(_stop)
        if settings.auto_seed and not db.query("SELECT 1 FROM resources LIMIT 1") and not db.get_meta("seed_enqueued"):
            db.set_meta("seed_enqueued", "1")
            get_bus().enqueue("seed", {})
    log.info("api %s ready: mode=%s db=%s bus=%s llm=%s embedder=%s", settings.instance_id, settings.mode, db.kind,
             get_bus().kind, get_llm().provider, get_embedder().name)
    yield
    _stop.set()


app = FastAPI(title="GranthSetu API", version="2.0.0", lifespan=lifespan)
_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
if _origins:  # same-origin by default (gateway or Vite proxy); cross-origin only for listed origins
    app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"], allow_headers=["content-type", auth.CSRF_HEADER])
app.include_router(platform_router)


@app.middleware("http")
async def headers(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:10]
    t0 = time.time()
    resp = await call_next(request)
    resp.headers["X-Served-By"] = settings.instance_id
    resp.headers["X-Request-Id"] = rid
    resp.headers["Server-Timing"] = f"app;dur={int((time.time() - t0) * 1000)}"
    if request.headers.get("authorization") or request.cookies.get(auth.COOKIE):
        # Responses for signed-in users may depend on their entitlements: never let a shared cache keep them.
        resp.headers["Cache-Control"] = "private, no-store"
        resp.headers["Vary"] = "Cookie, Authorization"
    return resp


@app.exception_handler(WorkflowError)
async def workflow_error(_: Request, exc: WorkflowError):
    return JSONResponse(status_code=exc.status, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def unexpected(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal error. It has been logged."})


def client_id(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    return (xff.split(",")[0].strip() if xff else (request.client.host if request.client else "unknown"))


def limiter(route: str, per_min_attr: str):
    def dep(request: Request) -> None:
        ok, reset = get_bus().allow(client_id(request), route, getattr(settings, per_min_attr))
        if not ok:
            raise HTTPException(429, detail=f"Too many requests. Try again in {reset}s.", headers={"Retry-After": str(reset)})
    return dep


def require_admin(request: Request, x_admin_token: str = Header(default="")) -> None:
    """Harvest triggers: a platform administrator's session, or the scoped automation token (n8n/cron).
    Weak or example tokens are refused, so the shipped .env.example value can never work."""
    import hmac

    tok = settings.admin_token
    if x_admin_token and tok and len(tok) >= 24 and tok not in ("change-me",) and hmac.compare_digest(x_admin_token, tok):
        return
    p = auth.current_principal(request)
    if p.can("user.manage"):
        return
    raise HTTPException(401, "automation token or administrator session required")


@app.exception_handler(ValueError)
async def value_error(_: Request, exc: ValueError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


# ---------------------------------------------------------------------------
@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "instance": settings.instance_id}


@app.get("/api/ready")
def ready() -> dict:
    get_db().query("SELECT 1 AS ok")
    return {"ready": True, "index_version": get_index().version}


@app.get("/api/system")
def system() -> dict:
    db, bus, emb = get_db(), get_bus(), get_embedder()
    return {
        "instance": settings.instance_id,
        "uptime_s": int(time.time() - STARTED),
        "mode": settings.mode,
        "database": {"engine": db.kind, "read_write_split": db.has_replica, "replication": db.replication_status(),
                     "partitioned_by": "lang" if db.kind == "postgres" else None,
                     "last_refresh": db.get_meta("last_refresh")},
        "bus": {"engine": bus.kind, "queue_depth": bus.queue_depth(), **bus.stats},
        "index": get_index().info(),
        "ai": get_llm().info(),
        "embedder": {"name": emb.name, "open_weight": emb.open_weight, "semantic": emb.semantic},
        "library": db.stats(),
        "limits": {"search_per_min": settings.rate_search_per_min, "scan_per_min": settings.rate_scan_per_min},
    }


class SearchIn(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    mode: str = "agent"
    explain: bool = True


@app.post("/api/search", dependencies=[Depends(limiter("search", "rate_search_per_min"))])
def search(body: SearchIn, p: Principal = Depends(auth.current_principal)) -> dict:
    return agent.run(body.query, mode=body.mode, explain=body.explain, principal=p)


ALLOWED_IMG = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


@app.post("/api/scan", dependencies=[Depends(limiter("scan", "rate_scan_per_min"))])
async def scan(file: UploadFile = File(...)) -> dict:
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(413, f"Image too large (max {settings.max_upload_bytes // 1024 // 1024} MB)")
    from PIL import Image, ImageOps

    try:
        img = Image.open(io.BytesIO(data))
        fmt = img.format
        img.verify()
    except Exception:
        raise HTTPException(415, "Not a valid image. Use JPEG, PNG or WEBP.")
    if fmt not in ALLOWED_IMG:
        raise HTTPException(415, "Unsupported image type. Use JPEG, PNG or WEBP.")
    # Re-encode: strips EXIF/GPS metadata and caps resolution before it leaves the server.
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    img.thumbnail((1600, 1600))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    del data
    llm = get_llm()
    if not llm.available:
        raise HTTPException(503, "Photo reading needs Gemma (set GEMINI_API_KEY or run a local Gemma in Ollama). Type your question instead.")
    t0 = time.time()
    try:
        out = ai_tasks.read_page(buf.getvalue(), "image/jpeg")
    except LLMError as exc:
        raise HTTPException(502, f"Could not read the photo ({exc}). You can type the text instead.")
    finally:
        buf.close()  # the photo is never written to disk or cache
    out["model"] = llm.vision_model
    out["provider"] = llm.provider
    out["ms"] = int((time.time() - t0) * 1000)
    out["privacy"] = ("Photo was sent to Google's Gemini API for reading and was not stored by GranthSetu."
                      if llm.provider == "gemini" else "Photo was processed by a local model and was not stored.")
    return out


class LessonIn(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    resource_ids: list[int] = Field(min_length=1, max_length=5)
    lang: str = "en"


@app.post("/api/lesson-pack", dependencies=[Depends(limiter("search", "rate_search_per_min"))])
def lesson_pack(body: LessonIn, p: Principal = Depends(auth.current_principal)) -> dict:
    idx = get_index()
    idx.ensure_fresh()
    meta = get_db().resources_by_ids(body.resource_ids)
    # Lesson packs reproduce passages and send them to the model: both need read AND machine-processing rights.
    denied = [r for r in body.resource_ids if not decide(p, meta.get(r), "ai").allowed]
    body.resource_ids = [r for r in body.resource_ids if r not in denied]
    if not body.resource_ids:
        raise HTTPException(403, "None of these resources may be used in a lesson pack by you.")
    passages = []
    for rid in body.resource_ids:
        for p in idx.passages_for(rid, 1):
            passages.append({"pid": int(p["id"]), "lang": p["lang"], "title": p["title"], "text": p["text"], "rid": rid})
    questions, status = [], "skipped"
    if get_llm().available and passages:
        try:
            qs = ai_tasks.practice_questions(body.lang, passages)
            by_pid = {p["pid"]: p["text"] for p in passages}
            for q in qs:
                q["verified"] = quote_found(q["quote"], by_pid.get(q["pid"], ""))
            questions = [q for q in qs if q["verified"]]
            status = "ok" if questions else "rejected"
        except LLMError:
            status = "failed"
    return {
        "query": body.query,
        "lang": body.lang,
        "resources": [{k: meta[r][k] for k in ("id", "title", "url", "source", "lang", "licence", "licence_url", "attribution")}
                      for r in body.resource_ids if r in meta],
        "passages": passages,
        "questions": questions,
        "questions_status": status,
        "excluded": len(denied),
    }


@app.get("/api/resources")
def resources(limit: int = 30, lang: str | None = None, p: Principal = Depends(auth.current_principal)) -> dict:
    """Recently added resources the caller may discover (the live-library feed)."""
    where, params = discover_sql(p)
    if lang:
        where += " AND lang=%s"
        params.append(lang)
    rows = get_db().query(f"SELECT id, title, source, kind, url, lang, licence, fetched_at, access, policy, origin FROM resources "
                          f"WHERE {where} ORDER BY fetched_at DESC, id DESC LIMIT %s", [*params, min(limit, 200)], primary=True)
    return {"resources": rows, "stats": get_db().stats()}


class TopicIn(BaseModel):
    topic: str = Field(min_length=2, max_length=120)
    subject: str | None = None


@app.post("/api/ingest/topic", dependencies=[Depends(limiter("ingest", "rate_ingest_per_min"))])
def ingest_topic(body: TopicIn) -> dict:
    return {"job_id": get_bus().enqueue("ingest_topic", {"topic": body.topic.strip(), "subject": body.subject})}


@app.get("/api/jobs/{job_id}")
def job(job_id: str) -> dict:
    j = get_bus().get_job(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    return j


@app.get("/api/gaps")
def gaps() -> dict:
    rows = get_db().list_gaps()
    groups: dict[str, int] = {}
    for g in rows:
        groups[f'{g["lang"]}/{g["subject"]}'] = groups.get(f'{g["lang"]}/{g["subject"]}', 0) + int(g["hits"])
    return {"gaps": rows, "by_lang_subject": groups}


@app.get("/api/eval/latest")
def eval_latest() -> dict:
    return {"run": get_db().latest_eval()}


class EvalIn(BaseModel):
    use_llm: bool = True


@app.post("/api/eval/run", dependencies=[Depends(limiter("ingest", "rate_ingest_per_min"))])
def eval_run(body: EvalIn) -> dict:
    return {"job_id": get_bus().enqueue("evaluate", {"use_llm": body.use_llm})}


@app.get("/api/events")
def events(request: Request) -> StreamingResponse:
    """Server-Sent Events: live ingestion progress and new resources."""
    stop = threading.Event()

    def gen():
        yield f"event: hello\ndata: {json.dumps({'instance': settings.instance_id})}\n\n"
        try:
            for ev in get_bus().subscribe(stop, timeout=15):
                yield ": keep-alive\n\n" if ev is None else f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        finally:
            stop.set()

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- admin (used by n8n / cron) ---------------------------------------------
@app.post("/api/admin/refresh", dependencies=[Depends(require_admin)])
def admin_refresh() -> dict:
    return {"job_id": get_bus().enqueue("refresh", {})}


@app.post("/api/admin/ingest", dependencies=[Depends(require_admin)])
def admin_ingest(body: TopicIn) -> dict:
    return {"job_id": get_bus().enqueue("ingest_topic", {"topic": body.topic, "subject": body.subject})}


@app.post("/api/admin/reembed", dependencies=[Depends(require_admin)])
def admin_reembed() -> dict:
    return {"job_id": get_bus().enqueue("reembed", {})}
