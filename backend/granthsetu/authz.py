"""The one place that decides who may do what with a resource.

Every endpoint that returns catalogue data, text, passages, files or AI output
calls `decide()` / `decide_many()` and acts on the returned `Decision`.
Decisions are computed from PostgreSQL (the primary) on every request, so a
revoked entitlement, an expired membership or a withdrawal takes effect at
once. The search indexes and caches are never consulted for permissions.

What each access policy allows for a PUBLISHED resource
-------------------------------------------------------
policy                    discover (see the record)        read (text, passages, AI)
OPEN_ACCESS               everyone                         everyone
PUBLIC_METADATA_ONLY      everyone                         only holders of an entitlement
REGISTERED_USERS          per catalogue visibility *       any signed-in user
INSTITUTION_ONLY          per catalogue visibility *       members of an entitled institution
GROUP_RESTRICTED          per catalogue visibility *       members of an entitled group
INDIVIDUAL_ENTITLEMENT    per catalogue visibility *       users with a personal entitlement
EXTERNAL_PROVIDER_ACCESS  everyone                         nobody here: link to the provider
PRIVATE                   staff only                       staff only
UNPUBLISHED               nobody (management portal only)  nobody

* catalogue visibility: "discoverable" = basic metadata is public (policy B);
  "private" = the record is hidden from everyone who cannot read it (policy A).

On top of the policy, reading always needs rights that permit display
(`allow_fulltext_display`, verified or provider-stated, not expired).
`download` additionally needs `allow_download`; `ai` needs `allow_ai_processing`.
Entitlements count only while not revoked and not expired. A resource that is
not PUBLISHED (draft, in review, withdrawn, archived ...) is invisible to
everyone except staff working on it in the management portal.
Anything that cannot be established is denied.
"""
from __future__ import annotations

from dataclasses import dataclass

from .auth import ANONYMOUS, Subject
from .db import get_db, now_iso

ENTITLEMENT_TYPES = {
    "PUBLIC_METADATA_ONLY": ("user", "group", "institution"),
    "INSTITUTION_ONLY": ("institution",),
    "GROUP_RESTRICTED": ("group",),
    "INDIVIDUAL_ENTITLEMENT": ("user",),
}
VISIBILITY_POLICIES = ("REGISTERED_USERS", "INSTITUTION_ONLY", "GROUP_RESTRICTED", "INDIVIDUAL_ENTITLEMENT")

POLICY_LABELS = {
    "OPEN_ACCESS": "Open access",
    "PUBLIC_METADATA_ONLY": "Catalogue record only",
    "REGISTERED_USERS": "Signed-in readers",
    "INSTITUTION_ONLY": "Institution members only",
    "GROUP_RESTRICTED": "Restricted to a group",
    "INDIVIDUAL_ENTITLEMENT": "Individually authorised readers",
    "EXTERNAL_PROVIDER_ACCESS": "Available from an external provider",
    "PRIVATE": "Private",
    "UNPUBLISHED": "Unpublished",
}


@dataclass(frozen=True)
class Decision:
    resource_id: int
    exists: bool = False
    discover: bool = False   # may know the record exists and see its basic metadata
    read: bool = False       # may receive full text, chapters, passages, snippets
    download: bool = False   # may receive the original file
    ai: bool = False         # passages may be placed in an AI prompt for this user
    staff: bool = False      # access comes from a management permission, not from the policy
    manage_view: bool = False  # may see it in the management portal (any workflow state)
    reason: str = "not found"
    policy: str = ""
    status: str = ""

    def availability(self) -> str:
        """Short, safe label for the UI."""
        if self.read:
            return "readable"
        if self.policy == "EXTERNAL_PROVIDER_ACCESS":
            return "external"
        if self.policy == "PUBLIC_METADATA_ONLY":
            return "catalogue_only"
        return "restricted"


DENY = Decision(0)

_SQL = """SELECT r.id, r.status, r.access_policy, r.catalogue_visibility, r.institution_id, r.created_by,
       x.allow_fulltext_display, x.allow_download, x.allow_ai_processing, x.verification_status, x.valid_until,
       (SELECT COUNT(*) FROM resource_files f WHERE f.resource_id = r.id AND f.is_current = TRUE AND f.status = 'stored') AS files
FROM resources r LEFT JOIN resource_rights x ON x.resource_id = r.id WHERE r.id IN ({ph})"""


def _entitled(subject: Subject, ids: list[int], now: str) -> dict[int, set[str]]:
    """resource id -> entitlement subject types the caller currently satisfies."""
    out: dict[int, set[str]] = {}
    if not subject.authenticated or not ids:
        return out
    ph = ", ".join(["%s"] * len(ids))
    rows = get_db().query(
        f"SELECT resource_id, subject_type, subject_id FROM entitlements WHERE resource_id IN ({ph}) "
        "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > %s)", (*ids, now), primary=True)
    for e in rows:
        sid, t = int(e["subject_id"]), e["subject_type"]
        if (t == "user" and sid == subject.user_id) or (t == "group" and sid in subject.group_ids) \
                or (t == "institution" and sid in subject.institution_ids):
            out.setdefault(int(e["resource_id"]), set()).add(t)
    return out


