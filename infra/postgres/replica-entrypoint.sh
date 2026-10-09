#!/bin/sh
# Streaming read replica: clone the primary with pg_basebackup on first boot,
# then start Postgres in hot-standby mode (read-only queries allowed).
set -e
export PGPASSWORD="${REPLICATION_PASSWORD:-replicator}"
if [ ! -s "$PGDATA/PG_VERSION" ]; then
  echo "replica: waiting for primary ${PRIMARY_HOST}..."
  until pg_isready -h "$PRIMARY_HOST" -p 5432 -U "${REPLICATION_USER:-replicator}" >/dev/null 2>&1; do sleep 1; done
  sleep 2
  mkdir -p "$PGDATA" && chown -R postgres:postgres "$PGDATA" && chmod 700 "$PGDATA"
  until su-exec postgres pg_basebackup -h "$PRIMARY_HOST" -p 5432 -U "${REPLICATION_USER:-replicator}" \
        -D "$PGDATA" -R -X stream -P; do
    echo "replica: basebackup failed, retrying"; rm -rf "${PGDATA:?}"/*; sleep 2
  done
fi
exec docker-entrypoint.sh postgres -c hot_standby=on
