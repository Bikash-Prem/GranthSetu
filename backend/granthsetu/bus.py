"""Cache, rate limiter, message queue and pub/sub.

Distributed mode uses Redis:
  * cache        -> GET/SETEX with TTL (search results, LLM outputs, query embeddings)
  * rate limiter -> fixed-window counters per client and route (INCR + EXPIRE)
  * queue        -> Redis Streams consumer group (at-least-once, retries, dead letter)
  * pub/sub      -> channel "gs:events" fanned out to every API replica (SSE + index reload)

Lite mode keeps the same interface in-process so the app runs with zero infra.
"""
from __future__ import annotations

import json
import logging
import queue as pyqueue
import threading
import time
import uuid
from typing import Any, Callable, Iterator

from .config import settings

log = logging.getLogger("granthsetu.bus")

STREAM = "gs:jobs"
GROUP = "workers"
DEAD = "gs:jobs:dead"
CHANNEL = "gs:events"
MAX_DELIVERIES = 3


class Bus:
    def __init__(self, redis_url: str = "") -> None:
        self.redis = None
        if redis_url:
            import redis

            self.redis = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=10)
            for attempt in range(20):
                try:
                    self.redis.ping()
                    break
                except Exception as exc:
                    log.warning("redis not ready (%s), retry %d", exc, attempt)
                    time.sleep(1.5)
            try:
                self.redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
            except Exception:
                pass  # group already exists
        self._mem: dict[str, tuple[float, str]] = {}
        self._mem_lock = threading.Lock()
        self._q: "pyqueue.Queue[dict]" = pyqueue.Queue()
        self._subs: list["pyqueue.Queue[dict]"] = []
        self.stats = {"cache_hits": 0, "cache_misses": 0, "rate_limited": 0}

    @property
    def kind(self) -> str:
        return "redis" if self.redis else "memory"

    # ---------------- cache --------------------------------------------
    def cache_get(self, key: str) -> Any | None:
        raw = None
        if self.redis:
            try:
                raw = self.redis.get("gs:c:" + key)
            except Exception as exc:  # cache is best-effort, never fatal
                log.warning("cache get failed: %s", exc)
        else:
            with self._mem_lock:
                item = self._mem.get(key)
                if item and item[0] > time.time():
                    raw = item[1]
        if raw is None:
            self.stats["cache_misses"] += 1
            return None
        self.stats["cache_hits"] += 1
        return json.loads(raw)

    def cache_set(self, key: str, value: Any, ttl: int) -> None:
        raw = json.dumps(value, ensure_ascii=False)
        if self.redis:
            try:
                self.redis.setex("gs:c:" + key, ttl, raw)
            except Exception as exc:
                log.warning("cache set failed: %s", exc)
            return
        with self._mem_lock:
            if len(self._mem) > 5000:
                self._mem.clear()
            self._mem[key] = (time.time() + ttl, raw)

    # ---------------- rate limiting ------------------------------------
    def allow(self, client: str, route: str, limit_per_min: int) -> tuple[bool, int]:
        """Fixed-window limiter. Returns (allowed, seconds_until_reset)."""
        window = int(time.time() // 60)
        key = f"gs:rl:{route}:{client}:{window}"
        reset = 60 - int(time.time() % 60)
        if self.redis:
            try:
                pipe = self.redis.pipeline()
                pipe.incr(key)
                pipe.expire(key, 61)
                n = int(pipe.execute()[0])
            except Exception:
                return True, reset  # fail open: availability over strictness
        else:
            with self._mem_lock:
                exp, raw = self._mem.get(key, (0, "0"))
                n = (int(raw) if exp > time.time() else 0) + 1
                self._mem[key] = (time.time() + 61, str(n))
        ok = n <= limit_per_min
        if not ok:
            self.stats["rate_limited"] += 1
        return ok, reset

    # ---------------- jobs / queue -------------------------------------
    def enqueue(self, kind: str, payload: dict) -> str:
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "kind": kind, "payload": payload, "created": time.time()}
        self.set_job(job_id, status="queued", kind=kind)
        if self.redis:
            self.redis.xadd(STREAM, {"job": json.dumps(job, ensure_ascii=False)}, maxlen=10000)
        else:
            self._q.put(job)
        self.publish({"type": "job", "job_id": job_id, "kind": kind, "status": "queued"})
        return job_id

    def set_job(self, job_id: str, **fields: Any) -> None:
        cur = self.get_job(job_id) or {"id": job_id}
        cur.update(fields)
        cur["updated"] = time.time()
        self.cache_set(f"job:{job_id}", cur, 86400)

    def get_job(self, job_id: str) -> dict | None:
        if self.redis:
            raw = self.redis.get("gs:c:job:" + job_id)
            return json.loads(raw) if raw else None
        with self._mem_lock:
            item = self._mem.get("job:" + job_id)
            return json.loads(item[1]) if item else None

    def queue_depth(self) -> int:
        if self.redis:
            try:
                info = self.redis.xinfo_groups(STREAM)
                return int(sum(g.get("lag") or 0 for g in info) + sum(g.get("pending", 0) for g in info))
            except Exception:
                return -1
        return self._q.qsize()

    def consume(self, consumer: str, handler: Callable[[dict], None], stop: threading.Event) -> None:
        """Blocking consume loop with at-least-once delivery."""
        while not stop.is_set():
            if not self.redis:
                try:
                    job = self._q.get(timeout=1)
                except pyqueue.Empty:
                    continue
                self._safe(handler, job)
                continue
            try:
                # Reclaim jobs whose worker died mid-way (idle > 120 s).
                claimed = self.redis.xautoclaim(STREAM, GROUP, consumer, min_idle_time=120_000, count=5)
                entries = claimed[1] if claimed else []
                if not entries:
                    resp = self.redis.xreadgroup(GROUP, consumer, {STREAM: ">"}, count=1, block=2000)
                    entries = resp[0][1] if resp else []
                for msg_id, fields in entries:
                    if not fields:
                        self.redis.xack(STREAM, GROUP, msg_id)
                        continue
                    job = json.loads(fields["job"])
                    deliveries = self._deliveries(msg_id)
                    if deliveries > MAX_DELIVERIES:
                        self.redis.xadd(DEAD, fields, maxlen=1000)
                        self.redis.xack(STREAM, GROUP, msg_id)
                        self.set_job(job["id"], status="failed", error="moved to dead-letter queue")
                        continue
                    if self._safe(handler, job):
                        self.redis.xack(STREAM, GROUP, msg_id)
            except Exception as exc:
                log.error("consume loop error: %s", exc)
                time.sleep(2)

    def _deliveries(self, msg_id: str) -> int:
        try:
            info = self.redis.xpending_range(STREAM, GROUP, min=msg_id, max=msg_id, count=1)
            return int(info[0]["times_delivered"]) if info else 1
        except Exception:
            return 1

    def _safe(self, handler: Callable[[dict], None], job: dict) -> bool:
        try:
            handler(job)
            return True
        except Exception as exc:
            log.exception("job %s failed", job.get("id"))
            self.set_job(job["id"], status="failed", error=str(exc)[:300])
            self.publish({"type": "job", "job_id": job["id"], "status": "failed", "error": str(exc)[:200]})
            return not self.redis  # in redis mode leave un-acked for retry

    # ---------------- pub/sub ------------------------------------------
    def publish(self, event: dict) -> None:
        event.setdefault("ts", time.time())
        if self.redis:
            try:
                self.redis.publish(CHANNEL, json.dumps(event, ensure_ascii=False))
            except Exception as exc:
                log.warning("publish failed: %s", exc)
            return
        for q in list(self._subs):
            q.put(event)

    def subscribe(self, stop: threading.Event | None = None, timeout: float = 15.0) -> Iterator[dict | None]:
        """Yields events; yields None every `timeout` seconds as a heartbeat."""
        if self.redis:
            ps = self.redis.pubsub(ignore_subscribe_messages=True)
            ps.subscribe(CHANNEL)
            try:
                while not (stop and stop.is_set()):
                    msg = ps.get_message(timeout=timeout)
                    yield json.loads(msg["data"]) if msg else None
            finally:
                ps.close()
            return
        q: "pyqueue.Queue[dict]" = pyqueue.Queue()
        self._subs.append(q)
        try:
            while not (stop and stop.is_set()):
                try:
                    yield q.get(timeout=timeout)
                except pyqueue.Empty:
                    yield None
        finally:
            self._subs.remove(q)


_bus: Bus | None = None


def get_bus() -> Bus:
    global _bus
    if _bus is None:
        _bus = Bus(settings.redis_url)
    return _bus


def set_bus(bus: Bus) -> None:  # tests
    global _bus
    _bus = bus