def _decide_row(subject: Subject, r: dict, ent: set[str], now: str) -> Decision:
    rid, status, policy = int(r["id"]), r["status"], r["access_policy"]
    inst = r.get("institution_id")
    staff = subject.can("resource.view_all", inst)
    owner = subject.authenticated and r.get("created_by") == subject.user_id and subject.can_anywhere("resource.create")
    manage_view = bool(staff or owner)
    base = dict(resource_id=rid, exists=True, policy=policy, status=status, manage_view=manage_view)

    rights_ok = bool(r.get("allow_fulltext_display")) and r.get("verification_status") in ("verified", "provider_stated") \
        and (not r.get("valid_until") or r["valid_until"] > now)
    can_dl = bool(r.get("allow_download")) and int(r.get("files") or 0) > 0
    can_ai = bool(r.get("allow_ai_processing"))

    if status != "PUBLISHED":
        # Not part of the library. Staff and the contributor see it only through the management portal,
        # where they may preview the upload they are reviewing. It is never searchable or AI-readable.
        return Decision(**base, discover=False, read=False, download=False, ai=False, staff=manage_view,
                        reason=f"resource is {status.lower()}")

    if staff:  # librarians and reviewers may open any published record, including PRIVATE ones
        return Decision(**base, discover=True, read=True, download=int(r.get("files") or 0) > 0, ai=can_ai and rights_ok,
                        staff=True, reason="management permission")

    read = False
    if policy == "OPEN_ACCESS":
        discover, read, why = True, True, "open access"
    elif policy == "EXTERNAL_PROVIDER_ACCESS":
        discover, read, why = True, False, "full text is provided by the external provider"
    elif policy == "PUBLIC_METADATA_ONLY":
        read = bool(ent)
        discover, why = True, "entitlement" if read else "catalogue record only"
    elif policy == "REGISTERED_USERS":
        read = subject.authenticated
        discover = read or r["catalogue_visibility"] == "discoverable"
        why = "signed-in reader" if read else "sign in to read"
    elif policy in ENTITLEMENT_TYPES:
        read = bool(ent & set(ENTITLEMENT_TYPES[policy]))
        discover = read or r["catalogue_visibility"] == "discoverable"
        why = "entitlement" if read else "no current entitlement"
    else:  # PRIVATE, UNPUBLISHED, or a value this code does not know: deny
        discover, read, why = False, False, "not available"

    if read and not rights_ok:
        read, why = False, "rights do not currently permit display"
    return Decision(**base, discover=discover, read=read, download=read and can_dl, ai=read and can_ai, reason=why)


def decide_many(subject: Subject | None, resource_ids: list[int]) -> dict[int, Decision]:
    subject = subject or ANONYMOUS
    ids = sorted({int(i) for i in resource_ids})
    if not ids:
        return {}
    now = now_iso()
    out: dict[int, Decision] = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        rows = get_db().query(_SQL.format(ph=", ".join(["%s"] * len(part))), part, primary=True)
        ent = _entitled(subject, [int(r["id"]) for r in rows if r["status"] == "PUBLISHED"], now)
        for r in rows:
            out[int(r["id"])] = _decide_row(subject, r, ent.get(int(r["id"]), set()), now)
    return out


def decide(subject: Subject | None, resource_id: int) -> Decision:
    return decide_many(subject, [resource_id]).get(int(resource_id), DENY)


def discoverable_sql(subject: Subject | None) -> tuple[str, list]:
    """A WHERE fragment (on alias `r`) selecting the published records the caller may discover.
    Used for catalogue listings so that filtering and paging happen in the database. Must stay
    equivalent to `_decide_row`; tests assert that every listed row also passes `decide()`."""
    subject = subject or ANONYMOUS
    if subject.can("resource.view_all"):
        return "r.status = 'PUBLISHED'", []
    now = now_iso()
    clauses = ["r.access_policy IN ('OPEN_ACCESS', 'PUBLIC_METADATA_ONLY', 'EXTERNAL_PROVIDER_ACCESS')",
               "(r.access_policy IN ('REGISTERED_USERS', 'INSTITUTION_ONLY', 'GROUP_RESTRICTED', 'INDIVIDUAL_ENTITLEMENT') "
               "AND r.catalogue_visibility = 'discoverable')"]
    params: list = []
    if subject.authenticated:
        clauses.append("r.access_policy = 'REGISTERED_USERS'")
        subj = [("user", [subject.user_id], "INDIVIDUAL_ENTITLEMENT"),
                ("group", sorted(subject.group_ids), "GROUP_RESTRICTED"),
                ("institution", sorted(subject.institution_ids), "INSTITUTION_ONLY")]
        for stype, sids, policy in subj:
            if not sids:
                continue
            clauses.append(
                f"(r.access_policy = '{policy}' AND EXISTS (SELECT 1 FROM entitlements e WHERE e.resource_id = r.id "
                f"AND e.subject_type = '{stype}' AND e.subject_id IN ({', '.join(['%s'] * len(sids))}) "
                "AND e.revoked_at IS NULL AND (e.expires_at IS NULL OR e.expires_at > %s)))")
            params += [*sids, now]
        scoped = sorted(i for i, p in subject.scoped_perms.items() if "*" in p or "resource.view_all" in p)
        if scoped:
            clauses.append(f"r.institution_id IN ({', '.join(['%s'] * len(scoped))})")
            params += scoped
    return "r.status = 'PUBLISHED' AND (" + " OR ".join(clauses) + ")", params
