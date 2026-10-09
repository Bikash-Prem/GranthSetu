"""Library management: registration, rights review, the submission state machine,
file uploads, entitlements, processing jobs and the transactional outbox.

Rules enforced here (never in the frontend):
- status changes only through TRANSITIONS, each with its own permission;
- a resource is published only by the worker, after processing succeeded,
  rights are verified and metadata is valid;
- every access-relevant change bumps `policy_version` and queues an index
  refresh in the SAME transaction (outbox), so it cannot be lost.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime
from typing import Any

from .audit import audit, audit_sql
from .config import settings
from .db import get_db
from .policy import (CATALOGUE, POLICIES, RIGHTS_BASES, Principal, as_dt, bump_policy_sql, decide, utcnow)

log = logging.getLogger("granthsetu.library")

LANGS = {"en", "hi", "kn", "ta", "te", "ml", "mr", "bn", "gu", "pa", "or", "ur", "sa", "other"}
TYPES = {"book", "textbook", "article", "thesis", "notes", "reference", "other"}

# field -> (max length or type, required)
FIELDS: dict[str, Any] = {
    "title": 300, "subtitle": 300, "author": 400, "isbn": 20, "publisher": 200, "pub_year": int, "edition": 60,
    "lang": "lang", "subject": 120, "categories": 300, "description": 4000, "kind": "type", "source": 120,
    "url": 1000, "provider_id": 200, "licence": 200, "licence_url": 1000, "rights_holder": 200,
    "rights_basis": "basis", "rights_notes": 2000, "policy": "policy", "catalogue": "catalogue",
    "institution_id": "fk", "group_id": "fk", "allow_display": bool, "allow_download": bool, "allow_ai": bool,
    "attribution": 400,
}
POLICY_FIELDS = {"policy", "catalogue", "institution_id", "group_id", "allow_display", "allow_download", "allow_ai"}
RIGHTS_FIELDS = {"rights_basis", "rights_holder", "licence", "licence_url", "allow_display", "allow_download", "allow_ai"}
OPEN_BASES = {"public_domain", "open_licence"}


class WorkflowError(Exception):
    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


def _clean(data: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in data.items():
        if k not in FIELDS:
            continue  # unknown and protected fields (status, created_by, ...) are ignored, never trusted
        spec = FIELDS[k]
        if v is None or (isinstance(v, str) and not v.strip() and spec not in (bool,)):
            out[k] = None
            continue
        if spec is int:
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise ValueError(f"{k} must be a number")
            if k == "pub_year" and not 0 < v <= utcnow().year + 1:
                raise ValueError("pub_year is out of range")
        elif spec is bool:
            v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
        elif spec == "fk":
            v = int(v)
        elif spec == "lang":
            v = str(v).strip().lower()
            if v not in LANGS:
                raise ValueError("unsupported language code")
        elif spec == "type":
            v = str(v).strip().lower()
            if v not in TYPES:
                raise ValueError("unknown resource type")
        elif spec == "basis":
            if v not in RIGHTS_BASES:
                raise ValueError("unknown rights basis")
        elif spec == "policy":
            if v not in POLICIES:
                raise ValueError("unknown access policy")
        elif spec == "catalogue":
            if v not in CATALOGUE:
                raise ValueError("catalogue must be 'discoverable' or 'private'")
        else:
            v = str(v).strip()
            if len(v) > spec:
                raise ValueError(f"{k} is too long (max {spec})")
            if k in ("url", "licence_url") and v and not v.startswith(("https://", "http://")):
                raise ValueError(f"{k} must be an http(s) URL")
            if k == "isbn":
                digits = v.replace("-", "").replace(" ", "")
                if not (digits[:-1].isdigit() and len(digits) in (10, 13)):
                    raise ValueError("ISBN must have 10 or 13 digits")
        out[k] = v
    return out


def publish_problems(r: dict, has_file: bool) -> list[str]:
    """Everything that blocks publication, in plain words."""
    p: list[str] = []
    if not (r.get("title") or "").strip():
        p.append("title is required")
    if not r.get("lang"):
        p.append("language is required")
    if not r.get("licence"):
        p.append("licence (or access terms) is required")
    if r.get("rights_status") != "verified":
        p.append("rights have not been verified by a rights reviewer")
    basis, pol = r.get("rights_basis"), r.get("policy")
    if basis == "unverified":
        p.append("rights basis is 'unverified'")
    if pol == "OPEN_ACCESS" and basis not in OPEN_BASES:
        p.append("OPEN_ACCESS needs a public-domain or open-licence rights basis")
    if pol == "EXTERNAL_PROVIDER_ACCESS" and not r.get("url"):
        p.append("EXTERNAL_PROVIDER_ACCESS needs the provider URL")
    if pol == "INSTITUTION_ONLY" and not r.get("institution_id"):
        p.append("INSTITUTION_ONLY needs an institution")
    if pol == "GROUP_RESTRICTED" and not r.get("group_id"):
        p.append("GROUP_RESTRICTED needs a group")
    if basis == "catalogue_only" and has_file:
        p.append("a catalogue-only record must not have a hosted file")
    if basis in ("external_provider", "catalogue_only") and pol not in ("EXTERNAL_PROVIDER_ACCESS", "PUBLIC_METADATA_ONLY", "PRIVATE", "UNPUBLISHED"):
        p.append("a catalogue-only or external-provider record cannot grant in-app reading")
    if not has_file and pol in ("OPEN_ACCESS", "REGISTERED_USERS", "INSTITUTION_ONLY", "GROUP_RESTRICTED", "INDIVIDUAL_ENTITLEMENT") \
            and basis not in ("catalogue_only", "external_provider"):
        p.append("no file uploaded: upload the full text, or register it as catalogue-only")
    return p


# ---------------------------------------------------------------------------
def get_resource(rid: int) -> dict | None:
    rows = get_db().query("SELECT * FROM resources WHERE id=%s", (rid,), primary=True)
    return rows[0] if rows else None


def current_file(rid: int) -> dict | None:
    rows = get_db().query("SELECT * FROM book_files WHERE resource_id=%s AND is_current=%s ORDER BY id DESC LIMIT 1",
                          (rid, True), primary=True)
    return rows[0] if rows else None


def _event(rid: int, frm: str | None, to: str, action: str, actor: Principal | None, reason: str | None) -> tuple[str, tuple]:
    return ("INSERT INTO publication_events(resource_id, from_status, to_status, action, actor_id, reason, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)", (rid, frm, to, action, actor.user_id if actor else None, reason, utcnow()))


def _outbox(topic: str, payload: dict) -> tuple[str, tuple]:
    return ("INSERT INTO outbox(topic, payload, created_at) VALUES (%s, %s, %s)", (topic, json.dumps(payload), utcnow()))


def _job(rid: int, kind: str, actor: Principal | None, file_id: int | None) -> tuple[str, tuple]:
    now = utcnow()
    return ("INSERT INTO processing_jobs(resource_id, kind, file_id, status, requested_by, created_at, updated_at) "
            "VALUES (%s, %s, %s, 'queued', %s, %s, %s) "
            "ON CONFLICT (resource_id) WHERE status IN ('queued', 'running') DO NOTHING",
            (rid, kind, file_id, actor.user_id if actor else None, now, now))


def require_manage(p: Principal, r: dict | None) -> dict:
    d = decide(p, r, "manage")
    if not d.allowed:
        if r is not None:
            audit(p, "resource.manage_denied", "resource", r["id"], "denied")
        raise WorkflowError("Resource not found." if d.status == 404 else "You cannot manage this resource.",
                            404 if d.status == 404 else 403)
    return r  # type: ignore[return-value]


# ---------------------------------------------------------------------------
def create_resource(p: Principal, data: dict) -> int:
    if not p.can("resource.create"):
        raise WorkflowError("You do not have permission to add resources.", 403)
    d = _clean(data)
    if not d.get("title"):
        raise ValueError("title is required")
    if not d.get("lang"):
        raise ValueError("language is required")
    if any(k in d for k in POLICY_FIELDS) and not p.can("policy.edit"):
        # contributors propose; a policy editor confirms during review
        d = {k: v for k, v in d.items() if k not in POLICY_FIELDS - {"allow_display", "allow_download", "allow_ai"}} | {"policy": "UNPUBLISHED"}
    now = utcnow()
    row = {"key": f"gs:{uuid.uuid4().hex}", "title": d.get("title"), "source": d.get("source") or "GranthSetu library",
           "kind": d.get("kind") or "book", "url": d.get("url") or "", "lang": d["lang"],
           "licence": d.get("licence") or "", "rights_basis": d.get("rights_basis") or "unverified",
           "rights_status": "unverified", "policy": d.get("policy") or "UNPUBLISHED",
           "catalogue": d.get("catalogue") or "private", "status": "DRAFT", "origin": "managed", "access": "open",
           "created_by": p.user_id, "created_at": now, "updated_at": now, "fetched_at": now.isoformat(timespec="seconds"),
           "allow_display": d.get("allow_display", True), "allow_download": d.get("allow_download", False),
           "allow_ai": d.get("allow_ai", True)}
    for k, v in d.items():
        row.setdefault(k, v)
    cols = list(row)
    db = get_db()
    rid = int(db.transaction([
        (f"INSERT INTO resources({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id", tuple(row.values())),
    ])[0][0]["id"])
    db.transaction([_event(rid, None, "DRAFT", "create", p, None),
                    audit_sql(p, "resource.create", "resource", rid, detail={"title": row["title"]})])
    return rid


def update_resource(p: Principal, rid: int, data: dict) -> dict:
    r = require_manage(p, get_resource(rid))
    staff = p.can("resource.edit_any")
    if r["status"] in ("ARCHIVED",):
        raise WorkflowError("Archived resources cannot be edited.")
    if not staff and r["status"] not in ("DRAFT", "NEEDS_INFORMATION"):
        raise WorkflowError("You can edit only drafts or resources that need information.", 403)
    d = _clean(data)
    if any(k in d for k in POLICY_FIELDS - {"allow_display", "allow_download", "allow_ai"}) and not p.can("policy.edit"):
        raise WorkflowError("Changing the access policy needs the policy editor permission.", 403)
    changes = {k: v for k, v in d.items() if r.get(k) != v}
    if not changes:
        return r
    if "lang" in changes and r["status"] == "PUBLISHED":
        raise WorkflowError("Change the language by withdrawing and re-reviewing the resource.")
    stmts: list[tuple[str, tuple]] = []
    if RIGHTS_FIELDS & changes.keys() and r["rights_status"] == "verified" and not p.can("rights.verify"):
        changes["rights_status"] = "unverified"  # changed rights must be re-verified
    changes["updated_at"] = utcnow()
    sets = ", ".join(f"{k}=%s" for k in changes)
    stmts.append((f"UPDATE resources SET {sets} WHERE id=%s", (*changes.values(), rid)))
    access_change = bool((POLICY_FIELDS | {"rights_status"}) & changes.keys())
    if r["status"] == "PUBLISHED":
        stmts.append(bump_policy_sql())
        stmts.append(_outbox("index_refresh", {"resource_id": rid, "reason": "metadata_or_policy_change"}))
        if {"title", "subtitle", "author", "description", "categories", "allow_ai"} & changes.keys():
            stmts.append(_job(rid, "reprocess", p, None))
            stmts.append(_outbox("process_resource", {"resource_id": rid}))
    stmts.append(audit_sql(p, "resource.policy_change" if access_change else "resource.update", "resource", rid,
                           detail={"fields": sorted(k for k in changes if k != "updated_at")}))
    get_db().transaction(stmts)
    return get_resource(rid)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
TRANSITIONS: dict[str, tuple[set[str], str, str | None, bool]] = {
    # action: (from statuses, to status, permission (None = owner or staff), reason required)
    "submit": ({"DRAFT", "NEEDS_INFORMATION"}, "SUBMITTED", None, False),
    "start_review": ({"SUBMITTED"}, "UNDER_REVIEW", "submission.review", False),
    "request_info": ({"SUBMITTED", "UNDER_REVIEW"}, "NEEDS_INFORMATION", "submission.review", True),
    "reject": ({"SUBMITTED", "UNDER_REVIEW"}, "REJECTED", "submission.review", True),
    "approve": ({"UNDER_REVIEW"}, "APPROVED", "submission.review", False),
    "publish": ({"APPROVED"}, "PROCESSING", "resource.publish", False),
    "retry": ({"PROCESSING_FAILED"}, "PROCESSING", "resource.publish", False),
    "withdraw": ({"PUBLISHED", "APPROVED", "PROCESSING_FAILED"}, "WITHDRAWN", "resource.withdraw", True),
    "reinstate": ({"WITHDRAWN"}, "UNDER_REVIEW", "submission.review", True),
    "revise": ({"REJECTED"}, "DRAFT", None, False),
    "archive": ({"WITHDRAWN", "REJECTED", "DRAFT"}, "ARCHIVED", "resource.withdraw", False),
}


def transition(p: Principal, rid: int, action: str, reason: str | None = None) -> dict:
    if action not in TRANSITIONS:
        raise WorkflowError("Unknown action.", 400)
    r = require_manage(p, get_resource(rid))
    frm, to, perm, need_reason = TRANSITIONS[action]
    own = r.get("created_by") is not None and int(r["created_by"]) == p.user_id
    if perm is None:
        if not (own or p.can("resource.edit_any")):
            raise WorkflowError("Only the submitter or a librarian can do this.", 403)
    elif not p.can(perm):
        audit(p, f"resource.{action}", "resource", rid, "denied")
        raise WorkflowError("You do not have permission for this action.", 403)
    if r["status"] not in frm:
        raise WorkflowError(f"Cannot {action.replace('_', ' ')} a resource that is {r['status']}.")
    reason = (reason or "").strip()[:2000] or None
    if need_reason and not reason:
        raise WorkflowError("A reason is required for this action.", 422)
    if action in ("start_review", "approve") and settings.require_separate_reviewer and own:
        raise WorkflowError("This deployment requires a different person to review your own submission.", 403)
    f = current_file(rid)
    if action == "submit":
        missing = [x for x in ("title", "lang") if not r.get(x)]
        if not r.get("rights_basis") or r.get("rights_basis") == "unverified":
            missing.append("rights basis")
        if missing:
            raise WorkflowError("Complete these before submitting: " + ", ".join(missing), 422)
    if action == "approve":
        probs = publish_problems(r, f is not None)
        if probs:
            raise WorkflowError("Cannot approve yet: " + "; ".join(probs), 422)
    if action in ("publish", "retry"):
        probs = publish_problems(r, f is not None)
        if probs:
            raise WorkflowError("Cannot publish yet: " + "; ".join(probs), 422)
    now = utcnow()
    stmts = [("UPDATE resources SET status=%s, updated_at=%s WHERE id=%s AND status=%s RETURNING id",
              (to, now, rid, r["status"]), "require"),
             _event(rid, r["status"], to, action, p, reason)]
    if action in ("approve", "reject", "request_info", "start_review", "reinstate"):
        stmts.append(("INSERT INTO book_reviews(resource_id, reviewer_id, decision, notes, created_at) VALUES (%s, %s, %s, %s, %s)",
                      (rid, p.user_id, action, reason, now)))
    if action in ("publish", "retry"):
        if action == "retry":
            stmts.append(("UPDATE processing_jobs SET status='cancelled', updated_at=%s WHERE resource_id=%s AND status='queued'",
                          (now, rid)))
        stmts.append(_job(rid, "publish", p, f["id"] if f else None))
        stmts.append(_outbox("process_resource", {"resource_id": rid}))
    if action == "withdraw" or (r["status"] == "PUBLISHED"):
        stmts.append(bump_policy_sql())
        stmts.append(_outbox("index_refresh", {"resource_id": rid, "reason": action}))
    stmts.append(audit_sql(p, f"resource.{action}", "resource", rid, detail={"from": r["status"], "to": to}))
    from .db import ConcurrentChange

    try:
        get_db().transaction(stmts)
    except ConcurrentChange:
        raise WorkflowError("The resource changed meanwhile; reload and try again.")
    return get_resource(rid)  # type: ignore[return-value]


def verify_rights(p: Principal, rid: int, decision: str, notes: str | None) -> dict:
    if not p.can("rights.verify"):
        raise WorkflowError("You do not have permission to verify rights.", 403)
    r = require_manage(p, get_resource(rid))
    if decision not in ("verified", "rejected", "unverified"):
        raise WorkflowError("decision must be verified, rejected or unverified", 400)
    if decision == "verified" and r.get("rights_basis") in (None, "unverified"):
        raise WorkflowError("Record the rights basis before verifying it.", 422)
    if settings.require_separate_reviewer and r.get("created_by") and int(r["created_by"]) == p.user_id and decision == "verified":
        raise WorkflowError("This deployment requires someone else to verify rights for your own submission.", 403)
    stmts = [("UPDATE resources SET rights_status=%s, rights_notes=COALESCE(%s, rights_notes), updated_at=%s WHERE id=%s",
              (decision, notes, utcnow(), rid)),
             ("INSERT INTO book_reviews(resource_id, reviewer_id, decision, notes, created_at) VALUES (%s, %s, %s, %s, %s)",
              (rid, p.user_id, f"rights_{decision}", notes, utcnow())),
             audit_sql(p, "resource.rights_" + decision, "resource", rid)]
    if r["status"] == "PUBLISHED":
        stmts += [bump_policy_sql(), _outbox("index_refresh", {"resource_id": rid, "reason": "rights"})]
    get_db().transaction(stmts)
    return get_resource(rid)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
def attach_file(p: Principal, rid: int, filename: str, data: bytes) -> dict:
    from .extract import safe_filename, sniff
    from .storage import get_storage, sha256

    r = require_manage(p, get_resource(rid))
    staff = p.can("resource.edit_any")
    if r["status"] in ("WITHDRAWN", "ARCHIVED", "PROCESSING") or (not staff and r["status"] not in ("DRAFT", "NEEDS_INFORMATION")):
        raise WorkflowError(f"Files cannot be uploaded while the resource is {r['status']}.")
    if r.get("rights_basis") == "catalogue_only":
        raise WorkflowError("Catalogue-only records do not host files. Change the rights basis first.", 422)
    if not data:
        raise ValueError("The file is empty.")
    if len(data) > settings.max_book_bytes:
        raise WorkflowError(f"File too large (max {settings.max_book_bytes // 1024 // 1024} MB).", 413)
    mime = sniff(data)
    digest = sha256(data)
    db = get_db()
    dup = db.query("SELECT f.resource_id FROM book_files f JOIN resources r ON r.id=f.resource_id "
                   "WHERE f.sha256=%s AND r.status<>'ARCHIVED' LIMIT 1", (digest,), primary=True)
    if dup:
        other = int(dup[0]["resource_id"])
        audit(p, "file.duplicate_rejected", "resource", rid, "denied", {"sha256": digest[:16]})
        if other == rid:
            raise WorkflowError("This exact file is already attached to this resource.")
        visible = decide(p, get_resource(other), "manage").allowed
        raise WorkflowError(f"This exact file is already in the library (resource #{other})." if visible
                            else "This exact file has already been uploaded to the library.")
    key = get_storage().put(data)
    now = utcnow()
    stmts = [("UPDATE book_files SET is_current=%s WHERE resource_id=%s", (False, rid)),
             ("INSERT INTO book_files(resource_id, storage_key, filename, mime, size_bytes, sha256, scan_status, is_current, "
              "uploaded_by, uploaded_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
              (rid, key, safe_filename(filename), mime, len(data), digest, "not_configured", True, p.user_id, now)),
             ("UPDATE resources SET updated_at=%s WHERE id=%s", (now, rid)),
             audit_sql(p, "file.upload", "resource", rid, detail={"sha256": digest[:16], "bytes": len(data), "mime": mime})]
    if r["status"] == "PUBLISHED":  # replacement: reprocess; old content stays live until the new run succeeds
        stmts += [_job(rid, "reprocess", p, None), _outbox("process_resource", {"resource_id": rid})]
    try:
        out = db.transaction(stmts)
    except Exception:
        get_storage().delete(key)  # never leave an orphan file behind a failed transaction
        raise
    return {"file_id": int(out[1][0]["id"]), "sha256": digest, "mime": mime, "size_bytes": len(data),
            "scan_status": "not_configured", "reprocessing": r["status"] == "PUBLISHED"}


def request_reprocess(p: Principal, rid: int) -> None:
    if not p.can("jobs.manage"):
        raise WorkflowError("You do not have permission to reprocess resources.", 403)
    r = require_manage(p, get_resource(rid))
    if r["status"] != "PUBLISHED":
        raise WorkflowError("Only published resources can be reprocessed; use publish or retry instead.")
    get_db().transaction([_job(rid, "reprocess", p, None), _outbox("process_resource", {"resource_id": rid}),
                          audit_sql(p, "resource.reprocess", "resource", rid)])


def purge(p: Principal, rid: int) -> None:
    """Retention: permanently delete an ARCHIVED resource and its files. The audit trail is kept."""
    from .storage import get_storage

    if not p.can("user.manage"):
        raise WorkflowError("Only platform administrators can permanently delete resources.", 403)
    r = require_manage(p, get_resource(rid))
    if r["status"] != "ARCHIVED":
        raise WorkflowError("Archive the resource before deleting it permanently.")
    keys = [f["storage_key"] for f in get_db().query("SELECT storage_key FROM book_files WHERE resource_id=%s", (rid,), primary=True)]
    db = get_db()
    db.transaction([("DELETE FROM passages WHERE resource_id=%s", (rid,)), ("DELETE FROM resources WHERE id=%s", (rid,)),
                    bump_policy_sql(), _outbox("index_refresh", {"resource_id": rid, "reason": "purge"}),
                    audit_sql(p, "resource.purge", "resource", rid, detail={"title": r["title"], "files": len(keys)})])
    for k in keys:
        get_storage().delete(k)


# ---------------------------------------------------------------------------
def grant(p: Principal, rid: int, subject_type: str, subject_id: int, expires_at: str | None, reason: str | None) -> int:
    if not p.can("entitlement.manage"):
        raise WorkflowError("You do not have permission to manage entitlements.", 403)
    require_manage(p, get_resource(rid))
    if subject_type not in ("user", "group", "institution"):
        raise WorkflowError("subject_type must be user, group or institution", 400)
    table = {"user": "users", "group": "user_groups", "institution": "institutions"}[subject_type]
    if not get_db().query(f"SELECT 1 FROM {table} WHERE id=%s", (subject_id,), primary=True):
        raise WorkflowError(f"Unknown {subject_type}.", 404)
    exp = as_dt(expires_at) if expires_at else None
    if exp and exp <= utcnow():
        raise WorkflowError("The expiry date is in the past.", 422)
    out = get_db().transaction([
        ("INSERT INTO entitlements(resource_id, subject_type, subject_id, reason, granted_by, created_at, expires_at) "
         "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id", (rid, subject_type, subject_id, reason, p.user_id, utcnow(), exp)),
        bump_policy_sql(),
        audit_sql(p, "entitlement.grant", "resource", rid, detail={"subject": f"{subject_type}:{subject_id}",
                                                                  "expires_at": exp.isoformat() if exp else None})])
    return int(out[0][0]["id"])


def revoke(p: Principal, entitlement_id: int) -> None:
    if not p.can("entitlement.manage"):
        raise WorkflowError("You do not have permission to manage entitlements.", 403)
    rows = get_db().query("SELECT * FROM entitlements WHERE id=%s", (entitlement_id,), primary=True)
    if not rows:
        raise WorkflowError("Unknown entitlement.", 404)
    e = rows[0]
    require_manage(p, get_resource(int(e["resource_id"])))
    get_db().transaction([
        ("UPDATE entitlements SET revoked_at=%s, revoked_by=%s WHERE id=%s AND revoked_at IS NULL", (utcnow(), p.user_id, entitlement_id)),
        bump_policy_sql(),
        audit_sql(p, "entitlement.revoke", "resource", e["resource_id"], detail={"entitlement": entitlement_id})])


# ---------------------------------------------------------------------------
# Worker side
def dispatch_outbox(limit: int = 50) -> int:
    """Deliver pending outbox rows. Delivery is at-least-once; handlers are idempotent."""
    from .bus import get_bus

    db = get_db()
    rows = db.query("SELECT * FROM outbox WHERE dispatched_at IS NULL ORDER BY id LIMIT %s", (limit,), primary=True)
    n = 0
    for row in rows:
        try:
            payload = json.loads(row["payload"])
            if row["topic"] == "index_refresh":
                refresh_index(payload)
            elif row["topic"] == "process_resource":
                get_bus().enqueue("process_resource", payload)
            db.execute("UPDATE outbox SET dispatched_at=%s, attempts=attempts+1 WHERE id=%s", (utcnow(), row["id"]))
            n += 1
        except Exception as exc:
            log.warning("outbox %s failed: %s", row["id"], exc)
            db.execute("UPDATE outbox SET attempts=attempts+1, last_error=%s WHERE id=%s", (str(exc)[:500], row["id"]))
    return n


def refresh_index(payload: dict | None = None) -> int:
    from .bus import get_bus

    v = get_db().bump_index_version()
    get_bus().publish({"type": "index_updated", "version": v, "added": 0, "reason": (payload or {}).get("reason")})
    return v


def sweep_jobs() -> int:
    """Recover from worker crashes: re-queue stale running jobs; re-deliver queued jobs whose message was lost."""
    from .bus import get_bus

    db = get_db()
    now = utcnow()
    n = 0
    for j in db.query("SELECT * FROM processing_jobs WHERE status IN ('queued', 'running')", primary=True):
        age = (now - (as_dt(j["updated_at"]) or now)).total_seconds()
        if j["status"] == "running" and age > settings.job_stale_after_s:
            final = int(j["attempts"]) >= int(j["max_attempts"])
            db.transaction([
                ("UPDATE processing_jobs SET status=%s, last_error=%s, updated_at=%s WHERE id=%s AND status='running'",
                 ("failed" if final else "queued", "worker stopped while processing (stale job recovered)", now, j["id"])),
                ("INSERT INTO processing_errors(job_id, resource_id, stage, message, created_at) VALUES (%s, %s, %s, %s, %s)",
                 (j["id"], j["resource_id"], j.get("stage") or "unknown", "worker stopped while processing", now))]
                + ([("UPDATE resources SET status='PROCESSING_FAILED', updated_at=%s WHERE id=%s AND status='PROCESSING'",
                     (now, j["resource_id"])), _event(int(j["resource_id"]), "PROCESSING", "PROCESSING_FAILED", "worker_crash", None,
                                                       "worker stopped while processing")] if final else [])
                + ([_outbox("process_resource", {"resource_id": int(j["resource_id"])})] if not final else []))
            n += 1
        elif j["status"] == "queued" and age > 60:
            get_bus().enqueue("process_resource", {"resource_id": int(j["resource_id"])})
            db.execute("UPDATE processing_jobs SET updated_at=%s WHERE id=%s AND status='queued'", (now, j["id"]))
            n += 1
    return n


def _metadata_text(r: dict) -> str:
    parts = [r.get("title"), r.get("subtitle"), r.get("author") and f"by {r['author']}", r.get("publisher"),
             r.get("subject"), r.get("categories"), r.get("description")]
    return ". ".join(str(x) for x in parts if x)


def process_resource(resource_id: int) -> dict:
    """Idempotent: claims the single active job for the resource; a duplicate delivery finds nothing to claim."""
    from .embeddings import get_embedder, to_bytes
    from .extract import ExtractionError, extract
    from .storage import StorageError, get_storage, sha256
    from .text import chunk

    db = get_db()
    now = utcnow()
    claimed = db.execute("UPDATE processing_jobs SET status='running', attempts=attempts+1, started_at=%s, updated_at=%s, "
                         "stage='validate' WHERE resource_id=%s AND status='queued' RETURNING *", (now, now, resource_id))
    if not claimed:
        return {"skipped": "no queued job (already processed or running elsewhere)"}
    job = claimed[0]
    jid = int(job["id"])
    stage = "validate"

    def set_stage(s: str) -> None:
        nonlocal stage
        stage = s
        db.execute("UPDATE processing_jobs SET stage=%s, updated_at=%s WHERE id=%s", (s, utcnow(), jid))

    try:
        r = get_resource(resource_id)
        if r is None or r["status"] not in ("PROCESSING", "PUBLISHED"):
            db.execute("UPDATE processing_jobs SET status='cancelled', last_error=%s, finished_at=%s, updated_at=%s WHERE id=%s",
                       (f"resource is {r['status'] if r else 'missing'}; nothing to process", utcnow(), utcnow(), jid))
            return {"cancelled": True}
        f = current_file(resource_id)
        probs = publish_problems(r, f is not None)
        if probs:
            raise ExtractionError("not publishable: " + "; ".join(probs))
        pages: list[dict] = []
        chapters: list[dict] = []
        info: dict[str, Any] = {"pages": 0, "ocr_pages": 0, "warnings": []}
        if f:
            set_stage("read_file")
            try:
                data = get_storage().get(f["storage_key"])
            except StorageError as exc:
                raise ExtractionError(f"stored file unavailable: {exc}")
            if sha256(data) != f["sha256"]:
                raise ExtractionError("stored file failed its checksum; re-upload it")
            set_stage("extract")
            out = extract(data, f["mime"], allow_ocr=bool(r["allow_ai"]))
            pages, chapters = out["pages"], out["chapters"]
            info = {"pages": len(pages), "ocr_pages": out["ocr_pages"], "warnings": out["warnings"], "paged": out["paged"]}
        set_stage("chunk")
        chunks: list[tuple[str, str, int | None, int | None]] = []  # (kind, text, page, chapter)
        meta = _metadata_text(r)
        if meta:
            chunks.append(("metadata", meta[:1500], None, None))
        if pages and bool(r["allow_ai"]):  # indexing full text is machine processing: only when the licence allows it
            for pg in pages:
                for c in chunk(pg["text"], max_chars=900):
                    chunks.append(("content", c, pg["page_no"], pg.get("chapter_no")))
        set_stage("embed")
        emb = get_embedder()
        vecs = emb.embed([f'{r["title"]}. {c[1]}' for c in chunks], kind="doc") if chunks else []
        set_stage("store")
        now = utcnow()
        stmts: list[tuple[str, tuple]] = [
            ("DELETE FROM passages WHERE resource_id=%s", (resource_id,)),
            ("DELETE FROM book_pages WHERE resource_id=%s", (resource_id,)),
            ("DELETE FROM book_chapters WHERE resource_id=%s", (resource_id,)),
        ]
        for c in chapters:
            stmts.append(("INSERT INTO book_chapters(resource_id, chapter_no, title, start_page, end_page) VALUES (%s, %s, %s, %s, %s)",
                          (resource_id, c["chapter_no"], c["title"], c["start_page"], c["end_page"])))
        for pg in pages:
            stmts.append(("INSERT INTO book_pages(resource_id, page_no, chapter_no, text, ocr) VALUES (%s, %s, %s, %s, %s)",
                          (resource_id, pg["page_no"], pg.get("chapter_no"), pg["text"], bool(pg["ocr"]))))
        for i, ((kind, text, page, chap), v) in enumerate(zip(chunks, vecs)):
            stmts.append(("INSERT INTO passages(resource_id, lang, chunk_no, text, embedding, embed_model, kind, page_no, chapter_no) "
                          "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                          (resource_id, r["lang"], i, text, to_bytes(v), emb.name, kind, page, chap)))
        info.update({"chunks": len(chunks), "content_chunks": sum(1 for c in chunks if c[0] == "content"),
                     "chapters": len(chapters), "embedder": emb.name})
        stmts.append(("UPDATE processing_jobs SET status='succeeded', stage='done', result=%s, last_error=NULL, finished_at=%s, "
                      "updated_at=%s WHERE id=%s", (json.dumps(info), now, now, jid)))
        if r["status"] == "PROCESSING":
            stmts += [("UPDATE resources SET status='PUBLISHED', published_at=%s, updated_at=%s WHERE id=%s AND status='PROCESSING'",
                       (now, now, resource_id)),
                      _event(resource_id, "PROCESSING", "PUBLISHED", "processed", None, None)]
        stmts += [bump_policy_sql(), _outbox("index_refresh", {"resource_id": resource_id, "reason": "processed"}),
                  audit_sql(None, "resource.processed", "resource", resource_id, detail={"job": jid, **info})]
        db.transaction(stmts)
        dispatch_outbox()
        return info
    except Exception as exc:
        transient = not isinstance(exc, ExtractionError)
        msg = str(exc)[:1000] if not transient else f"{type(exc).__name__}: {str(exc)[:900]}"
        log.warning("processing resource %s failed at %s: %s", resource_id, stage, msg)
        attempts = int(job["attempts"])
        retry = transient and attempts < int(job["max_attempts"])
        now = utcnow()
        stmts = [("UPDATE processing_jobs SET status=%s, last_error=%s, finished_at=%s, updated_at=%s WHERE id=%s",
                  ("queued" if retry else "failed", msg, None if retry else now, now, jid)),
                 ("INSERT INTO processing_errors(job_id, resource_id, stage, message, created_at) VALUES (%s, %s, %s, %s, %s)",
                  (jid, resource_id, stage, msg, now))]
        if retry:
            stmts.append(_outbox("process_resource", {"resource_id": resource_id}))
        else:
            stmts += [("UPDATE resources SET status='PROCESSING_FAILED', updated_at=%s WHERE id=%s AND status='PROCESSING'",
                       (now, resource_id)),
                      _event(resource_id, "PROCESSING", "PROCESSING_FAILED", "processing_failed", None, msg[:300]),
                      audit_sql(None, "resource.processing_failed", "resource", resource_id, "failed", {"job": jid, "stage": stage})]
        db.transaction(stmts)
        if retry:
            time.sleep(min(2 ** attempts, 30))
            dispatch_outbox()
        return {"failed": msg, "will_retry": retry}


def retry_job(p: Principal, job_id: int) -> None:
    if not p.can("jobs.manage"):
        raise WorkflowError("You do not have permission to retry jobs.", 403)
    rows = get_db().query("SELECT * FROM processing_jobs WHERE id=%s", (job_id,), primary=True)
    if not rows:
        raise WorkflowError("Unknown job.", 404)
    j = rows[0]
    if j["status"] != "failed":
        raise WorkflowError(f"Only failed jobs can be retried (this one is {j['status']}).")
    r = get_resource(int(j["resource_id"]))
    if r and r["status"] == "PROCESSING_FAILED":
        transition(p, int(j["resource_id"]), "retry")
        return
    if r and r["status"] == "PUBLISHED":
        request_reprocess(p, int(j["resource_id"]))
        return
    raise WorkflowError(f"The resource is {r['status'] if r else 'missing'}; nothing to retry.")


def ts(v: Any) -> str | None:
    d = as_dt(v)
    return d.isoformat() if isinstance(d, datetime) else None
