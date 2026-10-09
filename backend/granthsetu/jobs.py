"""Durable job queue backed by the `jobs` table (a transactional outbox).

A job row is inserted in the SAME transaction as the state change that needs
it (for example status -> PROCESSING), so a job can never be lost between the
database and the queue, and a state change can never be committed without its
job. Workers claim rows from the database; the Redis/in-process bus is only
used to wake them up sooner and to broadcast progress.

Guarantees
* at most one live job per `dedupe_key` (partial unique index), so retries and
  double clicks cannot fan out;
* a worker that dies leaves a `running` row with a stale heartbeat, which any
  worker re-queues after JOB_STALE_S;
* handlers are idempotent (they replace a resource's derived rows atomically),
  so at-least-once delivery is safe;
* every failure is stored in `processing_errors` and on the job; a job is
  `succeeded` only when its handler returned.
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from .config import settings
from .db import get_db, now_iso

log = logging.getLogger("granthsetu.jobs")


class PermanentJobError(RuntimeError):
    """The handler knows a retry cannot succeed (bad file, missing rights...)."""


def enqueue(tx, kind: str, resource_id: int | None = None, created_by: int | None = None, payload: dict | None = None,
            dedupe_key: str | None = None) -> tuple[str, bool]:
    """Insert a job inside the caller's transaction. Returns (job id, created?).
    If a live job with the same dedupe key exists, that job is returned instead."""
    if dedupe_key:
        live = tx.execute("SELECT id FROM jobs WHERE dedupe_key = %s AND status IN ('queued', 'running')", (dedupe_key,))
        if live:
            return live[0]["id"], False
    jid = uuid.uuid4().hex[:12]
    now = now_iso()
    tx.execute("INSERT INTO jobs(id, kind, resource_id, payload, dedupe_key, created_by, created_at, available_at, max_attempts) "
               "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
               (jid, kind, resource_id, json.dumps(payload or {}), dedupe_key, created_by, now, now, settings.job_max_attempts))
    return jid, True


def notify(job_id: str, kind: str, status: str, **extra) -> None:
    """Best-effort progress broadcast. Carries ids and states only, never titles or text."""
    try:
        from .bus import get_bus

        get_bus().publish({"type": "job", "job_id": job_id, "kind": kind, "status": status, "managed": True, **extra})
    except Exception:  # pragma: no cover
        pass


def _later(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def requeue_stale() -> int:
    """Running jobs whose worker stopped heart-beating go back to the queue (or fail for good)."""
    db = get_db()
    cutoff = _later(-settings.job_stale_s)
    n = 0
    for j in db.query("SELECT id, attempts, max_attempts, resource_id, stage FROM jobs WHERE status = 'running' "
                      "AND COALESCE(heartbeat_at, started_at) < %s", (cutoff,), primary=True):
        with db.tx() as tx:
            msg = "worker stopped responding while the job was running"
            tx.execute("INSERT INTO processing_errors(job_id, resource_id, stage, attempt, message, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                       (j["id"], j["resource_id"], j["stage"], j["attempts"], msg, now_iso()))
            if int(j["attempts"]) >= int(j["max_attempts"]):
                tx.execute("UPDATE jobs SET status = 'failed', error = %s, finished_at = %s WHERE id = %s AND status = 'running'",
                           (msg, now_iso(), j["id"]))
                _on_final_failure(tx, j["id"], msg)
            else:
                tx.execute("UPDATE jobs SET status = 'queued', worker = NULL, available_at = %s WHERE id = %s AND status = 'running'",
                           (now_iso(), j["id"]))
        n += 1
    return n


def claim(worker: str) -> dict | None:
    db = get_db()
    now = now_iso()
    pick = "SELECT id FROM jobs WHERE status = 'queued' AND available_at <= %s ORDER BY created_at, id LIMIT 1"
    if db.kind == "postgres":
        pick += " FOR UPDATE SKIP LOCKED"  # several workers never take the same row
    with db.tx() as tx:
        rows = tx.execute(
            f"UPDATE jobs SET status = 'running', worker = %s, attempts = attempts + 1, started_at = %s, heartbeat_at = %s, "
            f"error = NULL WHERE id = ({pick}) RETURNING *", (worker, now, now, now))
    if not rows:
        return None
    job = rows[0]
    job["payload"] = json.loads(job["payload"] or "{}")
    return job


def heartbeat(job_id: str, stage: str | None = None) -> None:
    get_db().execute("UPDATE jobs SET heartbeat_at = %s, stage = COALESCE(%s, stage) WHERE id = %s", (now_iso(), stage, job_id))


_final_failure_hooks: list[Callable] = []


def on_final_failure(fn: Callable) -> Callable:
    _final_failure_hooks.append(fn)
    return fn


def _on_final_failure(tx, job_id: str, message: str) -> None:
    job = tx.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))[0]
    for fn in _final_failure_hooks:
        fn(tx, job, message)


def run_one(worker: str, handlers: dict[str, Callable[[dict], dict | None]]) -> bool:
    """Claim and run one job. Returns False when the queue is empty."""
    job = claim(worker)
    if not job:
        return False
    db = get_db()
    jid, kind = job["id"], job["kind"]
    notify(jid, kind, "running", resource_id=job["resource_id"])
    try:
        handler = handlers.get(kind)
        if not handler:
            raise PermanentJobError(f"no handler for job kind {kind}")
        result = handler(job) or {}
        db.execute("UPDATE jobs SET status = 'succeeded', stage = 'done', result = %s, finished_at = %s WHERE id = %s",
                   (json.dumps(result, default=str)[:4000], now_iso(), jid))
        notify(jid, kind, "done", resource_id=job["resource_id"])
    except Exception as exc:
        permanent = isinstance(exc, PermanentJobError) or int(job["attempts"]) >= int(job["max_attempts"])
        message = str(exc)[:1000] or type(exc).__name__
        if not isinstance(exc, PermanentJobError):
            log.exception("job %s (%s) failed on attempt %s", jid, kind, job["attempts"])
        with db.tx() as tx:
            stage = tx.execute("SELECT stage FROM jobs WHERE id = %s", (jid,))[0]["stage"]
            tx.execute("INSERT INTO processing_errors(job_id, resource_id, stage, attempt, message, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
                       (jid, job["resource_id"], stage, job["attempts"], message, now_iso()))
            if permanent:
                tx.execute("UPDATE jobs SET status = 'failed', error = %s, finished_at = %s WHERE id = %s", (message, now_iso(), jid))
                _on_final_failure(tx, jid, message)
            else:  # transient: back off and try again
                tx.execute("UPDATE jobs SET status = 'queued', worker = NULL, error = %s, available_at = %s WHERE id = %s",
                           (message, _later(5 * 2 ** int(job["attempts"])), jid))
        notify(jid, kind, "failed" if permanent else "retrying", resource_id=job["resource_id"])
    return True


def worker_loop(worker: str, handlers: dict[str, Callable], stop: threading.Event) -> None:
    last_sweep = 0.0
    import time

    while not stop.is_set():
        try:
            if time.time() - last_sweep > 30:
                requeue_stale()
                last_sweep = time.time()
            if not run_one(worker, handlers):
                stop.wait(settings.job_poll_s)
        except Exception as exc:  # database briefly unavailable: keep the worker alive
            log.error("job loop error: %s", exc)
            stop.wait(5)


def get(job_id: str) -> dict | None:
    rows = get_db().query("SELECT * FROM jobs WHERE id = %s", (job_id,), primary=True)
    if not rows:
        return None
    j = rows[0]
    j["payload"] = json.loads(j["payload"] or "{}")
    j["result"] = json.loads(j["result"]) if j.get("result") else None
    j["errors"] = get_db().query("SELECT stage, attempt, message, created_at FROM processing_errors WHERE job_id = %s ORDER BY id",
                                 (job_id,), primary=True)
    return j


def listing(status: str | None = None, resource_id: int | None = None, limit: int = 50, offset: int = 0) -> dict:
    where, params = [], []
    if status:
        where.append("j.status = %s"); params.append(status)
    if resource_id:
        where.append("j.resource_id = %s"); params.append(resource_id)
    w = (" WHERE " + " AND ".join(where)) if where else ""
    db = get_db()
    total = db.query(f"SELECT COUNT(*) AS n FROM jobs j{w}", params, primary=True)[0]["n"]
    rows = db.query(f"SELECT j.id, j.kind, j.resource_id, j.status, j.stage, j.attempts, j.max_attempts, j.error, j.created_at, "
                    f"j.started_at, j.finished_at, j.worker, r.title AS resource_title, r.institution_id FROM jobs j "
                    f"LEFT JOIN resources r ON r.id = j.resource_id{w} ORDER BY j.created_at DESC, j.id DESC LIMIT %s OFFSET %s",
                    (*params, limit, offset), primary=True)
    counts = {r["status"]: int(r["n"]) for r in db.query("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status", primary=True)}
    return {"total": int(total), "items": rows, "counts": counts}
