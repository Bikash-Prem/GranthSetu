"""HTTP routes for accounts, the reader, personal data and the library management portal.

Every route resolves the caller server-side (`auth.current_subject`) and every
route that touches a resource goes through `authz.decide` (via `gate`) or through
`library.*`, which enforces object-level permissions itself.

Status codes: a resource the caller may not even discover answers 404 with the
same body as a resource that does not exist, so identifiers cannot be probed.
A discoverable resource whose content the caller may not read answers 401
(not signed in) or 403.
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import audit, auth, authz, jobs, library
from .auth import Subject, current_subject, require_perm, require_user
from .bus import get_bus
from .config import settings
from .db import get_db, now_iso, to_utc_iso
from .migrations import POLICIES, RIGHTS_CATEGORIES, STATUSES
from .storage import StorageError, get_storage

router = APIRouter(prefix="/api")


def client_id(request: Request) -> str:
    # uvicorn resolves the real client from X-Forwarded-For only for trusted proxies (--forwarded-allow-ips),
    # so we never read that header ourselves: a client cannot pick its own rate-limit bucket.
    return request.client.host if request.client else "unknown"


def limiter(route: str, per_min_attr: str):
    def dep(request: Request) -> None:
        ok, reset = get_bus().allow(client_id(request), route, getattr(settings, per_min_attr))
        if not ok:
            raise HTTPException(429, detail=f"Too many requests. Try again in {reset}s.", headers={"Retry-After": str(reset)})
    return dep


def gate(subject: Subject, rid: int, need: str = "discover") -> authz.Decision:
    """Authorise one operation on one resource, or raise. `need`: discover | read | download | ai."""
    d = authz.decide(subject, rid)
    preview = d.manage_view  # staff, or the contributor, working on it in the portal
    if not d.exists or not (d.discover or preview):
        raise HTTPException(404, "Not found")
    if need == "discover" or getattr(d, need) or (preview and need in ("read", "download")):
        return d
    audit.record("access.denied", subject, "resource", rid, "denied", {"operation": need, "policy": d.policy})
    if not subject.authenticated:
        raise HTTPException(401, "Sign in to access this resource.")
    raise HTTPException(403, "You do not have access to this content.")


# =========================== accounts =========================================
class RegisterIn(BaseModel):
    email: str = Field(max_length=256)
    password: str = Field(max_length=256)
    display_name: str = Field(default="", max_length=120)


class LoginIn(BaseModel):
    email: str = Field(max_length=256)
    password: str = Field(max_length=256)


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(auth.COOKIE, token, max_age=settings.session_ttl_hours * 3600, httponly=True, samesite="lax",
                        secure=settings.cookie_secure, path="/api")


@router.post("/auth/register", status_code=201, dependencies=[Depends(limiter("register", "rate_register_per_min"))])
def register(body: RegisterIn, request: Request, response: Response) -> dict:
    if not library.get_setting("auth.registration_open", True):
        raise HTTPException(403, "Self-registration is closed. Ask your librarian for an account.")
    # New accounts are always plain readers. Roles are assigned only by an administrator.
    uid = auth.create_user(body.email, body.password, body.display_name, roles=("reader",))
    audit.record("auth.register", None, "user", uid)
    token, subject = auth.login(body.email, body.password, request.headers.get("user-agent", ""))
    _set_cookie(response, token)
    return {"user": subject.public(), "token": token}


@router.post("/auth/login", dependencies=[Depends(limiter("login", "rate_login_per_min"))])
def login(body: LoginIn, request: Request, response: Response) -> dict:
    try:
        token, subject = auth.login(body.email, body.password, request.headers.get("user-agent", ""))
    except PermissionError as exc:
        raise HTTPException(401, str(exc)) from None
    _set_cookie(response, token)
    return {"user": subject.public(), "token": token}


@router.post("/auth/logout")
def logout(response: Response, subject: Subject = Depends(current_subject)) -> dict:
    if subject.session_id:
        auth.revoke_session(subject.session_id)
        audit.record("auth.logout", subject, "user", subject.user_id)
    response.delete_cookie(auth.COOKIE, path="/api")
    return {"ok": True}


@router.get("/auth/me")
def me(request: Request, subject: Subject = Depends(current_subject)) -> dict:
    return {"user": subject.public() if subject.authenticated else None,
            "session_expired": bool(getattr(request.state, "session_expired", False)),
            "registration_open": bool(library.get_setting("auth.registration_open", True))}


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


@router.post("/auth/password", dependencies=[Depends(limiter("login", "rate_login_per_min"))])
def change_password(body: PasswordIn, response: Response, subject: Subject = Depends(require_user)) -> dict:
    try:
        auth.change_password(subject, body.current_password, body.new_password)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None
    response.delete_cookie(auth.COOKIE, path="/api")
    return {"ok": True, "detail": "Password changed. Sign in again."}


class ProfileIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)  # the only self-editable profile field


@router.patch("/auth/me")
def update_profile(body: ProfileIn, subject: Subject = Depends(require_user)) -> dict:
    get_db().execute("UPDATE users SET display_name = %s, updated_at = %s WHERE id = %s",
                     (body.display_name.strip(), now_iso(), subject.user_id))
    return {"user": auth.load_subject(subject.user_id).public()}


class DeleteAccountIn(BaseModel):
    password: str = Field(max_length=256)


@router.post("/auth/delete-account")
def delete_account(body: DeleteAccountIn, response: Response, subject: Subject = Depends(require_user)) -> dict:
    """Erase the account and its personal reading data. Audit entries keep the numeric actor label only."""
    db = get_db()
    row = db.query("SELECT password_hash FROM users WHERE id = %s", (subject.user_id,), primary=True)[0]
    if not auth.verify_password(body.password, row["password_hash"]):
        raise HTTPException(403, "Password is wrong.")
    if "platform_admin" in subject.roles and _admin_count() <= 1:
        raise HTTPException(409, "The last platform administrator cannot delete their account.")
    with db.tx() as tx:
        audit.record("auth.account_deleted", subject, "user", subject.user_id, tx=tx)
        tx.execute("DELETE FROM entitlements WHERE subject_type = 'user' AND subject_id = %s", (subject.user_id,))
        tx.execute("DELETE FROM users WHERE id = %s", (subject.user_id,))  # cascades: sessions, roles, memberships, reading data
        db.bump_access_epoch(tx)
    response.delete_cookie(auth.COOKIE, path="/api")
    return {"ok": True}


# =========================== catalogue and reader =================================
@router.get("/catalogue")
def catalogue(q: str | None = Query(None, max_length=200), lang: str | None = None, subject: str | None = Query(None, max_length=120),
              kind: str | None = Query(None, max_length=80), year_from: int | None = None, year_to: int | None = None,
              licence: str | None = Query(None, max_length=120), access: str | None = None, source: str | None = None,
              sort: str = "recent", page: int = 1, page_size: int = 20, who: Subject = Depends(current_subject)) -> dict:
    return library.catalogue(who, q=q, lang=lang, subject_filter=subject, kind=kind, year_from=year_from, year_to=year_to,
                             licence=licence, policy=access, source=source, sort=sort, page=page, page_size=page_size)


@router.get("/catalogue/facets")
def catalogue_facets(who: Subject = Depends(current_subject)) -> dict:
    return library.facets(who)


def _chapters(rid: int) -> list[dict]:
    db = get_db()
    rows = db.query("SELECT c.id, c.position, c.title, c.page_start, c.page_end, "
                    "(SELECT COUNT(*) FROM passages p WHERE p.chapter_id = c.id) AS passages "
                    "FROM resource_chapters c WHERE c.resource_id = %s ORDER BY c.position", (rid,), primary=True)
    if rows:
        return rows
    n = db.query("SELECT COUNT(*) AS n FROM passages WHERE resource_id = %s AND kind = 'fulltext'", (rid,), primary=True)[0]["n"]
    # Connector records have no chapter structure: the text we hold is shown as one section.
    return [{"id": 0, "position": 0, "title": "Text held by GranthSetu", "page_start": None, "page_end": None, "passages": int(n)}] if n else []


@router.get("/resources/{rid}")
def resource_detail(rid: int, who: Subject = Depends(current_subject)) -> dict:
    d = gate(who, rid, "discover")
    db = get_db()
    res = db.query("SELECT * FROM resources WHERE id = %s", (rid,), primary=True)[0]
    can_read = d.read or d.manage_view
    file = library._current_file(lambda s, p: db.query(s, p, primary=True), rid) if can_read else None
    card = library.public_card(res, d, file)
    card["authors"] = [r["name"] for r in db.query("SELECT a.name FROM resource_authors ra JOIN authors a ON a.id = ra.author_id "
                                                   "WHERE ra.resource_id = %s ORDER BY ra.position", (rid,), primary=True)]
    card["categories"] = [r["name"] for r in db.query("SELECT name FROM resource_categories WHERE resource_id = %s ORDER BY name", (rid,), primary=True)]
    rights = db.query("SELECT x.category, x.rights_holder, x.allow_download, x.allow_ai_processing, l.code, l.name, l.url "
                      "FROM resource_rights x LEFT JOIN licences l ON l.id = x.licence_id WHERE x.resource_id = %s", (rid,), primary=True)
    if rights:
        r = rights[0]
        card["rights"] = {"category": r["category"], "rights_holder": r["rights_holder"], "licence_code": r["code"],
                          "licence_name": r["name"], "licence_url": r["url"]}
    card["preview"] = bool(d.manage_view and not d.read)  # staff looking at something the public cannot see
    card["status"] = d.status if d.manage_view else "PUBLISHED"
    card["can_read"] = can_read
    card["can_download"] = d.download or bool(d.manage_view and file)
    card["chapters"] = _chapters(rid) if can_read else []
    if not can_read:
        card["how_to_access"] = {
            "EXTERNAL_PROVIDER_ACCESS": "The full text is provided by the external source. Open it there with your own access.",
            "PUBLIC_METADATA_ONLY": "This is a catalogue record. GranthSetu does not serve its full text.",
            "REGISTERED_USERS": "Sign in to read this resource.",
            "INSTITUTION_ONLY": "Available to members of the licensed institution.",
            "GROUP_RESTRICTED": "Available to members of an authorised group.",
            "INDIVIDUAL_ENTITLEMENT": "Available to individually authorised readers.",
        }.get(d.policy, "You do not currently have access to the full text.")
    if who.authenticated:
        prog = db.query("SELECT chapter_id, chunk_no, percent, updated_at FROM reading_progress WHERE user_id = %s AND resource_id = %s",
                        (who.user_id, rid), primary=True)
        card["progress"] = prog[0] if prog and can_read else None
    return card


@router.get("/resources/{rid}/chapters/{cid}")
def read_chapter(rid: int, cid: int, offset: int = 0, limit: int = 40, who: Subject = Depends(current_subject)) -> dict:
    d = gate(who, rid, "read")
    limit, offset = max(1, min(limit, 100)), max(0, offset)
    db = get_db()
    cond, params = ("chapter_id = %s", [cid]) if cid else ("1 = 1", [])
    total = db.query(f"SELECT COUNT(*) AS n FROM passages WHERE resource_id = %s AND kind = 'fulltext' AND {cond}", (rid, *params), primary=True)[0]["n"]
    if cid and not db.query("SELECT 1 FROM resource_chapters WHERE id = %s AND resource_id = %s", (cid, rid), primary=True):
        raise HTTPException(404, "Not found")
    rows = db.query(f"SELECT id, chunk_no, page_no, text FROM passages WHERE resource_id = %s AND kind = 'fulltext' AND {cond} "
                    "ORDER BY chunk_no LIMIT %s OFFSET %s", (rid, *params, limit, offset), primary=True)
    if d.staff and not d.read:
        audit.record("content.preview", who, "resource", rid, detail={"chapter": cid})
    return {"resource_id": rid, "chapter_id": cid, "total": int(total), "offset": offset, "passages": rows}


@router.get("/resources/{rid}/search")
def search_in_book(rid: int, q: str = Query(min_length=2, max_length=200), who: Subject = Depends(current_subject)) -> dict:
    gate(who, rid, "read")
    needle = "%" + q.strip().lower().replace("%", "").replace("_", " ") + "%"
    rows = get_db().query("SELECT id, chunk_no, page_no, chapter_id, text FROM passages WHERE resource_id = %s AND kind = 'fulltext' "
                          "AND LOWER(text) LIKE %s ORDER BY chunk_no LIMIT 50", (rid, needle), primary=True)
    ql = q.strip().lower()
    for r in rows:
        i = max(0, r["text"].lower().find(ql))
        r["snippet"] = ("…" if i > 80 else "") + r["text"][max(0, i - 80): i + len(ql) + 120] + "…"
        del r["text"]
    return {"query": q, "matches": rows}


@router.get("/resources/{rid}/download")
def download(rid: int, who: Subject = Depends(current_subject)) -> StreamingResponse:
    """The only way a stored file leaves the server: an authorised request, streamed, never cached by proxies.
    There are no public or signed URLs, so there is nothing that keeps working after access is lost."""
    d = gate(who, rid, "download")
    db = get_db()
    f = library._current_file(lambda s, p: db.query(s, p, primary=True), rid)
    if not f:
        raise HTTPException(404, "Not found")
    try:
        body = get_storage().iter(f["storage_key"])
        first = next(body)
    except (StorageError, StopIteration):
        raise HTTPException(503, "The file is temporarily unavailable.") from None

    def stream():
        yield first
        yield from body

    audit.record("file.download", who, "resource", rid, detail={"file_id": f["id"], "staff_preview": bool(d.staff and not d.download)})
    ext = {"application/pdf": ".pdf", "text/plain": ".txt"}[f["media_type"]]
    name = (re.sub(r"[^A-Za-z0-9._-]+", "_", f["original_name"].rsplit(".", 1)[0]).strip("_") or f"resource-{rid}")[:80] + ext
    return StreamingResponse(stream(), media_type=f["media_type"] + ("; charset=utf-8" if ext == ".txt" else ""), headers={
        "Content-Disposition": f"attachment; filename=\"{name}\"; filename*=UTF-8''{quote(name)}",
        "Content-Length": str(f["size_bytes"]), "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox"})


class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    lang: str | None = None


@router.post("/resources/{rid}/ask", dependencies=[Depends(limiter("search", "rate_search_per_min"))])
def ask_book(rid: int, body: AskIn, who: Subject = Depends(current_subject)) -> dict:
    """Study help inside one book: an answer written only from passages of this book that the caller may read,
    each sentence checked by the citation verifier. Abstains when the passages do not answer."""
    from rank_bm25 import BM25Okapi

    from . import ai_tasks
    from .llm import LLMError, get_llm
    from .text import detect_lang, index_terms
    from .verifier import verify_all

    gate(who, rid, "ai")
    if not get_llm().available:
        raise HTTPException(503, "The AI model is not available right now. You can still read and search the book.")
    rows = get_db().query("SELECT id, chunk_no, page_no, lang, text FROM passages WHERE resource_id = %s AND kind = 'fulltext' "
                          "ORDER BY chunk_no LIMIT 5000", (rid,), primary=True)
    terms = index_terms(body.question)
    if not rows or not terms:
        return {"status": "insufficient_evidence", "sentences": [], "sources": [], "note": "No passage of this book matches the question."}
    scores = BM25Okapi([index_terms(r["text"]) for r in rows]).get_scores(terms)
    top = [rows[i] for i in sorted(range(len(rows)), key=lambda i: -scores[i])[:3] if scores[i] > 0]
    if not top:
        return {"status": "insufficient_evidence", "sentences": [], "sources": [], "note": "No passage of this book matches the question."}
    target = body.lang if body.lang in ("en", "hi", "kn") else detect_lang(body.question)
    passages = [{"pid": int(r["id"]), "lang": r["lang"], "title": "", "text": r["text"]} for r in top]
    try:
        sents = ai_tasks.explain(body.question, target, passages)
    except LLMError as exc:
        raise HTTPException(502, f"The AI model failed ({str(exc)[:120]}). Nothing was generated.") from None
    checked, rate = verify_all(sents, {p["pid"]: p["text"] for p in passages})
    kept = [s for s in checked if s["verified"]]
    return {"status": "answered" if kept else "insufficient_evidence", "language": target, "sentences": checked,
            "pass_rate": round(rate, 3) if checked else None,
            "sources": [{"pid": int(r["id"]), "page_no": r["page_no"], "chunk_no": r["chunk_no"], "text": r["text"]} for r in top],
            "note": "Written only from the passages listed, not from the whole book." if kept
            else "The retrieved passages do not support an answer, so none is shown."}


# =========================== personal reading data ===================================
def _visible_titles(who: Subject, rows: list[dict]) -> list[dict]:
    """Personal lists point at resources whose access may have changed since: re-check before showing anything."""
    d = authz.decide_many(who, [r["resource_id"] for r in rows])
    meta = get_db().resources_by_ids([r["resource_id"] for r in rows])
    for r in rows:
        x = d.get(int(r["resource_id"]))
        ok = bool(x and x.discover)
        m = meta.get(int(r["resource_id"]), {})
        r["available"] = ok
        r["title"] = m.get("title") if ok else "No longer available"
        r["author"] = m.get("author") if ok else None
        r["lang"] = m.get("lang") if ok else None
        r["can_read"] = bool(x and x.read)
        if not r["can_read"] and "note" in r and r.get("chunk_no") is not None:
            pass  # the note is the user's own text; it stays theirs
    return rows


class BookmarkIn(BaseModel):
    resource_id: int
    chapter_id: int | None = None
    chunk_no: int | None = None
    note: str | None = Field(default=None, max_length=2000)


@router.get("/me/bookmarks")
def my_bookmarks(who: Subject = Depends(require_user)) -> dict:
    rows = get_db().query("SELECT id, resource_id, chapter_id, chunk_no, note, created_at FROM bookmarks WHERE user_id = %s "
                          "ORDER BY id DESC LIMIT 500", (who.user_id,), primary=True)
    return {"bookmarks": _visible_titles(who, rows)}


@router.post("/me/bookmarks", status_code=201)
def add_bookmark(body: BookmarkIn, who: Subject = Depends(require_user)) -> dict:
    gate(who, body.resource_id, "read" if body.chunk_no is not None or body.chapter_id else "discover")
    db = get_db()
    dup = db.query("SELECT id FROM bookmarks WHERE user_id = %s AND resource_id = %s AND COALESCE(chunk_no, -1) = %s AND COALESCE(chapter_id, -1) = %s",
                   (who.user_id, body.resource_id, body.chunk_no if body.chunk_no is not None else -1, body.chapter_id or -1), primary=True)
    if dup:
        db.execute("UPDATE bookmarks SET note = %s WHERE id = %s", (body.note, dup[0]["id"]))
        return {"id": dup[0]["id"]}
    return {"id": db.execute("INSERT INTO bookmarks(user_id, resource_id, chapter_id, chunk_no, note, created_at) VALUES (%s,%s,%s,%s,%s,%s) "
                             "RETURNING id", (who.user_id, body.resource_id, body.chapter_id, body.chunk_no, body.note, now_iso()))[0]["id"]}


@router.delete("/me/bookmarks/{bid}")
def delete_bookmark(bid: int, who: Subject = Depends(require_user)) -> dict:
    if not get_db().execute("DELETE FROM bookmarks WHERE id = %s AND user_id = %s RETURNING id", (bid, who.user_id)):
        raise HTTPException(404, "Not found")
    return {"ok": True}


class ListIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@router.get("/me/lists")
def my_lists(who: Subject = Depends(require_user)) -> dict:
    db = get_db()
    lists = db.query("SELECT id, name, created_at FROM reading_lists WHERE user_id = %s ORDER BY name", (who.user_id,), primary=True)
    for li in lists:
        li["items"] = _visible_titles(who, db.query("SELECT resource_id, added_at FROM reading_list_items WHERE list_id = %s "
                                                    "ORDER BY added_at DESC", (li["id"],), primary=True))
    return {"lists": lists}


@router.post("/me/lists", status_code=201)
def create_list(body: ListIn, who: Subject = Depends(require_user)) -> dict:
    db = get_db()
    name = body.name.strip()
    if db.query("SELECT 1 FROM reading_lists WHERE user_id = %s AND name = %s", (who.user_id, name), primary=True):
        raise HTTPException(409, "You already have a list with this name.")
    return {"id": db.execute("INSERT INTO reading_lists(user_id, name, created_at) VALUES (%s,%s,%s) RETURNING id",
                             (who.user_id, name, now_iso()))[0]["id"]}


def _own_list(who: Subject, lid: int) -> None:
    if not get_db().query("SELECT 1 FROM reading_lists WHERE id = %s AND user_id = %s", (lid, who.user_id), primary=True):
        raise HTTPException(404, "Not found")


@router.delete("/me/lists/{lid}")
def delete_list(lid: int, who: Subject = Depends(require_user)) -> dict:
    _own_list(who, lid)
    get_db().execute("DELETE FROM reading_lists WHERE id = %s", (lid,))
    return {"ok": True}


class ListItemIn(BaseModel):
    resource_id: int


@router.post("/me/lists/{lid}/items", status_code=201)
def add_list_item(lid: int, body: ListItemIn, who: Subject = Depends(require_user)) -> dict:
    _own_list(who, lid)
    gate(who, body.resource_id, "discover")
    get_db().execute("INSERT INTO reading_list_items(list_id, resource_id, added_at) VALUES (%s,%s,%s) "
                     "ON CONFLICT(list_id, resource_id) DO NOTHING", (lid, body.resource_id, now_iso()))
    return {"ok": True}


@router.delete("/me/lists/{lid}/items/{rid}")
def remove_list_item(lid: int, rid: int, who: Subject = Depends(require_user)) -> dict:
    _own_list(who, lid)
    get_db().execute("DELETE FROM reading_list_items WHERE list_id = %s AND resource_id = %s", (lid, rid))
    return {"ok": True}


class ProgressIn(BaseModel):
    chapter_id: int | None = None
    chunk_no: int = Field(default=0, ge=0)
    percent: int = Field(default=0, ge=0, le=100)


@router.get("/me/history")
def my_history(who: Subject = Depends(require_user)) -> dict:
    rows = get_db().query("SELECT resource_id, chapter_id, chunk_no, percent, updated_at FROM reading_progress WHERE user_id = %s "
                          "ORDER BY updated_at DESC LIMIT 200", (who.user_id,), primary=True)
    return {"history": _visible_titles(who, rows)}


@router.put("/me/progress/{rid}")
def save_progress(rid: int, body: ProgressIn, who: Subject = Depends(require_user)) -> dict:
    gate(who, rid, "read")
    get_db().execute("INSERT INTO reading_progress(user_id, resource_id, chapter_id, chunk_no, percent, updated_at) VALUES (%s,%s,%s,%s,%s,%s) "
                     "ON CONFLICT(user_id, resource_id) DO UPDATE SET chapter_id = excluded.chapter_id, chunk_no = excluded.chunk_no, "
                     "percent = excluded.percent, updated_at = excluded.updated_at",
                     (who.user_id, rid, body.chapter_id, body.chunk_no, body.percent, now_iso()))
    return {"ok": True}


@router.delete("/me/history")
def clear_history(who: Subject = Depends(require_user)) -> dict:
    get_db().execute("DELETE FROM reading_progress WHERE user_id = %s", (who.user_id,))
    return {"ok": True}


# =========================== management portal ==========================================
PORTAL = Depends(require_perm("resource.create"))  # default coarse gate; handlers add object-level checks


def portal_user(request: Request) -> Subject:
    """Anyone holding at least one management permission may enter the portal; each action is checked again."""
    s = require_user(request)
    if not (s.global_perms or s.scoped_perms):
        audit.record("authz.denied", s, "portal", None, "denied", {"path": request.url.path})
        raise HTTPException(403, "You do not have access to library management.")
    return s


@router.get("/manage/reference")
def reference(who: Subject = Depends(portal_user)) -> dict:
    db = get_db()
    return {
        "licences": db.query("SELECT code, name, url, is_open, allows_download, allows_ai FROM licences ORDER BY id", primary=True),
        "policies": [{"value": p, "label": authz.POLICY_LABELS[p]} for p in POLICIES],
        "rights_categories": list(RIGHTS_CATEGORIES), "statuses": list(STATUSES), "resource_types": list(library.RESOURCE_TYPES),
        "institutions": db.query("SELECT id, name, status FROM institutions ORDER BY name", primary=True),
        "groups": db.query("SELECT id, name, institution_id FROM user_groups ORDER BY name", primary=True),
        "limits": {"max_upload_mb": settings.max_book_upload_bytes // 1024 // 1024, "file_types": ["PDF", "plain text (UTF-8)"],
                   "max_pdf_pages": settings.max_pdf_pages},
        "malware_scanning": "not configured",
        "settings": library.all_settings(),
    }


@router.get("/manage/summary")
def summary(who: Subject = Depends(portal_user)) -> dict:
    listing = library.manage_listing(who, page_size=1)
    out = {"resources_by_status": listing["counts"], "permissions": who.public()["permissions"]}
    if who.can_anywhere("job.view"):
        out["jobs"] = jobs.listing(limit=1)["counts"]
    return out


@router.get("/manage/resources")
def manage_resources(status: str | None = None, q: str | None = Query(None, max_length=200), mine: bool = False,
                     origin: str | None = "managed", page: int = 1, page_size: int = 25, who: Subject = Depends(portal_user)) -> dict:
    return library.manage_listing(who, status=status, q=q, mine=mine, origin=origin or None, page=page, page_size=page_size)


class RightsIn(BaseModel):
    category: str | None = None
    licence: str | None = None
    rights_holder: str | None = Field(default=None, max_length=300)
    permission_basis: str | None = Field(default=None, max_length=2000)
    allow_fulltext_display: bool | None = None
    allow_download: bool | None = None
    allow_ai_processing: bool | None = None
    allow_indexing: bool | None = None
    allow_ocr: bool | None = None
    valid_until: str | None = None
    notes: str | None = Field(default=None, max_length=2000)


class ResourceIn(BaseModel):
    # Bibliographic fields only. `status`, `created_by`, verification and timestamps are not accepted from clients.
    model_config = {"extra": "forbid"}
    title: str | None = Field(default=None, max_length=500)
    subtitle: str | None = Field(default=None, max_length=500)
    authors: list[str] | None = Field(default=None, max_length=20)
    isbn: str | None = None
    publisher: str | None = None
    publication_year: int | None = None
    edition: str | None = None
    lang: str | None = None
    subject: str | None = None
    categories: list[str] | None = Field(default=None, max_length=20)
    description: str | None = Field(default=None, max_length=6000)
    kind: str | None = None
    source: str | None = None
    source_url: str | None = None
    provider_id: str | None = None
    institution_id: int | None = None
    access_policy: str | None = None
    catalogue_visibility: str | None = None
    rights: RightsIn | None = None


@router.post("/manage/resources", status_code=201)
def create_resource(body: ResourceIn, who: Subject = Depends(require_perm("resource.create"))) -> dict:
    data = body.model_dump(exclude_unset=True)
    if body.rights:
        data["rights"] = body.rights.model_dump(exclude_unset=True)
    return {"id": library.create_resource(who, data)}


@router.get("/manage/resources/{rid}")
def manage_resource(rid: int, who: Subject = Depends(portal_user)) -> dict:
    return library.manage_detail(who, rid)


@router.patch("/manage/resources/{rid}")
def update_resource(rid: int, body: ResourceIn, who: Subject = Depends(portal_user)) -> dict:
    data = body.model_dump(exclude_unset=True)
    rights, policy, vis = data.pop("rights", None), data.pop("access_policy", None), data.pop("catalogue_visibility", None)
    if data:
        library.update_resource(who, rid, data)
    if rights is not None:
        library.update_rights(who, rid, body.rights.model_dump(exclude_unset=True))
    if policy or vis:
        cur = library.manage_detail(who, rid)["resource"]
        library.set_policy(who, rid, policy or cur["access_policy"], vis)
    return library.manage_detail(who, rid)


@router.put("/manage/resources/{rid}/rights")
def put_rights(rid: int, body: RightsIn, who: Subject = Depends(portal_user)) -> dict:
    library.update_rights(who, rid, body.model_dump(exclude_unset=True))
    return library.manage_detail(who, rid)


class VerifyIn(BaseModel):
    decision: str
    notes: str | None = Field(default=None, max_length=2000)


@router.post("/manage/resources/{rid}/rights/verify")
def verify_rights(rid: int, body: VerifyIn, who: Subject = Depends(portal_user)) -> dict:
    library.verify_rights(who, rid, body.decision, body.notes)
    return library.manage_detail(who, rid)


class PolicyIn(BaseModel):
    access_policy: str
    catalogue_visibility: str | None = None


@router.put("/manage/resources/{rid}/policy")
def put_policy(rid: int, body: PolicyIn, who: Subject = Depends(portal_user)) -> dict:
    library.set_policy(who, rid, body.access_policy, body.catalogue_visibility)
    return library.manage_detail(who, rid)


class EntitlementIn(BaseModel):
    subject_type: str
    subject_id: int | None = None
    email: str | None = Field(default=None, max_length=256)  # convenience for subject_type=user
    expires_at: str | None = None
    note: str | None = Field(default=None, max_length=500)


@router.post("/manage/resources/{rid}/entitlements", status_code=201)
def grant(rid: int, body: EntitlementIn, who: Subject = Depends(portal_user)) -> dict:
    sid = body.subject_id
    if body.subject_type == "user" and not sid and body.email:
        row = get_db().query("SELECT id FROM users WHERE email = %s", (body.email.strip().lower(),), primary=True)
        if not row:
            raise HTTPException(422, "No account with this e-mail address.")
        sid = row[0]["id"]
    if not sid:
        raise HTTPException(422, "Choose who receives access.")
    return {"id": library.grant_entitlement(who, rid, body.subject_type, int(sid), body.expires_at, body.note)}


@router.delete("/manage/resources/{rid}/entitlements/{eid}")
def revoke(rid: int, eid: int, who: Subject = Depends(portal_user)) -> dict:
    library.revoke_entitlement(who, rid, eid)
    return {"ok": True}


@router.post("/manage/resources/{rid}/file", status_code=201, dependencies=[Depends(limiter("upload", "rate_upload_per_min"))])
def upload(rid: int, file: UploadFile = File(...), allow_duplicate: bool = Form(False),
           who: Subject = Depends(require_perm("file.upload"))) -> dict:
    return library.upload_file(who, rid, file.file, file.filename or "upload", allow_duplicate=allow_duplicate)


@router.delete("/manage/resources/{rid}/file")
def delete_file(rid: int, who: Subject = Depends(portal_user)) -> dict:
    library.remove_file(who, rid)
    return {"ok": True}


class TransitionIn(BaseModel):
    action: str
    reason: str | None = Field(default=None, max_length=2000)


@router.post("/manage/resources/{rid}/transition")
def transition(rid: int, body: TransitionIn, who: Subject = Depends(portal_user)) -> dict:
    return library.transition(who, rid, body.action, body.reason)


@router.post("/manage/resources/{rid}/reprocess")
def reprocess(rid: int, who: Subject = Depends(portal_user)) -> dict:
    return {"job_id": library.reprocess(who, rid)}


class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


@router.post("/manage/resources/{rid}/purge")
def purge(rid: int, body: ReasonIn, who: Subject = Depends(portal_user)) -> dict:
    library.purge_content(who, rid, body.reason)
    return {"ok": True}


# ---- processing jobs -------------------------------------------------------------------
@router.get("/manage/jobs")
def list_jobs(status: str | None = None, resource_id: int | None = None, page: int = 1, page_size: int = 30,
              who: Subject = Depends(require_perm("job.view"))) -> dict:
    out = jobs.listing(status=status, resource_id=resource_id, limit=max(1, min(page_size, 100)), offset=(max(1, page) - 1) * page_size)
    if not who.can("job.view"):  # institution-scoped staff see only their institution's jobs
        out["items"] = [j for j in out["items"] if j["institution_id"] and who.can("job.view", j["institution_id"])]
        out.pop("counts", None)
    bus = get_bus()
    out["connector_queue_depth"] = bus.queue_depth()
    return out


@router.get("/manage/jobs/{job_id}")
def get_job(job_id: str, who: Subject = Depends(require_perm("job.view"))) -> dict:
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "Not found")
    if j["resource_id"]:
        inst = get_db().query("SELECT institution_id FROM resources WHERE id = %s", (j["resource_id"],), primary=True)
        if not who.can("job.view", inst[0]["institution_id"] if inst else None):
            raise HTTPException(404, "Not found")
    return j


@router.post("/manage/index/rebuild")
def rebuild_index(who: Subject = Depends(require_perm("job.manage"))) -> dict:
    """Throw away the derived BM25/FAISS indexes on every API replica and rebuild them from PostgreSQL."""
    if not who.can("job.manage"):
        raise HTTPException(403, "You do not have permission to do this.")
    db = get_db()
    version = db.bump_index_version("manual rebuild")
    audit.record("index.rebuild", who, "index", version)
    get_bus().publish({"type": "index_updated", "version": version, "added": 0})
    from .retrieval import get_index

    get_index().ensure_fresh()
    return {"index_version": version, "index": get_index().info()}


@router.get("/manage/audit")
def audit_log(action: str | None = None, target_type: str | None = None, target_id: str | None = None,
              actor_id: int | None = None, outcome: str | None = None, page: int = 1, page_size: int = 50,
              who: Subject = Depends(require_perm("audit.view"))) -> dict:
    if not who.can("audit.view"):
        raise HTTPException(403, "You do not have permission to do this.")
    size = max(1, min(page_size, 200))
    return audit.search(action, target_type, target_id, actor_id, outcome, limit=size, offset=(max(1, page) - 1) * size)


# ---- users and roles (platform administrators) ---------------------------------------------
def _admin_count() -> int:
    return int(get_db().query(
        "SELECT COUNT(DISTINCT ur.user_id) AS n FROM user_roles ur JOIN roles r ON r.id = ur.role_id JOIN users u ON u.id = ur.user_id "
        "WHERE r.name = 'platform_admin' AND ur.institution_id = 0 AND u.status = 'active'", primary=True)[0]["n"])


def _user_view(uid: int) -> dict:
    db = get_db()
    rows = db.query("SELECT id, email, display_name, status, created_at, last_login_at, locked_until FROM users WHERE id = %s", (uid,), primary=True)
    if not rows:
        raise HTTPException(404, "Not found")
    u = rows[0]
    u["roles"] = db.query("SELECT r.name AS role, ur.institution_id, i.name AS institution, ur.granted_at FROM user_roles ur "
                          "JOIN roles r ON r.id = ur.role_id LEFT JOIN institutions i ON i.id = ur.institution_id "
                          "WHERE ur.user_id = %s ORDER BY r.name", (uid,), primary=True)
    u["institutions"] = db.query("SELECT m.institution_id, i.name, m.member_type, m.expires_at FROM institution_memberships m "
                                 "JOIN institutions i ON i.id = m.institution_id WHERE m.user_id = %s", (uid,), primary=True)
    u["groups"] = db.query("SELECT g.id, g.name FROM group_memberships m JOIN user_groups g ON g.id = m.group_id WHERE m.user_id = %s",
                           (uid,), primary=True)
    return u


@router.get("/manage/users")
def list_users(q: str | None = Query(None, max_length=200), page: int = 1, page_size: int = 30,
               who: Subject = Depends(require_perm("user.manage"))) -> dict:
    db = get_db()
    cond, params = "", []
    if q and q.strip():
        cond = " WHERE LOWER(email) LIKE %s OR LOWER(display_name) LIKE %s"
        params = ["%" + q.strip().lower().replace("%", "") + "%"] * 2
    size = max(1, min(page_size, 100))
    total = db.query(f"SELECT COUNT(*) AS n FROM users{cond}", params, primary=True)[0]["n"]
    ids = db.query(f"SELECT id FROM users{cond} ORDER BY id DESC LIMIT %s OFFSET %s", (*params, size, (max(1, page) - 1) * size), primary=True)
    return {"total": int(total), "items": [_user_view(r["id"]) for r in ids],
            "roles": [{"name": r["name"], "description": r["description"], "permissions": json.loads(r["permissions"])}
                      for r in db.query("SELECT name, description, permissions FROM roles ORDER BY id", primary=True)]}


@router.get("/manage/lookup/users")
def lookup_users(q: str = Query(min_length=2, max_length=200), who: Subject = Depends(portal_user)) -> dict:
    """Find an account to grant access or membership to. Needs a permission that can grant something."""
    if not (who.can_anywhere("policy.manage") or who.can_anywhere("institution.manage") or who.can("user.manage")):
        raise HTTPException(403, "You do not have permission to do this.")
    v = "%" + q.strip().lower().replace("%", "") + "%"
    return {"users": get_db().query("SELECT id, email, display_name FROM users WHERE status = 'active' AND (LOWER(email) LIKE %s "
                                    "OR LOWER(display_name) LIKE %s) ORDER BY email LIMIT 10", (v, v), primary=True)}


class NewUserIn(BaseModel):
    email: str = Field(max_length=256)
    password: str = Field(max_length=256)
    display_name: str = Field(default="", max_length=120)
    role: str = "reader"


@router.post("/manage/users", status_code=201)
def create_user(body: NewUserIn, who: Subject = Depends(require_perm("user.manage"))) -> dict:
    uid = auth.create_user(body.email, body.password, body.display_name, roles=(body.role,), granted_by=who.user_id)
    audit.record("user.create", who, "user", uid, detail={"role": body.role})
    return _user_view(uid)


class UserPatch(BaseModel):
    status: str | None = None
    display_name: str | None = Field(default=None, max_length=120)


@router.patch("/manage/users/{uid}")
def patch_user(uid: int, body: UserPatch, who: Subject = Depends(require_perm("user.manage"))) -> dict:
    db = get_db()
    _user_view(uid)
    if body.status is not None:
        if body.status not in ("active", "disabled"):
            raise HTTPException(422, "Status must be active or disabled.")
        if body.status == "disabled":
            if uid == who.user_id:
                raise HTTPException(409, "You cannot disable your own account.")
            with db.tx() as tx:
                tx.execute("UPDATE users SET status = 'disabled', updated_at = %s WHERE id = %s", (now_iso(), uid))
                auth.revoke_all_sessions(uid, tx)  # takes effect on the very next request
                audit.record("user.disable", who, "user", uid, tx=tx)
                db.bump_access_epoch(tx)
        else:
            db.execute("UPDATE users SET status = 'active', failed_logins = 0, locked_until = NULL, updated_at = %s WHERE id = %s", (now_iso(), uid))
            audit.record("user.enable", who, "user", uid)
    if body.display_name:
        db.execute("UPDATE users SET display_name = %s, updated_at = %s WHERE id = %s", (body.display_name.strip(), now_iso(), uid))
    return _user_view(uid)


class RoleIn(BaseModel):
    role: str
    institution_id: int | None = None


@router.post("/manage/users/{uid}/roles", status_code=201)
def assign_role(uid: int, body: RoleIn, who: Subject = Depends(require_perm("role.assign"))) -> dict:
    db = get_db()
    _user_view(uid)
    role = db.query("SELECT id FROM roles WHERE name = %s", (body.role,), primary=True)
    if not role:
        raise HTTPException(422, "Unknown role.")
    inst = int(body.institution_id or 0)
    if inst and not db.query("SELECT 1 FROM institutions WHERE id = %s", (inst,), primary=True):
        raise HTTPException(422, "Unknown institution.")
    if body.role == "platform_admin" and inst:
        raise HTTPException(422, "Platform administrator cannot be scoped to an institution.")
    with db.tx() as tx:
        tx.execute("INSERT INTO user_roles(user_id, role_id, institution_id, granted_by, granted_at) VALUES (%s,%s,%s,%s,%s) "
                   "ON CONFLICT(user_id, role_id, institution_id) DO NOTHING", (uid, role[0]["id"], inst, who.user_id, now_iso()))
        audit.record("role.assign", who, "user", uid, detail={"role": body.role, "institution_id": inst or None}, tx=tx)
        db.bump_access_epoch(tx)
    return _user_view(uid)


@router.delete("/manage/users/{uid}/roles")
def revoke_role(uid: int, role: str, institution_id: int = 0, who: Subject = Depends(require_perm("role.assign"))) -> dict:
    db = get_db()
    if role == "platform_admin" and _admin_count() <= 1 and db.query(
            "SELECT 1 FROM user_roles ur JOIN roles r ON r.id = ur.role_id WHERE ur.user_id = %s AND r.name = 'platform_admin'", (uid,), primary=True):
        raise HTTPException(409, "This is the last platform administrator. Assign another one first.")
    with db.tx() as tx:
        gone = tx.execute("DELETE FROM user_roles WHERE user_id = %s AND institution_id = %s AND role_id = (SELECT id FROM roles WHERE name = %s) "
                          "RETURNING user_id", (uid, institution_id, role))
        if not gone:
            raise HTTPException(404, "Not found")
        audit.record("role.revoke", who, "user", uid, detail={"role": role, "institution_id": institution_id or None}, tx=tx)
        db.bump_access_epoch(tx)
    return _user_view(uid)


@router.post("/manage/users/{uid}/revoke-sessions")
def revoke_sessions(uid: int, who: Subject = Depends(require_perm("user.manage"))) -> dict:
    _user_view(uid)
    auth.revoke_all_sessions(uid)
    audit.record("user.sessions_revoked", who, "user", uid)
    return {"ok": True}


# ---- institutions, memberships, groups ------------------------------------------------------
def _inst_gate(who: Subject, iid: int) -> None:
    if not get_db().query("SELECT 1 FROM institutions WHERE id = %s", (iid,), primary=True) or not who.can("institution.manage", iid):
        raise HTTPException(404, "Not found")


class NameIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    description: str = Field(default="", max_length=500)


@router.get("/manage/institutions")
def institutions(who: Subject = Depends(require_perm("institution.manage"))) -> dict:
    rows = get_db().query("SELECT i.id, i.name, i.status, i.created_at, "
                          "(SELECT COUNT(*) FROM institution_memberships m WHERE m.institution_id = i.id) AS members, "
                          "(SELECT COUNT(*) FROM user_groups g WHERE g.institution_id = i.id) AS user_groups "
                          "FROM institutions i ORDER BY i.name", primary=True)
    return {"institutions": [r for r in rows if who.can("institution.manage", r["id"])], "can_create": who.can("institution.manage")}


@router.post("/manage/institutions", status_code=201)
def create_institution(body: NameIn, who: Subject = Depends(require_perm("institution.manage"))) -> dict:
    if not who.can("institution.manage"):
        raise HTTPException(403, "Only librarians and administrators can create institutions.")
    db = get_db()
    if db.query("SELECT 1 FROM institutions WHERE LOWER(name) = %s", (body.name.strip().lower(),), primary=True):
        raise HTTPException(409, "An institution with this name already exists.")
    iid = db.execute("INSERT INTO institutions(name, created_at) VALUES (%s,%s) RETURNING id", (body.name.strip(), now_iso()))[0]["id"]
    audit.record("institution.create", who, "institution", iid)
    return {"id": iid}


@router.get("/manage/institutions/{iid}")
def institution(iid: int, who: Subject = Depends(require_perm("institution.manage"))) -> dict:
    _inst_gate(who, iid)
    db = get_db()
    groups = db.query("SELECT id, name, description FROM user_groups WHERE institution_id = %s ORDER BY name", (iid,), primary=True)
    for g in groups:
        g["members"] = db.query("SELECT u.id, u.email, u.display_name FROM group_memberships m JOIN users u ON u.id = m.user_id "
                                "WHERE m.group_id = %s ORDER BY u.email", (g["id"],), primary=True)
    return {"institution": db.query("SELECT id, name, status, created_at FROM institutions WHERE id = %s", (iid,), primary=True)[0],
            "members": db.query("SELECT u.id, u.email, u.display_name, m.member_type, m.created_at, m.expires_at FROM institution_memberships m "
                                "JOIN users u ON u.id = m.user_id WHERE m.institution_id = %s ORDER BY u.email", (iid,), primary=True),
            "groups": groups}


class MemberIn(BaseModel):
    email: str = Field(max_length=256)
    member_type: str = Field(default="member", max_length=40)
    expires_at: str | None = None


def _user_by_email(email: str) -> int:
    row = get_db().query("SELECT id FROM users WHERE email = %s", (email.strip().lower(),), primary=True)
    if not row:
        raise HTTPException(422, "No account with this e-mail address. The person must register first.")
    return int(row[0]["id"])


@router.post("/manage/institutions/{iid}/members", status_code=201)
def add_member(iid: int, body: MemberIn, who: Subject = Depends(require_perm("institution.manage"))) -> dict:
    _inst_gate(who, iid)
    uid = _user_by_email(body.email)
    db = get_db()
    with db.tx() as tx:
        tx.execute("INSERT INTO institution_memberships(institution_id, user_id, member_type, added_by, created_at, expires_at) "
                   "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(institution_id, user_id) DO UPDATE SET member_type = excluded.member_type, "
                   "expires_at = excluded.expires_at", (iid, uid, body.member_type, who.user_id, now_iso(), to_utc_iso(body.expires_at)))
        audit.record("membership.add", who, "institution", iid, detail={"user_id": uid, "expires_at": to_utc_iso(body.expires_at)}, tx=tx)
        db.bump_access_epoch(tx)
    return {"ok": True}


@router.delete("/manage/institutions/{iid}/members/{uid}")
def remove_member(iid: int, uid: int, who: Subject = Depends(require_perm("institution.manage"))) -> dict:
    _inst_gate(who, iid)
    db = get_db()
    with db.tx() as tx:
        tx.execute("DELETE FROM institution_memberships WHERE institution_id = %s AND user_id = %s", (iid, uid))
        tx.execute("DELETE FROM group_memberships WHERE user_id = %s AND group_id IN (SELECT id FROM user_groups WHERE institution_id = %s)", (uid, iid))
        audit.record("membership.remove", who, "institution", iid, detail={"user_id": uid}, tx=tx)
        db.bump_access_epoch(tx)
    return {"ok": True}


@router.post("/manage/institutions/{iid}/groups", status_code=201)
def create_group(iid: int, body: NameIn, who: Subject = Depends(require_perm("institution.manage"))) -> dict:
    _inst_gate(who, iid)
    db = get_db()
    if db.query("SELECT 1 FROM user_groups WHERE institution_id = %s AND name = %s", (iid, body.name.strip()), primary=True):
        raise HTTPException(409, "This institution already has a group with this name.")
    gid = db.execute("INSERT INTO user_groups(institution_id, name, description, created_at) VALUES (%s,%s,%s,%s) RETURNING id",
                     (iid, body.name.strip(), body.description.strip(), now_iso()))[0]["id"]
    audit.record("group.create", who, "group", gid, detail={"institution_id": iid})
    return {"id": gid}


def _group_gate(who: Subject, gid: int) -> int:
    row = get_db().query("SELECT institution_id FROM user_groups WHERE id = %s", (gid,), primary=True)
    if not row or not who.can("institution.manage", row[0]["institution_id"]):
        raise HTTPException(404, "Not found")
    return int(row[0]["institution_id"] or 0)


class EmailIn(BaseModel):
    email: str = Field(max_length=256)


@router.post("/manage/groups/{gid}/members", status_code=201)
def add_group_member(gid: int, body: EmailIn, who: Subject = Depends(require_perm("institution.manage"))) -> dict:
    iid = _group_gate(who, gid)
    uid = _user_by_email(body.email)
    db = get_db()
    if iid and not db.query("SELECT 1 FROM institution_memberships WHERE institution_id = %s AND user_id = %s", (iid, uid), primary=True):
        raise HTTPException(422, "Add this person to the institution before adding them to one of its groups.")
    with db.tx() as tx:
        tx.execute("INSERT INTO group_memberships(group_id, user_id, added_by, created_at) VALUES (%s,%s,%s,%s) "
                   "ON CONFLICT(group_id, user_id) DO NOTHING", (gid, uid, who.user_id, now_iso()))
        audit.record("group.member_add", who, "group", gid, detail={"user_id": uid}, tx=tx)
        db.bump_access_epoch(tx)
    return {"ok": True}


@router.delete("/manage/groups/{gid}/members/{uid}")
def remove_group_member(gid: int, uid: int, who: Subject = Depends(require_perm("institution.manage"))) -> dict:
    _group_gate(who, gid)
    db = get_db()
    with db.tx() as tx:
        tx.execute("DELETE FROM group_memberships WHERE group_id = %s AND user_id = %s", (gid, uid))
        audit.record("group.member_remove", who, "group", gid, detail={"user_id": uid}, tx=tx)
        db.bump_access_epoch(tx)
    return {"ok": True}


@router.delete("/manage/groups/{gid}")
def delete_group(gid: int, who: Subject = Depends(require_perm("institution.manage"))) -> dict:
    _group_gate(who, gid)
    db = get_db()
    with db.tx() as tx:
        tx.execute("UPDATE entitlements SET revoked_at = %s, revoked_by = %s WHERE subject_type = 'group' AND subject_id = %s AND revoked_at IS NULL",
                   (now_iso(), who.user_id, gid))
        tx.execute("DELETE FROM user_groups WHERE id = %s", (gid,))
        audit.record("group.delete", who, "group", gid, tx=tx)
        db.bump_access_epoch(tx)
    return {"ok": True}


# ---- settings -----------------------------------------------------------------------------
class SettingIn(BaseModel):
    key: str
    value: bool | str


@router.get("/manage/settings")
def get_settings(who: Subject = Depends(portal_user)) -> dict:
    return {"settings": library.all_settings(), "can_edit": who.can("settings.manage")}


@router.put("/manage/settings")
def put_setting(body: SettingIn, who: Subject = Depends(require_perm("settings.manage"))) -> dict:
    library.set_setting(who, body.key, body.value)
    return {"settings": library.all_settings()}
