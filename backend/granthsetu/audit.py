"""Append-only audit log for administrative and security-relevant events.

Never record book content, passwords, tokens or search queries here: only who
did what to which record, and the outcome.
"""
from __future__ import annotations

import json
import logging

from .db import get_db
from .policy import Principal, utcnow

log = logging.getLogger("granthsetu.audit")


def audit(actor: Principal | None, action: str, target_type: str | None = None, target_id: object = None,
          outcome: str = "ok", detail: dict | None = None) -> None:
    try:
        get_db().execute(
            "INSERT INTO audit_logs(actor_id, action, target_type, target_id, outcome, detail, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (actor.user_id if actor else None, action, target_type, None if target_id is None else str(target_id),
             outcome, json.dumps(detail, default=str)[:2000] if detail else None, utcnow()))
    except Exception as exc:  # auditing must never take the request down, but must be visible
        log.error("audit write failed for %s: %s", action, exc)


def audit_sql(actor: Principal | None, action: str, target_type: str, target_id: object, outcome: str = "ok",
              detail: dict | None = None) -> tuple[str, tuple]:
    """Audit row as a statement, for inclusion in the same transaction as the change it records."""
    return ("INSERT INTO audit_logs(actor_id, action, target_type, target_id, outcome, detail, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (actor.user_id if actor else None, action, target_type, str(target_id), outcome,
             json.dumps(detail, default=str)[:2000] if detail else None, utcnow()))
