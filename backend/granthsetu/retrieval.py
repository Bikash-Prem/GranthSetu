"""Hybrid retrieval: BM25 (keyword) + FAISS (dense) fused with Reciprocal Rank Fusion.

Every API replica holds its own in-memory index built from the database
(read replica). When the ingest worker commits new resources it bumps
`index_version` and publishes an event; each replica then rebuilds. If the
replica has not caught up with the primary yet (replication lag), we wait
briefly and then fall back to reading from the primary.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict

import faiss
import numpy as np
from rank_bm25 import BM25Okapi

from .config import settings
from .db import get_db
from .embeddings import from_bytes, get_embedder
from .policy import ANON, Principal, can_discover, decide
from .text import index_terms

log = logging.getLogger("granthsetu.retrieval")


class HybridIndex:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.version = -1
        self.passages: list[dict] = []
        self.bm25: BM25Okapi | None = None
        self.faiss: faiss.Index | None = None
        self.faiss_rows: list[int] = []  # faiss row -> passage idx
        self.row_rid = np.zeros(0, dtype=np.int64)
        self.row_content = np.zeros(0, dtype=bool)
        self.res_attrs: dict[int, dict] = {}
        self.built_at = 0.0
        self.build_ms = 0
        self.source = "replica"
        self.skipped_embeddings = 0

    # ---------------- build --------------------------------------------
    def ensure_fresh(self, wanted_version: int | None = None) -> None:
        db = get_db()
        primary = False
        current = db.index_version()
        if wanted_version is not None and current < wanted_version:
            for _ in range(10):  # replica lag: wait up to ~5 s
                time.sleep(0.5)
                current = db.index_version()
                if current >= wanted_version:
                    break
            else:
                primary, current = True, db.index_version(primary=True)
        if current != self.version or not self.passages and current:
            self.rebuild(primary=primary, version=current)

    def rebuild(self, primary: bool = False, version: int | None = None) -> None:
        t0 = time.time()
        db = get_db()
        rows = db.all_passages(primary=primary)
        emb_name = get_embedder().name
        corpus = [index_terms(r["title"] + " " + r["text"]) for r in rows]
        bm25 = BM25Okapi(corpus) if rows else None
        vecs, vrows, skipped = [], [], 0
        for i, r in enumerate(rows):
            if r.get("embedding") and r.get("embed_model") == emb_name:
                vecs.append(from_bytes(r["embedding"]))
                vrows.append(i)
            else:
                skipped += 1
        index = None
        if vecs:
            dim = len(vecs[0])
            keep = [j for j, v in enumerate(vecs) if len(v) == dim]
            skipped += len(vecs) - len(keep)
            vrows = [vrows[j] for j in keep]
            mat = np.vstack([vecs[j] for j in keep]).astype("float32")
            index = faiss.IndexFlatIP(dim)  # cosine on L2-normalised vectors
            index.add(mat)
        attrs: dict[int, dict] = {}
        for r in rows:
            attrs.setdefault(int(r["resource_id"]), {
                "id": int(r["resource_id"]), "status": r.get("status") or "PUBLISHED", "policy": r.get("policy") or "OPEN_ACCESS",
                "catalogue": r.get("catalogue") or "discoverable", "allow_display": r.get("allow_display", True),
                "allow_ai": r.get("allow_ai", True), "institution_id": r.get("institution_id"), "group_id": r.get("group_id")})
        row_rid = np.array([int(r["resource_id"]) for r in rows], dtype=np.int64)
        row_content = np.array([(r.get("pkind") or "content") == "content" for r in rows], dtype=bool)
        with self._lock:
            self.passages, self.bm25, self.faiss, self.faiss_rows = rows, bm25, index, vrows
            self.row_rid, self.row_content, self.res_attrs = row_rid, row_content, attrs
            self.version = version if version is not None else db.index_version(primary=primary)
            self.built_at, self.build_ms = time.time(), int((time.time() - t0) * 1000)
            self.source = "primary" if primary else ("replica" if db.has_replica else "single-node")
            self.skipped_embeddings = skipped
        log.info("index v%s rebuilt: %d passages, %d vectors, %d ms", self.version, len(rows), len(vrows), self.build_ms)

    # ---------------- access ---------------------------------------------
    def access_mask(self, p: Principal) -> tuple[np.ndarray, dict[int, bool]]:
        """Which passages this principal may retrieve, decided BEFORE ranking.

        A content passage needs read access; a metadata passage (title/abstract) needs only
        discovery. Restricted full text therefore never influences ranking, snippets or AI
        context for someone who cannot read it. The agent re-checks every result against the
        database, so a policy change made after this snapshot was built is still honoured."""
        with self._lock:
            attrs, row_rid, row_content = self.res_attrs, self.row_rid, self.row_content
        readable: dict[int, bool] = {}
        discover_ok, read_ok = set(), set()
        for rid, a in attrs.items():
            if can_discover(p, a):
                discover_ok.add(rid)
                if decide(p, a, "read").allowed:
                    read_ok.add(rid)
            readable[rid] = rid in read_ok
        if not len(row_rid):
            return np.zeros(0, dtype=bool), readable
        in_read = np.isin(row_rid, np.fromiter(read_ok, dtype=np.int64, count=len(read_ok)))
        in_disc = np.isin(row_rid, np.fromiter(discover_ok, dtype=np.int64, count=len(discover_ok)))
        return (row_content & in_read) | (~row_content & in_disc), readable

    # ---------------- search -------------------------------------------
    def keyword(self, queries: list[str], k: int, mask: np.ndarray | None = None) -> list[tuple[int, float]]:
        with self._lock:
            if not self.bm25:
                return []
            scores = np.zeros(len(self.passages))
            for q in queries:
                terms = index_terms(q)
                if terms:
                    scores = np.maximum(scores, self.bm25.get_scores(terms))
        if mask is not None and len(mask) == len(scores):
            scores = np.where(mask, scores, 0.0)
        order = np.argsort(-scores)[:k]
        return [(int(i), float(scores[i])) for i in order if scores[i] > 0]

    def semantic(self, queries: list[str], k: int, mask: np.ndarray | None = None) -> list[tuple[int, float]]:
        with self._lock:
            index, rows = self.faiss, self.faiss_rows
        if index is None or not queries:
            return []
        params = None
        if mask is not None:
            allowed = np.array([j for j, prow in enumerate(rows) if prow < len(mask) and mask[prow]], dtype=np.int64)
            if not len(allowed):
                return []
            # FAISS-level pre-filter: disallowed vectors are never scored.
            params = faiss.SearchParameters(sel=faiss.IDSelectorBatch(allowed))
        emb = get_embedder()
        # The lexical hash fallback gives every same-script text some similarity, so demand more.
        floor = settings.semantic_floor if emb.semantic else max(settings.semantic_floor, 0.25)
        qv = emb.embed(queries, kind="query")
        kk = min(k, index.ntotal)
        sims, ids = (index.search(qv.astype("float32"), kk, params=params) if params is not None
                     else index.search(qv.astype("float32"), kk))
        best: dict[int, float] = {}
        for qi in range(len(queries)):
            for s, j in zip(sims[qi], ids[qi]):
                if j < 0 or s < floor:
                    continue
                p = rows[int(j)]
                best[p] = max(best.get(p, -1.0), float(s))
        return sorted(best.items(), key=lambda x: -x[1])[:k]

    @staticmethod
    def rrf(*rankings: list[tuple[int, float]], k: int | None = None) -> list[tuple[int, float]]:
        k = k or settings.rrf_k
        fused: dict[int, float] = defaultdict(float)
        for ranking in rankings:
            for rank, (idx, _) in enumerate(ranking):
                fused[idx] += 1.0 / (k + rank + 1)
        return sorted(fused.items(), key=lambda x: -x[1])

    def search(self, queries: list[str], mode: str = "hybrid", k: int | None = None,
               principal: Principal = ANON) -> dict:
        """Access-aware retrieval. Returns resource-level candidates (best permitted passage per
        resource): up to k the principal can read and up to k they can only discover."""
        k = k or settings.candidate_k
        depth = max(k * 4, 40)
        mask, readable = self.access_mask(principal)
        kw = self.keyword(queries, depth, mask) if mode in ("keyword", "hybrid") else []
        sem = self.semantic(queries, depth, mask) if mode in ("semantic", "hybrid") else []
        if mode == "keyword":
            fused = kw
        elif mode == "semantic":
            fused = sem
        else:
            fused = self.rrf(kw, sem)
        kw_score = dict(kw)
        sem_score = dict(sem)
        seen: dict[int, dict] = {}
        counts = {"open": 0, "restricted": 0}  # k candidates per column
        with self._lock:
            passages = self.passages
        for idx, score in fused:
            p = passages[idx]
            rid = int(p["resource_id"])
            if rid in seen:
                continue
            access = p.get("access") or "open"
            can_read = readable.get(rid, False)
            grp = "open" if can_read else "restricted"
            if counts[grp] >= k:
                continue
            counts[grp] += 1
            seen[rid] = {
                "access": access,
                "can_read": can_read,
                "pkind": p.get("pkind") or "content",
                "page_no": p.get("page_no"),
                "rid": rid,
                "pid": int(p["id"]),
                "passage": p["text"],
                "title": p["title"],
                "lang": p["lang"],
                "topic_key": p.get("topic_key"),
                "key": p["key"],
                "fused": round(score, 5),
                "bm25": round(kw_score.get(idx, 0.0), 3),
                "semantic": round(sem_score.get(idx, 0.0), 3) if idx in sem_score else None,
            }
            if all(v >= k for v in counts.values()):
                break
        return {"candidates": list(seen.values()), "keyword_hits": len(kw), "semantic_hits": len(sem)}

    def passage_text(self, pid: int) -> str:
        with self._lock:
            for p in self.passages:
                if int(p["id"]) == pid:
                    return p["text"]
        return ""

    def passages_for(self, rid: int, limit: int = 3) -> list[dict]:
        with self._lock:
            return [p for p in self.passages if int(p["resource_id"]) == rid and (p.get("pkind") or "content") == "content"][:limit]

    def info(self) -> dict:
        return {
            "version": self.version,
            "passages": len(self.passages),
            "vectors": 0 if self.faiss is None else int(self.faiss.ntotal),
            "skipped_embeddings": self.skipped_embeddings,
            "build_ms": self.build_ms,
            "built_from": self.source,
            "embedder": get_embedder().name,
        }


_index: HybridIndex | None = None


def get_index() -> HybridIndex:
    global _index
    if _index is None:
        _index = HybridIndex()
    return _index


def set_index(i: HybridIndex) -> None:  # tests
    global _index
    _index = i
