"""Storage layer.

Distributed mode: PostgreSQL primary for writes + streaming replica for reads
(read/write split). The `passages` table is partitioned by language
(PARTITION BY LIST), which is how we shard the corpus inside one cluster.

Lite mode: one SQLite file, same SQL (placeholders are translated).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterable

from .config import settings

log = logging.getLogger("granthsetu.db")

def _adapt_dt(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat()


sqlite3.register_adapter(datetime, _adapt_dt)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ConcurrentChange(Exception):
    pass


class Database:
    """Tiny repository wrapper. SQL uses %s placeholders for both engines."""

    def __init__(self, url: str = "", read_url: str = "", sqlite_path: str = "") -> None:
        self.kind = "postgres" if url else "sqlite"
        self._lock = threading.RLock()
        if self.kind == "postgres":
            from psycopg_pool import ConnectionPool

            self._primary = ConnectionPool(url, min_size=1, max_size=8, open=True, timeout=15)
            self._replica = (
                ConnectionPool(read_url, min_size=1, max_size=8, open=True, timeout=15)
                if read_url and read_url != url
                else self._primary
            )
        else:
            path = sqlite_path or settings.sqlite_path
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")

    # ------------------------------------------------------------------
    @property
    def has_replica(self) -> bool:
        return self.kind == "postgres" and self._replica is not self._primary

    def init_schema(self) -> None:
        """Apply versioned migrations (see migrations.py). Retries while the primary boots."""
        from .migrations import migrate

        for attempt in range(30):
            try:
                migrate(self)
                return
            except Exception as exc:  # primary may still be booting in docker
                if attempt == 29:
                    raise
                log.warning("migration failed (%s), retry %d", exc, attempt)
                time.sleep(2)

    def _sql(self, sql: str) -> str:
        return sql if self.kind == "postgres" else sql.replace("%s", "?")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        """Write path: always the primary."""
        return self._run(sql, params, primary=True)

    def query(self, sql: str, params: Iterable[Any] = (), primary: bool = False) -> list[dict]:
        """Read path: the replica unless the caller needs read-your-writes."""
        return self._run(sql, params, primary=primary)

    def _run(self, sql: str, params: Iterable[Any], primary: bool) -> list[dict]:
        sql = self._sql(sql)
        params = tuple(params)
        if self.kind == "sqlite":
            with self._lock:
                cur = self._conn.execute(sql, params)
                rows = cur.fetchall() if cur.description else []
                return [dict(r) for r in rows]
        pool = self._primary if primary else self._replica
        from psycopg.rows import dict_row

        with pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                rows = cur.fetchall() if cur.description else []
            conn.commit()
        return [{k: (bytes(v) if isinstance(v, memoryview) else v) for k, v in r.items()} for r in rows]

    def transaction(self, statements: list[tuple]) -> list[list[dict]]:
        """Run several writes atomically on the primary.

        A statement given as (sql, params, "require") must return at least one row (use RETURNING);
        otherwise the whole transaction is rolled back and ConcurrentChange is raised. This is how
        optimistic state transitions (UPDATE ... WHERE status=<expected>) stay race-free."""
        out: list[list[dict]] = []
        if self.kind == "sqlite":
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    for st in statements:
                        cur = self._conn.execute(self._sql(st[0]), st[1])
                        rows = [dict(r) for r in cur.fetchall()] if cur.description else []
                        if len(st) > 2 and st[2] == "require" and not rows:
                            raise ConcurrentChange("a concurrent change was detected")
                        out.append(rows)
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
            return out
        from psycopg.rows import dict_row

        with self._primary.connection() as conn:
            with conn.transaction():
                with conn.cursor(row_factory=dict_row) as cur:
                    for st in statements:
                        cur.execute(st[0], st[1])
                        rows = cur.fetchall() if cur.description else []
                        if len(st) > 2 and st[2] == "require" and not rows:
                            raise ConcurrentChange("a concurrent change was detected")
                        out.append([{k: (bytes(v) if isinstance(v, memoryview) else v) for k, v in r.items()} for r in rows])
        return out

    def close(self) -> None:
        if self.kind == "postgres":
            self._primary.close()
            if self._replica is not self._primary:
                self._replica.close()

    # ---------------- meta / versioning --------------------------------
    def get_meta(self, key: str, primary: bool = False) -> str | None:
        rows = self.query("SELECT value FROM meta WHERE key=%s", (key,), primary=primary)
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(key, value) VALUES (%s, %s) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def bump_index_version(self) -> int:
        rows = self.execute(
            "INSERT INTO meta(key, value) VALUES ('index_version', '1') "
            "ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(meta.value AS INTEGER) + 1 AS TEXT) RETURNING value"
        )
        return int(rows[0]["value"])

    def index_version(self, primary: bool = False) -> int:
        v = self.get_meta("index_version", primary=primary)
        return int(v) if v else 0

    def replication_status(self) -> dict:
        if not self.has_replica:
            return {"enabled": False}
        try:
            rows = self.query(
                "SELECT pg_is_in_recovery() AS replica, "
                "CASE WHEN pg_last_wal_receive_lsn() = pg_last_wal_replay_lsn() THEN 0 "
                "ELSE COALESCE(EXTRACT(EPOCH FROM now() - pg_last_xact_replay_timestamp()), 0) END AS lag_s, "
                "pg_wal_lsn_diff(pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn()) AS lag_bytes"
            )
            r = rows[0]
            return {"enabled": True, "replica_in_recovery": bool(r["replica"]), "lag_seconds": round(float(r["lag_s"]), 2),
                    "replay_lag_bytes": int(r["lag_bytes"] or 0)}
        except Exception as exc:
            return {"enabled": True, "error": str(exc)}

    # ---------------- resources ----------------------------------------
    def upsert_resource(self, res: dict, passages: list[tuple[str, bytes | None, str | None]]) -> int:
        """Insert/replace a resource and its passages in one transaction."""
        access = res.get("access") or "open"
        restricted = access != "open"
        # Harvested records: open ones are OPEN_ACCESS; login/paid ones are external-provider
        # records whose text is only an abstract (metadata chunk). Policy/status columns are set
        # on insert only, so a later refresh never re-opens something a librarian withdrew.
        res = {**res, "access": access,
               "policy": "EXTERNAL_PROVIDER_ACCESS" if restricted else "OPEN_ACCESS",
               "rights_basis": "external_provider" if restricted else "open_licence",
               "allow_display": not restricted, "allow_ai": not restricted}
        pkind = "metadata" if restricted else "content"
        cols = ["key", "topic_key", "title", "source", "kind", "url", "lang", "licence",
                "licence_url", "attribution", "author", "subject", "access", "policy", "rights_basis",
                "allow_display", "allow_ai", "fetched_at"]
        values = tuple(res.get(c) for c in cols[:-1]) + (now_iso(),)
        insert_only = {"key", "policy", "rights_basis", "allow_display", "allow_ai"}
        upd = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in insert_only)
        sql = (
            f"INSERT INTO resources({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
            f"ON CONFLICT(key) DO UPDATE SET {upd} RETURNING id"
        )
        if self.kind == "sqlite":
            with self._lock:
                self._conn.execute("BEGIN")
                try:
                    rid = self._conn.execute(self._sql(sql), values).fetchone()[0]
                    self._conn.execute("DELETE FROM passages WHERE resource_id=?", (rid,))
                    self._conn.executemany(
                        "INSERT INTO passages(resource_id, lang, chunk_no, text, embedding, embed_model, kind) VALUES (?,?,?,?,?,?,?)",
                        [(rid, res["lang"], i, t, e, m, pkind) for i, (t, e, m) in enumerate(passages)],
                    )
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
            return int(rid)
        with self._primary.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(sql, values)
                    rid = cur.fetchone()[0]
                    cur.execute("DELETE FROM passages WHERE resource_id=%s", (rid,))
                    cur.executemany(
                        "INSERT INTO passages(resource_id, lang, chunk_no, text, embedding, embed_model, kind) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        [(rid, res["lang"], i, t, e, m, pkind) for i, (t, e, m) in enumerate(passages)],
                    )
        return int(rid)

    def all_passages(self, primary: bool = False) -> list[dict]:
        return self.query(
            # Only published resources are ever indexed. Access attributes travel with each row so
            # retrieval can pre-filter; the final decision is re-checked against the DB per request.
            "SELECT p.id, p.resource_id, p.lang, p.chunk_no, p.text, p.embedding, p.embed_model, p.kind AS pkind, "
            "p.page_no, r.title, r.key, r.topic_key, r.access, r.policy, r.catalogue, r.status, r.allow_display, "
            "r.allow_ai, r.institution_id, r.group_id FROM passages p JOIN resources r ON r.id = p.resource_id "
            "WHERE r.status = 'PUBLISHED' ORDER BY p.id",
            primary=primary,
        )

    def resources_by_ids(self, ids: list[int]) -> dict[int, dict]:
        if not ids:
            return {}
        ph = ", ".join(["%s"] * len(ids))
        rows = self.query(f"SELECT * FROM resources WHERE id IN ({ph})", ids, primary=True)  # access decisions need fresh data
        return {int(r["id"]): r for r in rows}

    def recent_resources(self, limit: int = 30, lang: str | None = None) -> list[dict]:
        if lang:
            return self.query(
                "SELECT * FROM resources WHERE lang=%s ORDER BY fetched_at DESC, id DESC LIMIT %s", (lang, limit)
            )
        return self.query("SELECT * FROM resources ORDER BY fetched_at DESC, id DESC LIMIT %s", (limit,))

    PUBLIC = "status='PUBLISHED' AND policy NOT IN ('PRIVATE', 'UNPUBLISHED') AND catalogue='discoverable'"

    def stats(self) -> dict:
        """Aggregate counts over the publicly discoverable catalogue only."""
        w = self.PUBLIC
        by_lang = self.query(f"SELECT lang, COUNT(*) AS n FROM resources WHERE {w} GROUP BY lang ORDER BY n DESC")
        by_source = self.query(f"SELECT source, COUNT(*) AS n FROM resources WHERE {w} GROUP BY source ORDER BY n DESC")
        p = self.query(f"SELECT COUNT(*) AS n FROM passages WHERE resource_id IN (SELECT id FROM resources WHERE {w})")
        by_access = self.query(f"SELECT access, COUNT(*) AS n FROM resources WHERE {w} GROUP BY access")
        return {
            "resources_by_access": {r["access"]: int(r["n"]) for r in by_access},
            "resources_by_lang": {r["lang"]: int(r["n"]) for r in by_lang},
            "resources_by_source": {r["source"]: int(r["n"]) for r in by_source},
            "passages": int(p[0]["n"]) if p else 0,
        }

    # ---------------- gaps ---------------------------------------------
    def record_gap(self, lang: str, subject: str, topic: str) -> dict:
        rows = self.execute(
            "INSERT INTO gaps(lang, subject, topic, last_seen) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT(lang, topic) DO UPDATE SET hits = gaps.hits + 1, last_seen = excluded.last_seen, "
            "subject = excluded.subject RETURNING *",
            (lang, subject[:60], topic[:120], now_iso()),
        )
        return rows[0]

    def update_gap(self, gap_id: int, **fields: Any) -> None:
        sets = ", ".join(f"{k}=%s" for k in fields)
        self.execute(f"UPDATE gaps SET {sets} WHERE id=%s", (*fields.values(), gap_id))

    def list_gaps(self, limit: int = 100) -> list[dict]:
        return self.query("SELECT * FROM gaps ORDER BY hits DESC, last_seen DESC LIMIT %s", (limit,), primary=True)

    # ---------------- evaluation ---------------------------------------
    def save_eval(self, config: dict, summary: dict, details: list) -> int:
        rows = self.execute(
            "INSERT INTO eval_runs(created_at, config, summary, details) VALUES (%s, %s, %s, %s) RETURNING id",
            (now_iso(), json.dumps(config), json.dumps(summary), json.dumps(details, ensure_ascii=False)),
        )
        return int(rows[0]["id"])

    def latest_eval(self) -> dict | None:
        rows = self.query("SELECT * FROM eval_runs ORDER BY id DESC LIMIT 1", primary=True)
        if not rows:
            return None
        r = rows[0]
        return {
            "id": r["id"],
            "created_at": r["created_at"],
            "config": json.loads(r["config"]),
            "summary": json.loads(r["summary"]),
            "details": json.loads(r["details"]),
        }


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database(settings.database_url, settings.database_read_url, settings.sqlite_path)
        _db.init_schema()
    return _db


def set_db(db: Database) -> None:  # tests
    global _db
    _db = db
