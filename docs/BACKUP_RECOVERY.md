# Backup and recovery

A complete backup has **two parts that must be taken together**:

1. the PostgreSQL database (all records, rights, policies, entitlements, users, workflow history, audit log, extracted pages and chunks);
2. the private file store (`STORAGE_DIR`, the `book-files` volume in Docker): the original uploaded files.

Search indexes (BM25, FAISS) and Redis are **derived** and are not backed up: they are rebuilt from PostgreSQL.

## Docker Compose

```bash
make backup                              # -> backups/<timestamp>/granthsetu.dump + files.tgz
make restore DIR=backups/20261009-120000 # stops api+worker, restores both parts, restarts, reindexes
```

`make backup` runs `pg_dump -Fc` inside the primary and tars the private volume through the `migrate` container. Copy the `backups/` directory off the machine (object storage, another server): a backup on the same disk is not a backup.

## Native / manual

```bash
# backup
pg_dump -h <host> -U gs -d granthsetu -Fc > granthsetu.dump
tar -C "$(dirname "$STORAGE_DIR")" -czf files.tgz "$(basename "$STORAGE_DIR")"

# restore into an empty database
createdb -h <host> -U gs granthsetu
pg_restore -h <host> -U gs -d granthsetu --no-owner granthsetu.dump
tar -C "$(dirname "$STORAGE_DIR")" -xzf files.tgz
python -m granthsetu.manage migrate      # no-op if the dump is current; applies newer migrations otherwise
python -m granthsetu.manage reindex      # API replicas rebuild their indexes
```

## What we verified

On PostgreSQL 16 (native processes), after running the acceptance workflows:

- `pg_dump` → `pg_restore` into a new database gave identical counts: 18 resources, 12 users, 18 files, 214 audit rows, schema version 2;
- the restored file tree had identical SHA-256 sums for all 18 files;
- an API started against the restored database and restored files served a book page (200) and its PDF download.

Not verified: `make backup` / `make restore` themselves (they need Docker, which was unavailable to us), point-in-time recovery, and backups of the read replica.

## Recovery scenarios

| Situation | Action |
|---|---|
| API or worker crashed | restart it. Jobs left `running` are re-queued after `JOB_STALE_AFTER_S`; undelivered outbox rows are delivered when the worker returns |
| Search results look stale | `make reindex` |
| Redis lost | restart it; queued messages are re-created from the `processing_jobs` table by the sweeper; caches refill |
| Read replica broken | remove `DATABASE_READ_URL` (reads fall back to the primary), rebuild the replica volume |
| Primary database lost | restore the latest backup (both parts), then `migrate` and `reindex` |
| A file is missing or damaged in storage | the job fails with "stored file unavailable" or "failed its checksum"; re-upload the file and retry |

Replication to a hot standby, continuous WAL archiving, monitoring and alerting are **not configured** by this repository. The compose replica is a streaming read replica for load, not a tested failover target.
