"""Central authorization service.

Every protected endpoint asks this module; nothing else decides access.

Inputs to each decision: who the user is (roles, institution and group
memberships with expiry), the resource's publication status, its access
policy, its catalogue policy, its rights flags, and any active (unexpired,
unrevoked) entitlements. All of these are read from PostgreSQL per request,
so revocations, expiries and withdrawals apply immediately.

Actions:
  discover : the catalogue record (permitted metadata) may be shown
  read     : full text, pages, chapters, content passages, snippets, AI context,
             lesson packs and summaries
  download : the original file
  ai       : content may be placed in a model prompt (read + licence allows machine processing)
  manage   : staff views of an unpublished resource
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import settings

POLICIES = {
    "OPEN_ACCESS": "Anyone may read it in GranthSetu (downloads only where the licence allows).",
    "PUBLIC_METADATA_ONLY": "The catalogue record is public; the full text is served only to explicitly entitled users.",
    "REGISTERED_USERS": "Any signed-in user may read it.",
    "INSTITUTION_ONLY": "Only current members of the named institution (or entitled users) may read it.",
    "GROUP_RESTRICTED": "Only current members of the named group (or entitled users) may read it.",
    "INDIVIDUAL_ENTITLEMENT": "Only users with an explicit, unexpired entitlement may read it.",
    "EXTERNAL_PROVIDER_ACCESS": "GranthSetu shows the permitted metadata and sends the user to the provider's own access flow.",
    "PRIVATE": "Only authorised staff (reviewers and administrators) can discover it.",
    "UNPUBLISHED": "Not available through public discovery.",
}
STATUSES = ["DRAFT", "SUBMITTED", "UNDER_REVIEW", "NEEDS_INFORMATION", "APPROVED", "REJECTED", "PROCESSING",
            "PROCESSING_FAILED", "PUBLISHED", "WITHDRAWN", "ARCHIVED"]
RIGHTS_BASES = {
    "public_domain": "Public-domain work",
    "open_licence": "Openly licensed work (e.g. CC BY, CC BY-SA)",
    "institutional_licence": "Licensed to an institution",
    "rights_holder_permission": "Written permission from the rights holder",
    "external_provider": "Accessible only through an authorised external provider",
    "catalogue_only": "Catalogue record only; no full text is hosted",
    "unverified": "Rights not yet verified",
}
CATALOGUE = ("discoverable", "private")
# Policies that gate reading on membership/entitlement; their catalogue visibility is configurable.
GATED = {"REGISTERED_USERS", "INSTITUTION_ONLY", "GROUP_RESTRICTED", "INDIVIDUAL_ENTITLEMENT"}
ALWAYS_DISCOVERABLE = {"OPEN_ACCESS", "PUBLIC_METADATA_ONLY", "EXTERNAL_PROVIDER_ACCESS"}
STAFF_ONLY = {"PRIVATE", "UNPUBLISHED"}

_LIB = {"resource.create", "resource.edit_any", "resource.view_unpublished", "submission.review", "resource.publish",
        "resource.withdraw", "policy.edit", "entitlement.manage", "jobs.manage", "audit.view"}
ROLE_PERMS: dict[str, set[str]] = {
    "reader": set(),
    "student": set(),
    "contributor": {"resource.create"},
    "librarian": _LIB | ({"rights.verify"} if settings.librarian_can_verify_rights else set()),
    "rights_manager": {"resource.view_unpublished", "rights.verify", "policy.edit", "entitlement.manage",
                       "submission.review", "resource.withdraw", "audit.view"},
    "institution_admin": {"institution.manage_own"},
    "platform_admin": _LIB | {"rights.verify", "user.manage", "institution.manage", "institution.manage_own"},
}
ROLES = list(ROLE_PERMS)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_dt(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def active(expires_at: Any, now: datetime | None = None) -> bool:
    e = as_dt(expires_at)
    return e is None or e > (now or utcnow())


@dataclass
class Principal:
    user_id: int | None = None
    email: str = ""
    display_name: str = ""
    roles: set[str] = field(default_factory=set)
    institutions: set[int] = field(default_factory=set)
    admin_of: set[int] = field(default_factory=set)
    groups: set[int] = field(default_factory=set)
    entitled: set[int] = field(default_factory=set)  # resource ids with an active entitlement
    session_id: int | None = None

    @property
    def authenticated(self) -> bool:
        return self.user_id is not None

    @property
    def perms(self) -> set[str]:
        out: set[str] = set()
        for r in self.roles:
            out |= ROLE_PERMS.get(r, set())
        return out

    def can(self, perm: str) -> bool:
        return perm in self.perms

    @property
    def is_staff(self) -> bool:
        return self.can("resource.view_unpublished")

    def fingerprint(self) -> str:
        """Cache-key component: two principals with the same fingerprint see the same results."""
        if not self.authenticated:
            return "anon"
        return f"u{self.user_id}"

    def public(self) -> dict:
        return {"id": self.user_id, "email": self.email, "display_name": self.display_name,
                "roles": sorted(self.roles), "permissions": sorted(self.perms),
                "institutions": sorted(self.institutions), "groups": sorted(self.groups)}


ANON = Principal()


def load_principal(db, user_id: int, session_id: int | None = None) -> Principal | None:
    rows = db.query("SELECT id, email, display_name, is_active FROM users WHERE id=%s", (user_id,), primary=True)
    if not rows or not bool(rows[0]["is_active"]):
        return None
    u = rows[0]
    now = utcnow()
    roles = {r["role"] for r in db.query("SELECT role FROM user_roles WHERE user_id=%s", (user_id,), primary=True)}
    insts, admin_of = set(), set()
    for m in db.query("SELECT institution_id, member_role, expires_at FROM institution_memberships WHERE user_id=%s",
                      (user_id,), primary=True):
        if active(m["expires_at"], now):
            insts.add(int(m["institution_id"]))
            if m["member_role"] == "admin":
                admin_of.add(int(m["institution_id"]))
    if "institution_admin" not in roles:  # managing members needs the role AND an admin membership
        admin_of = set()
    groups = {int(m["group_id"]) for m in db.query(
        "SELECT group_id, expires_at FROM group_memberships WHERE user_id=%s", (user_id,), primary=True)
        if active(m["expires_at"], now)}
    p = Principal(user_id=int(u["id"]), email=u["email"], display_name=u["display_name"], roles=roles,
                  institutions=insts, admin_of=admin_of, groups=groups, session_id=session_id)
    p.entitled = entitled_resources(db, p)
    return p


def entitled_resources(db, p: Principal) -> set[int]:
    if not p.authenticated:
        return set()
    clauses, params = ["(subject_type='user' AND subject_id=%s)"], [p.user_id]
    if p.groups:
        clauses.append(f"(subject_type='group' AND subject_id IN ({', '.join(['%s'] * len(p.groups))}))")
        params += sorted(p.groups)
    if p.institutions:
        clauses.append(f"(subject_type='institution' AND subject_id IN ({', '.join(['%s'] * len(p.institutions))}))")
        params += sorted(p.institutions)
    rows = db.query(f"SELECT resource_id, expires_at FROM entitlements WHERE revoked_at IS NULL AND ({' OR '.join(clauses)})",
                    params, primary=True)
    now = utcnow()
    return {int(r["resource_id"]) for r in rows if active(r["expires_at"], now)}


# ---------------------------------------------------------------------------
@dataclass
class Decision:
    allowed: bool
    reason: str
    status: int = 200  # HTTP status to use when denied: 404 hides existence, 401 asks to sign in, 403 forbids


def _eligible(p: Principal, r: dict) -> tuple[bool, str]:
    """May this principal read the full text of a PUBLISHED resource, ignoring licence flags?"""
    pol = r.get("policy") or "OPEN_ACCESS"
    rid = int(r["id"])
    if pol == "OPEN_ACCESS":
        return True, "open access"
    if pol == "EXTERNAL_PROVIDER_ACCESS":
        return False, "available only through the provider's own access flow"
    if rid in p.entitled:
        return True, "you have an active entitlement"
    if pol == "REGISTERED_USERS":
        return (True, "signed-in users may read it") if p.authenticated else (False, "sign in to read this resource")
    if pol == "INSTITUTION_ONLY":
        inst = r.get("institution_id")
        ok = inst is not None and int(inst) in p.institutions
        return (True, "institution member") if ok else (False, "only members of the owning institution may read it")
    if pol == "GROUP_RESTRICTED":
        grp = r.get("group_id")
        ok = grp is not None and int(grp) in p.groups
        return (True, "group member") if ok else (False, "only members of the owning group may read it")
    if pol == "INDIVIDUAL_ENTITLEMENT":
        return False, "requires an individual entitlement"
    if pol == "PUBLIC_METADATA_ONLY":
        return False, "only the catalogue record is public"
    return False, "not available"


def can_discover(p: Principal, r: dict) -> bool:
    if (r.get("status") or "PUBLISHED") != "PUBLISHED":
        return p.is_staff or (p.authenticated and r.get("created_by") is not None and int(r["created_by"]) == p.user_id)
    pol = r.get("policy") or "OPEN_ACCESS"
    if pol in STAFF_ONLY:
        return p.is_staff
    if pol in ALWAYS_DISCOVERABLE or p.is_staff:
        return True
    if (r.get("catalogue") or "discoverable") == "discoverable":
        return True
    return _eligible(p, r)[0]  # private catalogue: only eligible users even learn it exists


def decide(p: Principal, r: dict | None, action: str) -> Decision:
    if r is None or not can_discover(p, r):
        return Decision(False, "not found", 404)
    if action == "discover":
        return Decision(True, "discoverable")
    published = (r.get("status") or "PUBLISHED") == "PUBLISHED"
    if action == "manage":
        own = p.authenticated and r.get("created_by") is not None and int(r["created_by"]) == p.user_id
        return Decision(True, "staff") if (p.is_staff or own) else Decision(False, "forbidden", 403)
    if not published:
        # Staff review content through the management endpoints, which are audited; the public
        # reader never serves an unpublished or partially processed resource.
        return Decision(False, f"resource is {str(r.get('status')).lower()}", 403)
    ok, why = _eligible(p, r)
    if not ok:
        return Decision(False, why, 401 if (not p.authenticated and why.startswith("sign in")) else 403)
    if not bool(r.get("allow_display", True)):
        return Decision(False, "the licence does not permit displaying the full text here", 403)
    if action == "read":
        return Decision(True, why)
    if action == "ai":
        return Decision(True, why) if bool(r.get("allow_ai", True)) else Decision(
            False, "the licence does not permit machine processing", 403)
    if action == "download":
        return Decision(True, why) if bool(r.get("allow_download", False)) else Decision(
            False, "the licence does not permit downloads", 403)
    return Decision(False, "unknown action", 400)


def discover_sql(p: Principal, alias: str = "") -> tuple[str, list]:
    """SQL predicate equivalent to can_discover() for PUBLISHED catalogue listings."""
    a = f"{alias}." if alias else ""
    if p.is_staff:
        return f"{a}status='PUBLISHED'", []
    parts = [f"{a}policy IN ('OPEN_ACCESS', 'PUBLIC_METADATA_ONLY', 'EXTERNAL_PROVIDER_ACCESS')",
             f"({a}policy IN ('REGISTERED_USERS', 'INSTITUTION_ONLY', 'GROUP_RESTRICTED', 'INDIVIDUAL_ENTITLEMENT') "
             f"AND {a}catalogue='discoverable')"]
    params: list = []
    if p.authenticated:
        parts.append(f"{a}policy='REGISTERED_USERS'")
    if p.institutions:
        parts.append(f"({a}policy='INSTITUTION_ONLY' AND {a}institution_id IN ({', '.join(['%s'] * len(p.institutions))}))")
        params += sorted(p.institutions)
    if p.groups:
        parts.append(f"({a}policy='GROUP_RESTRICTED' AND {a}group_id IN ({', '.join(['%s'] * len(p.groups))}))")
        params += sorted(p.groups)
    if p.entitled:
        parts.append(f"({a}policy NOT IN ('PRIVATE', 'UNPUBLISHED') AND {a}id IN ({', '.join(['%s'] * len(p.entitled))}))")
        params += sorted(p.entitled)
    return f"{a}status='PUBLISHED' AND ({' OR '.join(parts)})", params


def access_label(p: Principal, r: dict) -> dict:
    """What the UI shows on a card: can the viewer read/download, and why not."""
    rd, dl = decide(p, r, "read"), decide(p, r, "download")
    return {"policy": r.get("policy"), "policy_text": POLICIES.get(r.get("policy") or "", ""),
            "can_read": rd.allowed, "can_download": dl.allowed, "reason": rd.reason if not rd.allowed else dl.reason,
            "needs_login": rd.status == 401,
            "external_url": r.get("url") if (r.get("policy") == "EXTERNAL_PROVIDER_ACCESS") else None}


def policy_version(db) -> int:
    v = db.get_meta("policy_version", primary=True)
    return int(v) if v else 0


def bump_policy_sql() -> tuple[str, tuple]:
    """Statement to include in the same transaction as any access-relevant change."""
    return ("INSERT INTO meta(key, value) VALUES ('policy_version', '1') ON CONFLICT(key) DO UPDATE SET "
            "value=CAST(CAST(meta.value AS INTEGER) + 1 AS TEXT)", ())
