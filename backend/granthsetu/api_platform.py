"""Accounts, catalogue, reader, personal data, library management and administration.

Every route that touches a resource goes through policy.decide(); every route
that changes something goes through library.py, which enforces the workflow.
"""
from __future__ import annotations

import json
import math
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import Response as RawResponse
from pydantic import BaseModel, Field

from . import auth, library
from .audit import audit
from .bus import get_bus
from .config import settings
from .db import get_db
from .policy import (CATALOGUE, POLICIES, RIGHTS_BASES, ROLES, STATUSES, Principal, access_label, as_dt, decide,
                     discover_sql, utcnow)

router = APIRouter(prefix="/api")
NO_STORE = {"Cache-Control": "private, no-store", "Vary": "Cookie, Authorization"}


def _ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    return xff.split(",")[0].strip() if xff else (request.client.host if request.client else "?")


def _limit(request: Request, route: str, per_min: int, extra: str = "") -> None:
    ok, reset = get_bus().allow(_ip(request) + extra, route, per_min)
    if not ok:
        raise HTTPException(429, f"Too many attempts. Try again in {reset}s.", headers={"Retry-After": str(reset)})


def _wf(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except library.WorkflowError as exc:
        raise HTTPException(exc.status, str(exc))


def _set_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(auth.COOKIE, token, max_age=settings.session_ttl_hours * 3600, httponly=True,
                    secure=settings.cookie_secure, samesite="strict", path="/")


# ---------------------------------------------------------------------------
# Accounts
class RegisterIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=200)
    display_name: str = Field(default="", max_length=80)


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=200)


@router.post("/auth/register")
def register(body: RegisterIn, request: Request, response: Response) -> dict:
    if not settings.registration_open:
        raise HTTPException(403, "Self-registration is disabled on this library. Ask a librarian for an account.")
    _limit(request, "register", 5)
    uid = auth.create_user(body.email, body.password, body.display_name)  # always the 'reader' role only
    audit(None, "user.register", "user", uid)
    out = auth.login(body.email, body.password)
    token, p = out  # type: ignore[misc]
    _set_cookie(response, token)
    return {"user": p.public()}


@router.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response) -> dict:
    _limit(request, "login", settings.rate_login_per_min)
    _limit(request, "login-email", settings.rate_login_per_min, "|" + body.email.strip().lower())
    out = auth.login(body.email, body.password)
    if not out:
        audit(None, "auth.login_failed", "user", None, "denied")
        raise HTTPException(401, "Invalid email or password.")
    token, p = out
    audit(p, "auth.login", "user", p.user_id)
    _set_cookie(response, token)  # the browser gets an HttpOnly cookie; JavaScript never sees the token
    return {"user": p.public(), "expires_in_s": settings.session_ttl_hours * 3600}


@router.post("/auth/token")
def token(body: LoginIn, request: Request) -> dict:
    """For scripts and API clients: returns a bearer token (no cookie)."""
    _limit(request, "login", settings.rate_login_per_min)
    _limit(request, "login-email", settings.rate_login_per_min, "|" + body.email.strip().lower())
    out = auth.login(body.email, body.password)
    if not out:
        audit(None, "auth.login_failed", "user", None, "denied")
        raise HTTPException(401, "Invalid email or password.")
    audit(out[1], "auth.token", "user", out[1].user_id)
    return {"token": out[0], "token_type": "bearer", "expires_in_s": settings.session_ttl_hours * 3600}


@router.post("/auth/logout")
def logout(request: Request, response: Response, p: Principal = Depends(auth.current_principal)) -> dict:
    token, _ = auth.token_from(request)
    if token:
        auth.logout(token)
    response.delete_cookie(auth.COOKIE, path="/")
    if p.authenticated:
        audit(p, "auth.logout", "user", p.user_id)
    return {"ok": True}


@router.get("/auth/me")
def me(response: Response, p: Principal = Depends(auth.current_principal)) -> dict:
    response.headers.update(NO_STORE)
    return {"user": p.public() if p.authenticated else None,
            "registration_open": settings.registration_open}


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)


