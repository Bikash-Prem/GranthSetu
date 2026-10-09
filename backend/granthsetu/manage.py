"""Operator commands.

    python -m granthsetu.manage migrate
    python -m granthsetu.manage create-admin --email you@college.edu     # password from GS_ADMIN_PASSWORD or a prompt
    python -m granthsetu.manage grant-role --email x@y.z --role librarian
    python -m granthsetu.manage reindex                                  # bump the index version; every API replica rebuilds
    python -m granthsetu.manage retry-failed                             # re-queue every failed processing job
    python -m granthsetu.manage drain-outbox                             # deliver pending outbox rows now
    python -m granthsetu.manage status
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys

from .db import get_db


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(prog="granthsetu.manage")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    a = sub.add_parser("create-admin")
    a.add_argument("--email", required=True)
    a.add_argument("--name", default="Administrator")
    g = sub.add_parser("grant-role")
    g.add_argument("--email", required=True)
    g.add_argument("--role", required=True)
    sub.add_parser("reindex")
    sub.add_parser("retry-failed")
    sub.add_parser("drain-outbox")
    sub.add_parser("status")
    args = ap.parse_args(argv)

    db = get_db()  # applies pending migrations
    if args.cmd == "migrate":
        from .migrations import applied

        print("schema versions applied:", applied(db))
        return 0
    if args.cmd == "create-admin":
        from .auth import create_user

        pw = os.getenv("GS_ADMIN_PASSWORD") or getpass.getpass("New administrator password (min 10 chars): ")
        try:
            uid = create_user(args.email, pw, args.name, roles=["reader", "platform_admin", "librarian"])
        except ValueError as exc:
            print("error:", exc, file=sys.stderr)
            return 1
        from .audit import audit

        audit(None, "user.create_admin_cli", "user", uid)
        print(f"created administrator #{uid} ({args.email})")
        return 0
    if args.cmd == "grant-role":
        from .policy import ROLES, utcnow

        if args.role not in ROLES:
            print("unknown role; choose from", ROLES, file=sys.stderr)
            return 1
        rows = db.query("SELECT id FROM users WHERE email=%s", (args.email.strip().lower(),), primary=True)
        if not rows:
            print("no such user", file=sys.stderr)
            return 1
        db.execute("INSERT INTO user_roles(user_id, role, granted_at) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                   (rows[0]["id"], args.role, utcnow()))
        print("granted", args.role)
        return 0
    if args.cmd == "reindex":
        from .library import refresh_index

        print("index version", refresh_index({"reason": "manual"}))
        return 0
    if args.cmd == "retry-failed":
        from .library import _job, _outbox
        from .policy import utcnow

        n = 0
        for j in db.query("SELECT * FROM processing_jobs WHERE status='failed' ORDER BY id", primary=True):
            r = db.query("SELECT status FROM resources WHERE id=%s", (j["resource_id"],), primary=True)
            if not r or r[0]["status"] not in ("PROCESSING_FAILED", "PUBLISHED"):
                continue
            stmts = [_job(int(j["resource_id"]), "retry", None, j["file_id"]), _outbox("process_resource", {"resource_id": j["resource_id"]})]
            if r[0]["status"] == "PROCESSING_FAILED":
                stmts.insert(0, ("UPDATE resources SET status='PROCESSING', updated_at=%s WHERE id=%s", (utcnow(), j["resource_id"])))
            db.transaction(stmts)
            n += 1
        print("re-queued", n, "resources (the worker picks them up)")
        return 0
    if args.cmd == "drain-outbox":
        from .library import dispatch_outbox

        print("dispatched", dispatch_outbox(1000))
        return 0
    if args.cmd == "status":
        q = lambda sql: db.query(sql, primary=True)  # noqa: E731
        print(json.dumps({
            "migrations": [r["version"] for r in q("SELECT version FROM schema_migrations ORDER BY version")],
            "resources_by_status": {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) AS n FROM resources GROUP BY status")},
            "jobs_by_status": {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) AS n FROM processing_jobs GROUP BY status")},
            "outbox_pending": q("SELECT COUNT(*) AS n FROM outbox WHERE dispatched_at IS NULL")[0]["n"],
            "users": q("SELECT COUNT(*) AS n FROM users")[0]["n"],
            "index_version": db.index_version(primary=True),
        }, indent=2, default=str))
        return 0
    return 1


if __name__ == "__main__":
    code = main()
    from .db import get_db as _g

    _g().close()
    sys.exit(code)
