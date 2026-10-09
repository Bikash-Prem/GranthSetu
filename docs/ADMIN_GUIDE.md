# Library administrator guide

## First start

1. Start the stack ([SETUP.md](SETUP.md)).
2. Create the first administrator from the shell (there is no default account and no default password):
   `make admin EMAIL=you@college.edu` (Docker) or `python -m granthsetu.manage create-admin --email you@college.edu`.
   The password is read from a prompt or the `GS_ADMIN_PASSWORD` environment variable and must be at least 10 characters.
3. Sign in at **Sign in** (top right). The **Manage** and **Admin** menus appear for your roles.

## Accounts and roles (Admin → Users)

- People register themselves (role `reader`) unless `REGISTRATION_OPEN=false`. Nobody can give themselves a role: roles are changed only by platform administrators.
- Roles: `reader`, `student`, `contributor`, `librarian`, `rights_manager`, `institution_admin`, `platform_admin`. A person can hold several. The permission each role carries is in `policy.py` (`ROLE_PERMS`).
- Removing a role or deactivating an account applies on the person's next request (permissions are read from the database every time). Deactivation also ends all their sessions. The last platform administrator cannot be removed, and you cannot deactivate yourself.

## Institutions and groups (Admin → Institutions / Groups)

- Create an institution (platform admin), then add members by email with an optional end date. A member marked **admin** (appointed by a platform admin) can manage that institution's members and create groups for it, if they also hold the `institution_admin` role.
- Groups (e.g. a class or a research lab) may belong to an institution.
- Membership changes apply immediately to `INSTITUTION_ONLY` and `GROUP_RESTRICTED` resources, and to entitlements granted to that institution or group.

## Daily work

| Where | What |
|---|---|
| Manage → Dashboard | counts by status, rights checks waiting, job states, undelivered outbox rows |
| Manage → Review queue | submitted resources waiting for review |
| Manage → Processing jobs | queued, running, succeeded and failed jobs with the exact error; **Retry** |
| Manage → Audit history | who did what to which record and the outcome; filter by id or action (e.g. `access.denied`) |

## Operations checklist

- Back up the database **and** the private file volume together ([BACKUP_RECOVERY.md](BACKUP_RECOVERY.md)).
- Watch `make status`: failed jobs and a growing outbox mean the worker needs attention.
- After changing the embedding model, re-embed (`/api/admin/reembed`) so vector search uses one model.
- Serve over HTTPS and set `COOKIE_SECURE=true` in production.
- Rotate `AUTOMATION_TOKEN` if it is ever exposed; it can only trigger harvesting of open sources.

## Retention and deletion

- Readers can delete their reading history and bookmarks from **My library**. Search queries are not stored with accounts. Scan-to-Learn photos are never stored.
- Withdrawn resources keep their records and history. Permanent deletion is possible only for archived resources, by a platform administrator; it removes the files and database rows but keeps the audit log entries.
- Audit logs and publication history are kept indefinitely by default; define your own retention period and prune `audit_logs` with SQL if your policy requires it.