@router.post("/auth/password")
def change_password(body: PasswordIn, request: Request, p: Principal = Depends(auth.require_user)) -> dict:
    _limit(request, "password", 5)
    row = get_db().query("SELECT password_hash FROM users WHERE id=%s", (p.user_id,), primary=True)[0]
    if not auth.verify_password(body.current_password, row["password_hash"]):
        raise HTTPException(401, "Current password is wrong.")
    auth.check_password_strength(body.new_password, p.email)
    get_db().execute("UPDATE users SET password_hash=%s, updated_at=%s WHERE id=%s",
                     (auth.hash_password(body.new_password), utcnow(), p.user_id))
    auth.revoke_sessions(p.user_id, keep_session=p.session_id)  # sign out every other device
    audit(p, "auth.password_change", "user", p.user_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Public catalogue and reader
def _book_card(p: Principal, r: dict) -> dict:
    return {"id": r["id"], "title": r["title"], "subtitle": r.get("subtitle"), "author": r.get("author"),
            "lang": r["lang"], "kind": r.get("kind"), "source": r.get("source"), "publisher": r.get("publisher"),
            "pub_year": r.get("pub_year"), "subject": r.get("subject"), "licence": r.get("licence"),
            "licence_url": r.get("licence_url"), "url": r.get("url") or None, "origin": r.get("origin"),
            "access": access_label(p, r)}


SORTS = {"recent": "COALESCE(published_at, created_at) DESC, id DESC", "title": "title ASC, id ASC",
         "year": "pub_year DESC, id DESC"}


@router.get("/catalogue")
def catalogue(response: Response, q: str = "", lang: str = "", origin: str = "", policy: str = "", sort: str = "recent",
              page: int = 1, per_page: int = 20, p: Principal = Depends(auth.current_principal)) -> dict:
    """Browsable digital library: filtering, sorting and pagination happen in SQL, inside the policy predicate."""
    response.headers.update(NO_STORE)
    page, per_page = max(1, page), max(1, min(per_page, 50))
    where, params = discover_sql(p)
    if q.strip():
        like = f"%{q.strip()[:100].lower()}%"
        where += " AND (LOWER(title) LIKE %s OR LOWER(COALESCE(author, '')) LIKE %s OR LOWER(COALESCE(subject, '')) LIKE %s)"
        params += [like, like, like]
    if lang:
        where += " AND lang=%s"
        params.append(lang)
    if origin in ("managed", "harvest"):
        where += " AND origin=%s"
        params.append(origin)
    if policy in POLICIES:
        where += " AND policy=%s"
        params.append(policy)
    db = get_db()
    total = int(db.query(f"SELECT COUNT(*) AS n FROM resources WHERE {where}", params, primary=True)[0]["n"])
    rows = db.query(f"SELECT * FROM resources WHERE {where} ORDER BY {SORTS.get(sort, SORTS['recent'])} LIMIT %s OFFSET %s",
                    [*params, per_page, (page - 1) * per_page], primary=True)
    return {"items": [_book_card(p, r) for r in rows], "page": page, "per_page": per_page, "total": total,
            "pages": max(1, math.ceil(total / per_page))}


def _load(p: Principal, rid: int, action: str, request: Request | None = None) -> dict:
    r = library.get_resource(rid)
    d = decide(p, r, action)
    if not d.allowed:
        if r is not None and d.status != 404:
            audit(p, f"access.denied.{action}", "resource", rid, "denied", {"reason": d.reason})
        elif r is not None:
            audit(p, f"access.hidden.{action}", "resource", rid, "denied")
        if d.status == 404:
            raise HTTPException(404, "Resource not found.")  # identical for missing and hidden resources
        raise HTTPException(d.status, d.reason)
    return r  # type: ignore[return-value]


@router.get("/books/{rid}")
def book(rid: int, response: Response, p: Principal = Depends(auth.current_principal)) -> dict:
    response.headers.update(NO_STORE)
    r = _load(p, rid, "discover")
    db = get_db()
    card = _book_card(p, r)
    card.update({"description": r.get("description"), "categories": r.get("categories"), "isbn": r.get("isbn"),
                 "edition": r.get("edition"), "rights_holder": r.get("rights_holder"), "attribution": r.get("attribution"),
                 "status": r["status"], "published_at": library.ts(r.get("published_at"))})
    can_read = card["access"]["can_read"]
    card["chapters"] = db.query("SELECT chapter_no, title, start_page, end_page FROM book_chapters WHERE resource_id=%s "
                                "ORDER BY chapter_no", (rid,), primary=True) if can_read else []
    npages = db.query("SELECT COUNT(*) AS n FROM book_pages WHERE resource_id=%s", (rid,), primary=True)[0]["n"] if can_read else 0
    card["pages"] = int(npages)
    card["excerpts"] = []
    if can_read and not npages:  # harvested open records: show the excerpts GranthSetu holds
        card["excerpts"] = [x["text"] for x in db.query(
            "SELECT text FROM passages WHERE resource_id=%s AND kind='content' ORDER BY chunk_no LIMIT 8", (rid,), primary=True)]
    f = library.current_file(rid) if card["access"]["can_download"] else None
    card["file"] = {"mime": f["mime"], "size_bytes": f["size_bytes"], "filename": f["filename"]} if f else None
    if p.authenticated and can_read:
        pr = db.query("SELECT page_no FROM reading_progress WHERE user_id=%s AND resource_id=%s", (p.user_id, rid), primary=True)
        card["progress_page"] = int(pr[0]["page_no"]) if pr else None
        card["bookmarks"] = db.query("SELECT id, page_no, note FROM bookmarks WHERE user_id=%s AND resource_id=%s ORDER BY page_no",
                                     (p.user_id, rid), primary=True)
    return card


@router.get("/books/{rid}/pages/{page_no}")
def book_page(rid: int, page_no: int, response: Response, p: Principal = Depends(auth.current_principal)) -> dict:
    _load(p, rid, "read")
    response.headers.update(NO_STORE)
    rows = get_db().query("SELECT page_no, chapter_no, text, ocr FROM book_pages WHERE resource_id=%s AND page_no=%s",
                          (rid, page_no), primary=True)
    if not rows:
        raise HTTPException(404, "No such page.")
    pg = rows[0]
    total = get_db().query("SELECT COUNT(*) AS n FROM book_pages WHERE resource_id=%s", (rid,), primary=True)[0]["n"]
    return {"page_no": pg["page_no"], "chapter_no": pg["chapter_no"], "text": pg["text"], "ocr": bool(pg["ocr"]),
            "pages": int(total)}


@router.get("/books/{rid}/search")
def book_search(rid: int, q: str, response: Response, p: Principal = Depends(auth.current_principal)) -> dict:
    _load(p, rid, "read")
    response.headers.update(NO_STORE)
    q = q.strip()[:100]
    if len(q) < 2:
        raise HTTPException(422, "Search for at least 2 characters.")
    rows = get_db().query("SELECT page_no, text FROM book_pages WHERE resource_id=%s AND LOWER(text) LIKE %s ORDER BY page_no LIMIT 50",
                          (rid, f"%{q.lower()}%"), primary=True)
    hits = []
    for r in rows:
        i = r["text"].lower().find(q.lower())
        hits.append({"page_no": r["page_no"], "snippet": r["text"][max(0, i - 80): i + len(q) + 80]})
    return {"query": q, "hits": hits}


@router.get("/books/{rid}/download")
def download(rid: int, p: Principal = Depends(auth.current_principal)) -> RawResponse:
    from .storage import StorageError, get_storage

    r = _load(p, rid, "download")
    f = library.current_file(rid)
    if not f:
        raise HTTPException(404, "No file is available for this resource.")
    try:
        data = get_storage().get(f["storage_key"])
    except StorageError:
        raise HTTPException(503, "The file is temporarily unavailable.")
    audit(p, "file.download", "resource", rid)
    ext = ".pdf" if f["mime"] == "application/pdf" else ".txt"
    name = "".join(ch for ch in r["title"][:60] if ch.isalnum() or ch in " -_").strip() or "book"
    return RawResponse(data, media_type=f["mime"], headers={
        **NO_STORE, "Content-Disposition": f'attachment; filename="{name}{ext}"', "X-Content-Type-Options": "nosniff"})


# ---------------------------------------------------------------------------
# Personal reading data
class ProgressIn(BaseModel):
    page_no: int = Field(ge=1, le=100000)


@router.put("/me/progress/{rid}")
def set_progress(rid: int, body: ProgressIn, p: Principal = Depends(auth.require_user)) -> dict:
    _load(p, rid, "read")
    get_db().execute("INSERT INTO reading_progress(user_id, resource_id, page_no, updated_at) VALUES (%s, %s, %s, %s) "
                     "ON CONFLICT(user_id, resource_id) DO UPDATE SET page_no=excluded.page_no, updated_at=excluded.updated_at",
                     (p.user_id, rid, body.page_no, utcnow()))
    return {"ok": True}


class BookmarkIn(BaseModel):
    resource_id: int
    page_no: int = Field(ge=1, le=100000)
    note: str = Field(default="", max_length=500)


@router.post("/me/bookmarks")
def add_bookmark(body: BookmarkIn, p: Principal = Depends(auth.require_user)) -> dict:
    _load(p, body.resource_id, "read")
    rows = get_db().execute("INSERT INTO bookmarks(user_id, resource_id, page_no, note, created_at) VALUES (%s, %s, %s, %s, %s) "
                            "ON CONFLICT(user_id, resource_id, page_no) DO UPDATE SET note=excluded.note RETURNING id",
                            (p.user_id, body.resource_id, body.page_no, body.note.strip() or None, utcnow()))
    return {"id": rows[0]["id"]}


@router.delete("/me/bookmarks/{bid}")
def delete_bookmark(bid: int, p: Principal = Depends(auth.require_user)) -> dict:
    rows = get_db().execute("DELETE FROM bookmarks WHERE id=%s AND user_id=%s RETURNING id", (bid, p.user_id))
    if not rows:
        raise HTTPException(404, "Bookmark not found.")
    return {"ok": True}


@router.get("/me/library")
def my_library(response: Response, p: Principal = Depends(auth.require_user)) -> dict:
    """Reading history and bookmarks. Titles are shown only while the user can still discover the resource."""
    response.headers.update(NO_STORE)
    db = get_db()
    out: dict[str, list] = {"reading": [], "bookmarks": []}
    for row in db.query("SELECT resource_id, page_no, updated_at FROM reading_progress WHERE user_id=%s ORDER BY updated_at DESC",
                        (p.user_id,), primary=True):
        r = library.get_resource(int(row["resource_id"]))
        ok = decide(p, r, "discover").allowed
        out["reading"].append({"resource_id": row["resource_id"], "page_no": row["page_no"], "updated_at": library.ts(row["updated_at"]),
                               "title": r["title"] if ok else None, "can_read": ok and decide(p, r, "read").allowed})
    for row in db.query("SELECT id, resource_id, page_no, note FROM bookmarks WHERE user_id=%s ORDER BY id DESC", (p.user_id,), primary=True):
        r = library.get_resource(int(row["resource_id"]))
        ok = decide(p, r, "discover").allowed
        out["bookmarks"].append({**row, "title": r["title"] if ok else None, "can_read": ok and decide(p, r, "read").allowed})
    return out


@router.delete("/me/data")
def delete_my_data(p: Principal = Depends(auth.require_user)) -> dict:
    db = get_db()
    db.transaction([("DELETE FROM reading_progress WHERE user_id=%s", (p.user_id,)),
                    ("DELETE FROM bookmarks WHERE user_id=%s", (p.user_id,))])
    audit(p, "user.delete_reading_data", "user", p.user_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Library management
@router.get("/manage/options")
def manage_options(p: Principal = Depends(auth.require_user)) -> dict:
    db = get_db()
    return {"policies": POLICIES, "rights_bases": RIGHTS_BASES, "catalogue": list(CATALOGUE), "statuses": STATUSES,
            "languages": sorted(library.LANGS), "types": sorted(library.TYPES),
            "transitions": {k: {"from": sorted(v[0]), "to": v[1], "permission": v[2], "reason_required": v[3]}
                            for k, v in library.TRANSITIONS.items()},
            "institutions": db.query("SELECT id, name FROM institutions ORDER BY name", primary=True),
            "groups": db.query("SELECT id, name, institution_id FROM user_groups ORDER BY name", primary=True),
            "max_upload_mb": settings.max_book_bytes // 1024 // 1024,
            "separate_reviewer_required": settings.require_separate_reviewer}


@router.get("/manage/dashboard")
def manage_dashboard(p: Principal = Depends(auth.require("resource.create"))) -> dict:
    db = get_db()
    mine = "" if p.is_staff else " AND created_by=%s"
    prm = [] if p.is_staff else [p.user_id]
    by_status = {r["status"]: int(r["n"]) for r in db.query(
        f"SELECT status, COUNT(*) AS n FROM resources WHERE origin='managed'{mine} GROUP BY status", prm, primary=True)}
    jobs = {r["status"]: int(r["n"]) for r in db.query("SELECT status, COUNT(*) AS n FROM processing_jobs GROUP BY status",
                                                       primary=True)} if p.is_staff else {}
    pending_rights = int(db.query("SELECT COUNT(*) AS n FROM resources WHERE origin='managed' AND rights_status='unverified' "
                                  "AND status IN ('SUBMITTED', 'UNDER_REVIEW')", primary=True)[0]["n"]) if p.is_staff else 0
    return {"by_status": by_status, "jobs": jobs, "pending_rights": pending_rights,
            "outbox_pending": int(db.query("SELECT COUNT(*) AS n FROM outbox WHERE dispatched_at IS NULL", primary=True)[0]["n"])
            if p.is_staff else None}


@router.get("/manage/resources")
def manage_list(status: str = "", q: str = "", page: int = 1, origin: str = "managed",
                p: Principal = Depends(auth.require("resource.create"))) -> dict:
    where, params = ["1=1"], []
    if origin in ("managed", "harvest"):
        where.append("origin=%s")
        params.append(origin)
    if not p.is_staff:
        where.append("created_by=%s")
        params.append(p.user_id)
    if status:
        where.append("status=%s")
        params.append(status)
    if q.strip():
        where.append("LOWER(title) LIKE %s")
        params.append(f"%{q.strip().lower()[:100]}%")
    w = " AND ".join(where)
    db = get_db()
    total = int(db.query(f"SELECT COUNT(*) AS n FROM resources WHERE {w}", params, primary=True)[0]["n"])
    rows = db.query(f"SELECT id, title, author, lang, status, policy, catalogue, rights_basis, rights_status, origin, created_by, "
                    f"updated_at FROM resources WHERE {w} ORDER BY updated_at DESC, id DESC LIMIT 25 OFFSET %s",
                    [*params, (max(page, 1) - 1) * 25], primary=True)
    for r in rows:
        r["updated_at"] = library.ts(r["updated_at"])
    return {"items": rows, "total": total, "page": page, "pages": max(1, math.ceil(total / 25))}


@router.post("/manage/resources")
def manage_create(body: dict, p: Principal = Depends(auth.require("resource.create"))) -> dict:
    return {"id": _wf(library.create_resource, p, body)}


def _detail(p: Principal, rid: int) -> dict:
    r = _wf(library.require_manage, p, library.get_resource(rid))
    db = get_db()
    r = dict(r)
    for k in ("created_at", "updated_at", "published_at"):
        r[k] = library.ts(r.get(k))
    r.pop("access", None)
    files = db.query("SELECT id, filename, mime, size_bytes, sha256, scan_status, is_current, uploaded_at FROM book_files "
                     "WHERE resource_id=%s ORDER BY id DESC", (rid,), primary=True)
    for f in files:
        f["uploaded_at"] = library.ts(f["uploaded_at"])
        f["is_current"] = bool(f["is_current"])
    ev = db.query("SELECT e.*, u.email AS actor FROM publication_events e LEFT JOIN users u ON u.id=e.actor_id "
                  "WHERE resource_id=%s ORDER BY e.id", (rid,), primary=True)
    rv = db.query("SELECT b.*, u.email AS reviewer FROM book_reviews b LEFT JOIN users u ON u.id=b.reviewer_id "
                  "WHERE resource_id=%s ORDER BY b.id", (rid,), primary=True)
    jobs = db.query("SELECT * FROM processing_jobs WHERE resource_id=%s ORDER BY id DESC LIMIT 10", (rid,), primary=True)
    errs = db.query("SELECT * FROM processing_errors WHERE resource_id=%s ORDER BY id DESC LIMIT 10", (rid,), primary=True)
    ents = db.query("SELECT * FROM entitlements WHERE resource_id=%s ORDER BY id DESC", (rid,), primary=True) \
        if p.can("entitlement.manage") else []
    now = utcnow()
    for e in ents:
        e["active"] = e["revoked_at"] is None and (e["expires_at"] is None or as_dt(e["expires_at"]) > now)
    for row in [*ev, *rv, *jobs, *errs, *ents]:
        for k, v in list(row.items()):
            if k.endswith("_at"):
                row[k] = library.ts(v)
    for j in jobs:
        j["result"] = json.loads(j["result"]) if j.get("result") else None
    pages = int(db.query("SELECT COUNT(*) AS n FROM book_pages WHERE resource_id=%s", (rid,), primary=True)[0]["n"])
    own = r.get("created_by") is not None and int(r["created_by"]) == p.user_id
    allowed = [a for a, (frm, _, perm, _) in library.TRANSITIONS.items()
               if r["status"] in frm and ((perm is None and (own or p.can("resource.edit_any"))) or (perm and p.can(perm)))]
    return {"resource": r, "files": files, "events": ev, "reviews": rv, "jobs": jobs, "errors": errs, "entitlements": ents,
            "pages": pages, "problems": library.publish_problems(r, any(f["is_current"] for f in files)),
            "allowed_actions": allowed, "can": {"verify_rights": p.can("rights.verify"), "edit_policy": p.can("policy.edit"),
                                                "entitlements": p.can("entitlement.manage"), "reprocess": p.can("jobs.manage")}}


@router.get("/manage/resources/{rid}")
def manage_get(rid: int, p: Principal = Depends(auth.require_user)) -> dict:
    return _detail(p, rid)


@router.patch("/manage/resources/{rid}")
def manage_update(rid: int, body: dict, p: Principal = Depends(auth.require_user)) -> dict:
    _wf(library.update_resource, p, rid, body)
    return _detail(p, rid)


@router.post("/manage/resources/{rid}/file")
async def manage_upload(rid: int, request: Request, file: UploadFile = File(...), p: Principal = Depends(auth.require_user)) -> dict:
    _limit(request, "upload", 20, f"|{p.user_id}")
    data = await file.read(settings.max_book_bytes + 1)
    if len(data) > settings.max_book_bytes:
        raise HTTPException(413, f"File too large (max {settings.max_book_bytes // 1024 // 1024} MB).")
    try:
        return _wf(library.attach_file, p, rid, file.filename or "upload", data)
    finally:
        del data


@router.get("/manage/resources/{rid}/file")
def manage_file(rid: int, p: Principal = Depends(auth.require("submission.review"))) -> RawResponse:
    """Reviewers may inspect the uploaded file of an unpublished resource. Every access is audited."""
    from .storage import StorageError, get_storage

    _wf(library.require_manage, p, library.get_resource(rid))
    f = library.current_file(rid)
    if not f:
        raise HTTPException(404, "No file uploaded.")
    try:
        data = get_storage().get(f["storage_key"])
    except StorageError:
        raise HTTPException(503, "The file is temporarily unavailable.")
    audit(p, "file.review_download", "resource", rid)
    return RawResponse(data, media_type=f["mime"], headers={**NO_STORE, "Content-Disposition": "attachment; filename=review" +
                                                            (".pdf" if f["mime"] == "application/pdf" else ".txt")})


class TransitionIn(BaseModel):
    action: str = Field(max_length=30)
    reason: str = Field(default="", max_length=2000)


@router.post("/manage/resources/{rid}/transition")
def manage_transition(rid: int, body: TransitionIn, p: Principal = Depends(auth.require_user)) -> dict:
    _wf(library.transition, p, rid, body.action, body.reason)
    return _detail(p, rid)


class RightsIn(BaseModel):
    decision: str
    notes: str = Field(default="", max_length=2000)


@router.post("/manage/resources/{rid}/rights")
def manage_rights(rid: int, body: RightsIn, p: Principal = Depends(auth.require_user)) -> dict:
    _wf(library.verify_rights, p, rid, body.decision, body.notes.strip() or None)
    return _detail(p, rid)


@router.post("/manage/resources/{rid}/reprocess")
def manage_reprocess(rid: int, p: Principal = Depends(auth.require_user)) -> dict:
    _wf(library.request_reprocess, p, rid)
    return _detail(p, rid)


@router.delete("/manage/resources/{rid}")
def manage_purge(rid: int, p: Principal = Depends(auth.require_user)) -> dict:
    _wf(library.purge, p, rid)
    return {"ok": True}


class GrantIn(BaseModel):
    subject_type: str
    subject_id: int | None = None
    email: str | None = None
    expires_at: str | None = None
    reason: str = Field(default="", max_length=500)


@router.post("/manage/resources/{rid}/entitlements")
def manage_grant(rid: int, body: GrantIn, p: Principal = Depends(auth.require_user)) -> dict:
    sid = body.subject_id
    if body.subject_type == "user" and body.email:
        rows = get_db().query("SELECT id FROM users WHERE email=%s", (body.email.strip().lower(),), primary=True)
        if not rows:
            raise HTTPException(404, "No user with that email.")
        sid = int(rows[0]["id"])
    if sid is None:
        raise HTTPException(422, "Choose who receives the entitlement.")
    _wf(library.grant, p, rid, body.subject_type, sid, body.expires_at, body.reason.strip() or None)
    return _detail(p, rid)


@router.delete("/manage/entitlements/{eid}")
def manage_revoke(eid: int, p: Principal = Depends(auth.require_user)) -> dict:
    _wf(library.revoke, p, eid)
    return {"ok": True}


@router.get("/manage/jobs")
def manage_jobs(status: str = "", p: Principal = Depends(auth.require("jobs.manage"))) -> dict:
    db = get_db()
    where, params = ("WHERE j.status=%s", [status]) if status else ("", [])
    rows = db.query(f"SELECT j.*, r.title FROM processing_jobs j JOIN resources r ON r.id=j.resource_id {where} "
                    f"ORDER BY j.id DESC LIMIT 100", params, primary=True)
    for j in rows:
        for k in ("created_at", "updated_at", "started_at", "finished_at"):
            j[k] = library.ts(j[k])
        j["result"] = json.loads(j["result"]) if j.get("result") else None
    return {"jobs": rows}


@router.post("/manage/jobs/{jid}/retry")
def manage_job_retry(jid: int, p: Principal = Depends(auth.require("jobs.manage"))) -> dict:
    _wf(library.retry_job, p, jid)
    return {"ok": True}


@router.get("/manage/audit")
def manage_audit(target_id: str = "", action: str = "", page: int = 1, p: Principal = Depends(auth.require("audit.view"))) -> dict:
    where, params = ["1=1"], []
    if target_id:
        where.append("a.target_id=%s")
        params.append(target_id)
    if action:
        where.append("a.action LIKE %s")
        params.append(action.replace("%", "") + "%")
    rows = get_db().query(f"SELECT a.id, a.action, a.target_type, a.target_id, a.outcome, a.detail, a.created_at, u.email AS actor "
                          f"FROM audit_logs a LEFT JOIN users u ON u.id=a.actor_id WHERE {' AND '.join(where)} "
                          f"ORDER BY a.id DESC LIMIT 50 OFFSET %s", [*params, (max(page, 1) - 1) * 50], primary=True)
    for r in rows:
        r["created_at"] = library.ts(r["created_at"])
    return {"items": rows, "page": page}


# ---------------------------------------------------------------------------
# Administration: users, roles, institutions, groups
@router.get("/admin/users")
def admin_users(q: str = "", p: Principal = Depends(auth.require_user)) -> dict:
    if not (p.can("user.manage") or p.can("entitlement.manage") or p.admin_of):
        raise HTTPException(403, "You do not have permission for this action.")
    db = get_db()
    like = f"%{q.strip().lower()[:100]}%"
    rows = db.query("SELECT id, email, display_name, is_active, created_at, last_login_at FROM users "
                    "WHERE LOWER(email) LIKE %s OR LOWER(display_name) LIKE %s ORDER BY id LIMIT 100", (like, like), primary=True)
    for u in rows:
        u["roles"] = [r["role"] for r in db.query("SELECT role FROM user_roles WHERE user_id=%s ORDER BY role", (u["id"],), primary=True)]
        u["is_active"] = bool(u["is_active"])
        u["created_at"], u["last_login_at"] = library.ts(u["created_at"]), library.ts(u["last_login_at"])
    return {"users": rows, "roles": ROLES, "can_manage_roles": p.can("user.manage")}


class RoleIn(BaseModel):
    role: str


@router.post("/admin/users/{uid}/roles")
def admin_grant_role(uid: int, body: RoleIn, p: Principal = Depends(auth.require("user.manage"))) -> dict:
    if body.role not in ROLES:
        raise HTTPException(422, "Unknown role.")
    db = get_db()
    if not db.query("SELECT 1 FROM users WHERE id=%s", (uid,), primary=True):
        raise HTTPException(404, "Unknown user.")
    db.execute("INSERT INTO user_roles(user_id, role, granted_by, granted_at) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
               (uid, body.role, p.user_id, utcnow()))
    audit(p, "user.role_grant", "user", uid, detail={"role": body.role})
    return {"ok": True}


@router.delete("/admin/users/{uid}/roles/{role}")
def admin_revoke_role(uid: int, role: str, p: Principal = Depends(auth.require("user.manage"))) -> dict:
    db = get_db()
    if role == "platform_admin" and uid == p.user_id:
        raise HTTPException(409, "You cannot remove your own administrator role.")
    if role == "platform_admin":
        n = db.query("SELECT COUNT(*) AS n FROM user_roles WHERE role='platform_admin'", primary=True)[0]["n"]
        if int(n) <= 1:
            raise HTTPException(409, "The last platform administrator cannot be removed.")
    db.execute("DELETE FROM user_roles WHERE user_id=%s AND role=%s", (uid, role))
    audit(p, "user.role_revoke", "user", uid, detail={"role": role})
    return {"ok": True}


class ActiveIn(BaseModel):
    active: bool


@router.post("/admin/users/{uid}/active")
def admin_set_active(uid: int, body: ActiveIn, p: Principal = Depends(auth.require("user.manage"))) -> dict:
    if uid == p.user_id and not body.active:
        raise HTTPException(409, "You cannot deactivate yourself.")
    get_db().execute("UPDATE users SET is_active=%s, updated_at=%s WHERE id=%s", (body.active, utcnow(), uid))
    if not body.active:
        auth.revoke_sessions(uid)
    audit(p, "user.activate" if body.active else "user.deactivate", "user", uid)
    return {"ok": True}


class NameIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    institution_id: int | None = None


@router.get("/admin/orgs")
def admin_orgs(p: Principal = Depends(auth.require_user)) -> dict:
    if not (p.can("institution.manage") or p.admin_of or p.can("entitlement.manage")):
        raise HTTPException(403, "You do not have permission for this action.")
    db = get_db()
    insts = db.query("SELECT id, name FROM institutions ORDER BY name", primary=True)
    groups = db.query("SELECT id, name, institution_id FROM user_groups ORDER BY name", primary=True)
    for i in insts:
        i["members"] = db.query("SELECT u.id, u.email, m.member_role, m.expires_at FROM institution_memberships m JOIN users u "
                                "ON u.id=m.user_id WHERE m.institution_id=%s ORDER BY u.email", (i["id"],), primary=True)
        i["can_manage"] = p.can("institution.manage") or int(i["id"]) in p.admin_of
    for g in groups:
        g["members"] = db.query("SELECT u.id, u.email, m.expires_at FROM group_memberships m JOIN users u ON u.id=m.user_id "
                                "WHERE m.group_id=%s ORDER BY u.email", (g["id"],), primary=True)
        g["can_manage"] = p.can("institution.manage") or (g["institution_id"] is not None and int(g["institution_id"]) in p.admin_of)
    for row in [*(m for i in insts for m in i["members"]), *(m for g in groups for m in g["members"])]:
        row["expires_at"] = library.ts(row["expires_at"])
    return {"institutions": insts, "groups": groups, "can_create": p.can("institution.manage")}


@router.post("/admin/institutions")
def admin_new_inst(body: NameIn, p: Principal = Depends(auth.require("institution.manage"))) -> dict:
    rows = get_db().execute("INSERT INTO institutions(name, created_at) VALUES (%s, %s) ON CONFLICT(name) DO NOTHING RETURNING id",
                            (body.name.strip(), utcnow()))
    if not rows:
        raise HTTPException(409, "An institution with that name exists.")
    audit(p, "institution.create", "institution", rows[0]["id"])
    return {"id": rows[0]["id"]}


@router.post("/admin/groups")
def admin_new_group(body: NameIn, p: Principal = Depends(auth.require_user)) -> dict:
    if not (p.can("institution.manage") or (body.institution_id and body.institution_id in p.admin_of)):
        raise HTTPException(403, "You do not have permission for this action.")
    rows = get_db().execute("INSERT INTO user_groups(name, institution_id, created_at) VALUES (%s, %s, %s) "
                            "ON CONFLICT(name) DO NOTHING RETURNING id", (body.name.strip(), body.institution_id, utcnow()))
    if not rows:
        raise HTTPException(409, "A group with that name exists.")
    audit(p, "group.create", "group", rows[0]["id"])
    return {"id": rows[0]["id"]}


class MemberIn(BaseModel):
    email: str
    expires_at: str | None = None
    member_role: str = "member"


def _org_guard(p: Principal, kind: str, oid: int) -> None:
    if p.can("institution.manage"):
        return
    if kind == "institution" and oid in p.admin_of:
        return
    if kind == "group":
        g = get_db().query("SELECT institution_id FROM user_groups WHERE id=%s", (oid,), primary=True)
        if g and g[0]["institution_id"] is not None and int(g[0]["institution_id"]) in p.admin_of:
            return
    raise HTTPException(403, "You do not have permission for this action.")


@router.post("/admin/{kind}/{oid}/members")
def admin_add_member(kind: str, oid: int, body: MemberIn, p: Principal = Depends(auth.require_user)) -> dict:
    if kind not in ("institutions", "groups"):
        raise HTTPException(404, "Not found.")
    k = kind[:-1]
    _org_guard(p, k, oid)
    db = get_db()
    u = db.query("SELECT id FROM users WHERE email=%s", (body.email.strip().lower(),), primary=True)
    if not u:
        raise HTTPException(404, "No user with that email.")
    exp = as_dt(body.expires_at) if body.expires_at else None
    if k == "institution":
        role = body.member_role if body.member_role in ("member", "admin") else "member"
        if role == "admin" and not p.can("institution.manage"):
            raise HTTPException(403, "Only platform administrators can appoint institution administrators.")
        db.transaction([("INSERT INTO institution_memberships(institution_id, user_id, member_role, expires_at, created_at) "
                         "VALUES (%s, %s, %s, %s, %s) ON CONFLICT(institution_id, user_id) DO UPDATE SET "
                         "member_role=excluded.member_role, expires_at=excluded.expires_at",
                         (oid, u[0]["id"], role, exp, utcnow()))])
    else:
        db.execute("INSERT INTO group_memberships(group_id, user_id, expires_at, created_at) VALUES (%s, %s, %s, %s) "
                   "ON CONFLICT(group_id, user_id) DO UPDATE SET expires_at=excluded.expires_at", (oid, u[0]["id"], exp, utcnow()))
    from .policy import bump_policy_sql

    db.transaction([bump_policy_sql()])
    audit(p, f"{k}.member_add", k, oid, detail={"user": u[0]["id"]})
    return {"ok": True}


@router.delete("/admin/{kind}/{oid}/members/{uid}")
def admin_remove_member(kind: str, oid: int, uid: int, p: Principal = Depends(auth.require_user)) -> dict:
    if kind not in ("institutions", "groups"):
        raise HTTPException(404, "Not found.")
    k = kind[:-1]
    _org_guard(p, k, oid)
    from .policy import bump_policy_sql

    table, col = ("institution_memberships", "institution_id") if k == "institution" else ("group_memberships", "group_id")
    get_db().transaction([(f"DELETE FROM {table} WHERE {col}=%s AND user_id=%s", (oid, uid)), bump_policy_sql()])
    audit(p, f"{k}.member_remove", k, oid, detail={"user": uid})
    return {"ok": True}


def ensure_json(v: Any) -> Any:  # pragma: no cover - helper for debugging
    return json.loads(json.dumps(v, default=str))
