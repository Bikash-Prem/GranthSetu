"""Local accounts: scrypt password hashes, server-side sessions, CSRF guard.

Sessions are opaque random tokens; only their SHA-256 is stored, so a database
leak does not leak usable sessions. Roles and memberships are re-read from the
database on every request, so revoking a role or deactivating a user takes
effect on the next request. Browsers get an HttpOnly, SameSite=Strict cookie;
scripts may send the same token as `Authorization: Bearer <token>`.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from datetime import timedelta

from fastapi import HTTPException, Request

from .config import settings
from .db import get_db
from .policy import ANON, Principal, as_dt, load_principal, utcnow

COOKIE = "gs_session"
CSRF_HEADER = "x-granthsetu-csrf"
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
_N, _R, _P = 2 ** 14, 8, 1


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=64 * 1024 * 1024, dklen=32)
    return f"scrypt${_N}${_R}${_P}${base64.b64encode(salt).decode()}${base64.b64encode(h).decode()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, h = stored.split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                              maxmem=64 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(h))
    except Exception:
        return False


_DUMMY = None


def dummy_verify(pw: str) -> None:
    """Spend the same time when the email is unknown, so timing does not reveal accounts."""
    global _DUMMY
    _DUMMY = _DUMMY or hash_password(secrets.token_hex(8))
    verify_password(pw, _DUMMY)


def check_password_strength(pw: str, email: str = "") -> None:
    if len(pw) < 10:
        raise ValueError("Password must be at least 10 characters.")
    if len(pw) > 200:
        raise ValueError("Password is too long.")
    if email and len(email.split("@")[0]) >= 4 and email.split("@")[0].lower() in pw.lower():
        raise ValueError("Password must not contain your email name.")
    if pw.lower() in {"password123", "granthsetu", "1234567890", "qwertyuiop", "admin12345"}:
        raise ValueError("That password is too common.")


def normalise_email(email: str) -> str:
    e = (email or "").strip().lower()
    if not EMAIL_RE.match(e):
        raise ValueError("Enter a valid email address.")
    return e


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
def create_user(email: str, password: str, display_name: str, roles: list[str] | None = None,
                granted_by: int | None = None) -> int:
    db = get_db()
    email = normalise_email(email)
    check_password_strength(password, email)
    name = (display_name or "").strip()[:80] or email.split("@")[0]
    now = utcnow()
    if db.query("SELECT 1 FROM users WHERE email=%s", (email,), primary=True):
        raise ValueError("An account with this email already exists.")
    rows = db.execute("INSERT INTO users(email, display_name, password_hash, is_active, created_at, updated_at) "
                      "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id", (email, name, hash_password(password), True, now, now))
    uid = int(rows[0]["id"])
    for role in roles or ["reader"]:
        db.execute("INSERT INTO user_roles(user_id, role, granted_by, granted_at) VALUES (%s, %s, %s, %s) "
                   "ON CONFLICT DO NOTHING", (uid, role, granted_by, now))
    return uid


def login(email: str, password: str) -> tuple[str, Principal] | None:
    db = get_db()
    try:
        email = normalise_email(email)
    except ValueError:
        dummy_verify(password)
        return None
    rows = db.query("SELECT id, password_hash, is_active FROM users WHERE email=%s", (email,), primary=True)
    if not rows:
        dummy_verify(password)
        return None
    u = rows[0]
    if not verify_password(password, u["password_hash"]) or not bool(u["is_active"]):
        return None
    token = secrets.token_urlsafe(32)
    now = utcnow()
    rows = db.execute("INSERT INTO sessions(token_hash, user_id, created_at, expires_at, last_seen_at) "
                      "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                      (_hash_token(token), u["id"], now, now + timedelta(hours=settings.session_ttl_hours), now))
    db.execute("UPDATE users SET last_login_at=%s WHERE id=%s", (now, u["id"]))
    p = load_principal(db, int(u["id"]), int(rows[0]["id"]))
    return token, p  # type: ignore[return-value]


def logout(token: str) -> None:
    get_db().execute("DELETE FROM sessions WHERE token_hash=%s", (_hash_token(token),))


def revoke_sessions(user_id: int, keep_session: int | None = None) -> None:
    if keep_session:
        get_db().execute("DELETE FROM sessions WHERE user_id=%s AND id<>%s", (user_id, keep_session))
    else:
        get_db().execute("DELETE FROM sessions WHERE user_id=%s", (user_id,))


def token_from(request: Request) -> tuple[str | None, bool]:
    """Returns (token, via_cookie)."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None, False
    c = request.cookies.get(COOKIE)
    return (c, True) if c else (None, False)


def principal_from_token(token: str | None) -> Principal:
    if not token:
        return ANON
    db = get_db()
    rows = db.query("SELECT id, user_id, expires_at FROM sessions WHERE token_hash=%s", (_hash_token(token),), primary=True)
    if not rows:
        return ANON
    s = rows[0]
    if as_dt(s["expires_at"]) <= utcnow():
        db.execute("DELETE FROM sessions WHERE id=%s", (s["id"],))
        return ANON
    return load_principal(db, int(s["user_id"]), int(s["id"])) or ANON


UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def current_principal(request: Request) -> Principal:
    """FastAPI dependency: who is calling. Anonymous if there is no valid session."""
    token, via_cookie = token_from(request)
    p = principal_from_token(token)
    if p.authenticated and via_cookie and request.method in UNSAFE and request.headers.get(CSRF_HEADER) != "1":
        # A custom header cannot be added by a cross-site form or image; cross-origin fetches need a
        # CORS preflight that we do not grant. Together with SameSite=Strict this blocks CSRF.
        raise HTTPException(403, "Missing CSRF header.")
    request.state.principal = p
    return p


def require_user(request: Request) -> Principal:
    p = current_principal(request)
    if not p.authenticated:
        raise HTTPException(401, "Sign in required.")
    return p


def require(perm: str):
    def dep(request: Request) -> Principal:
        p = require_user(request)
        if not p.can(perm):
            from .audit import audit

            audit(p, "permission.denied", "permission", perm, "denied")
            raise HTTPException(403, "You do not have permission for this action.")
        return p
    return dep
