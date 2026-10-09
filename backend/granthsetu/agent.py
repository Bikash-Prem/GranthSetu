"""The GranthSetu agent: an explicit, observable multi-step workflow.

input -> understand (Gemma) -> multilingual expansion -> hybrid retrieval (BM25 + FAISS)
      -> RRF fusion -> Gemma rerank with reasons -> confidence decision
      -> grounded explanation (Gemma) -> sentence-level citation verification (our code)
      -> final answer, OR honest failure + Gap Board entry + live fetch job
Every step is recorded in `trace` and shown in the UI.
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from . import ai_tasks
from .bus import get_bus
from .config import settings
from .db import get_db
from .llm import LLMError, get_llm
from .policy import ANON, Principal, access_label, decide, policy_version
from .retrieval import get_index
from .text import LANG_NAMES, detect_lang, keyword_topic
from .verifier import verify_all

log = logging.getLogger("granthsetu.agent")

MODES = ("keyword", "semantic", "hybrid", "agent")


class Trace:
    def __init__(self) -> None:
        self.steps: list[dict] = []

    def step(self, name: str, status: str, t0: float, **detail: Any) -> None:
        self.steps.append({"step": name, "status": status, "ms": int((time.time() - t0) * 1000), **detail})


def _understand(query: str, trace: Trace, use_llm: bool) -> dict:
    t0 = time.time()
    lang = detect_lang(query)
    base = {"language": lang, "intent": "", "subject": "general", "topic": "", "expansions": {"en": [], "hi": [], "kn": []}}
    if not use_llm:
        trace.step("understand", "skipped", t0, note="no language model; script-based language detection only",
                   language=lang)
        return base
    try:
        u = ai_tasks.understand(query)
        trace.step("understand", "ok", t0, language=u["language"], subject=u["subject"], topic=u["topic"],
                   intent=u["intent"])
        return u
    except LLMError as exc:
        trace.step("understand", "fallback", t0, error=str(exc)[:160], language=lang)
        return base


def _queries(query: str, u: dict) -> list[str]:
    qs = [query]
    if u.get("topic"):
        qs.append(u["topic"])
    for lang in ("en", "hi", "kn"):
        qs += u["expansions"].get(lang, [])
    seen, out = set(), []
    for q in qs:
        k = q.strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(q.strip())
    return out[:12]


def run(query: str, mode: str = "agent", explain: bool = True, use_cache: bool = True,
        record_gaps: bool = True, use_llm: bool | None = None, principal: Principal = ANON) -> dict:
    t_start = time.time()
    query = (query or "").strip()
    if not 2 <= len(query) <= 500:
        raise ValueError("query must be 2-500 characters")
    mode = mode if mode in MODES else "agent"
    llm = get_llm()
    use_llm = llm.available if use_llm is None else (use_llm and llm.available)
    index = get_index()
    index.ensure_fresh()
    bus = get_bus()

    db = get_db()
    # The cache key includes who is asking and the policy version: a revoked entitlement or a
    # withdrawal bumps policy_version, so no cached answer can outlive the access it was built on.
    ck = "search:" + hashlib.sha256(
        f"{query.lower()}|{mode}|{explain}|{index.version}|{llm.model if use_llm else 'nollm'}|{index.info()['embedder']}"
        f"|{principal.fingerprint()}|{policy_version(db)}".encode()
    ).hexdigest()
    if use_cache:
        hit = bus.cache_get(ck)
        if hit:
            hit["cached"] = True
            hit["served_by"] = settings.instance_id
            hit["timings"]["total_ms"] = int((time.time() - t_start) * 1000)
            return hit

    trace = Trace()
    agent_llm = use_llm and mode == "agent"
    u = _understand(query, trace, agent_llm)
    queries = _queries(query, u) if agent_llm else [query]
    trace.step("expand", "ok" if len(queries) > 1 else "skipped", time.time(), queries=queries)

    # ---- retrieval --------------------------------------------------------
    t0 = time.time()
    retr_mode = "hybrid" if mode == "agent" else mode
    r = index.search(queries, mode=retr_mode, principal=principal)
    cands = r["candidates"]
    # Defence in depth: re-check every candidate against the database's current status, policy and
    # entitlements (the in-memory index may be a few seconds older than a withdrawal or revocation).
    res_meta = db.resources_by_ids([c["rid"] for c in cands])
    checked = []
    for c in cands:
        m = res_meta.get(c["rid"])
        if m is None:
            continue
        can_read = decide(principal, m, "read").allowed
        if c["pkind"] == "content" and not can_read:
            continue
        if not decide(principal, m, "discover").allowed:
            continue
        c["can_read"] = can_read
        c["can_ai"] = can_read and decide(principal, m, "ai").allowed
        c["source"] = m.get("source", "?")
        checked.append(c)
    dropped = len(cands) - len(checked)
    cands = checked
    trace.step("retrieve", "ok" if cands else "empty", t0, mode=retr_mode, keyword_hits=r["keyword_hits"],
               semantic_hits=r["semantic_hits"], candidates=len(cands), fusion="RRF" if retr_mode == "hybrid" else None,
               access_filter="pre-filtered by policy; re-checked against the database",
               dropped_by_recheck=dropped)

    # ---- rerank -----------------------------------------------------------
    reranked = False
    if agent_llm and cands:
        t0 = time.time()
        try:
            scores = ai_tasks.rerank(query, u.get("intent", ""), cands)
            for c in cands:
                s = scores.get(c["rid"])
                c["rerank_score"] = s["score"] if s else None
                c["reason"] = s["reason"] if s else "not scored by the reranker"
            cands.sort(key=lambda c: (-(c["rerank_score"] if c["rerank_score"] is not None else -1), -c["fused"]))
            reranked = True
            trace.step("rerank", "ok", t0, model=llm.model, scored=len(scores))
        except LLMError as exc:
            trace.step("rerank", "fallback", t0, error=str(exc)[:160], note="kept fused order")

    # ---- split: what this user can read vs. records they may only discover ------
    restricted = [c for c in cands if not c["can_read"]]
    cands = [c for c in cands if c["can_read"]]

    # ---- confidence decision ------------------------------------------------
    t0 = time.time()
    top = cands[0] if cands else None
    if not top:
        confidence, why = "none", "no candidate passages matched"
    elif reranked:
        best = top.get("rerank_score") or 0
        confidence = "high" if best >= settings.rerank_min_score else "low"
        why = f"best rerank score {best:.1f} vs threshold {settings.rerank_min_score}"
    else:
        sem = max((c["semantic"] or 0) for c in cands[:3]) if any(c["semantic"] is not None for c in cands[:3]) else None
        kw = top["bm25"]
        ok = (sem is not None and sem >= settings.semantic_min_sim) or kw >= 3.0
        confidence = "medium" if ok else "low"
        why = f"no reranker; bm25={kw:.2f}" + (f", semantic={sem:.2f}" if sem is not None else "")
    shown = [c for c in cands if not reranked or (c.get("rerank_score") or 0) >= settings.rerank_min_score / 2][: settings.result_k]
    if reranked:
        shown_r = [c for c in restricted if (c.get("rerank_score") or 0) >= settings.rerank_min_score]
    else:
        shown_r = [c for c in restricted if (c["semantic"] or 0) >= settings.semantic_min_sim or c["bm25"] >= 3.0]
    shown_r = shown_r[: settings.result_k]
    trace.step("decide", confidence, t0, rule=why, open_results=len(shown), restricted_results=len(shown_r))

    # ---- grounded explanation + verification ---------------------------------
    explanation: dict[str, Any] = {"status": "skipped", "sentences": [], "pass_rate": None}
    target_lang = u.get("language") or detect_lang(query)
    if explain and agent_llm and confidence == "high":
        t0 = time.time()
        # Only full-text passages the user may read AND whose licence allows machine processing.
        use = [c for c in shown if (c.get("rerank_score") or 0) >= settings.rerank_min_score
               and c.get("can_ai") and c.get("pkind") == "content"][:3]
        passages = [{"pid": c["pid"], "lang": c["lang"], "title": c["title"], "text": c["passage"]} for c in use]
        try:
            if not passages:
                raise LLMError("no passages that may be sent to the model")
            sents = ai_tasks.explain(query, target_lang, passages)
            checked, rate = verify_all(sents, {p["pid"]: p["text"] for p in passages})
            kept = [s for s in checked if s["verified"]]
            # Never pass off an untranslated sentence as a translation.
            src_lang = {p["pid"]: p["lang"] for p in passages}
            untranslated = 0
            for s_ in checked:
                need = src_lang.get(s_["pid"]) != target_lang
                ok = bool(s_.get("display_text")) and (not need or target_lang == "en" or detect_lang(s_["display_text"]) == target_lang)
                s_["translated"] = need and ok
                if not ok:
                    s_["display_text"] = s_.get("source_text") or ""
                    untranslated += 1 if need else 0
            explanation = {
                "status": "verified" if kept and len(kept) == len(checked) else "partial" if kept else "rejected",
                "sentences": checked,
                "pass_rate": round(rate, 3),
                "language": target_lang,
                "translation": "not_needed" if all(src_lang.get(s_["pid"]) == target_lang for s_ in checked)
                else ("unavailable" if untranslated == len(checked) else "partial" if untranslated else "ok"),
                "evidence": [{"pid": p["pid"], "title": p["title"], "page_no": c.get("page_no"), "resource_id": c["rid"]}
                             for p, c in zip(passages, use)],
                "scope": "Based only on the passages listed as evidence, not on the whole book.",
            }
            trace.step("explain", "ok" if sents else "empty", t0, sentences=len(sents))
            trace.step("verify", explanation["status"], time.time(), kept=len(kept), rejected=len(checked) - len(kept))
        except LLMError as exc:
            trace.step("explain", "fallback", t0, error=str(exc)[:160], note="showing sources without a summary")
            explanation["status"] = "failed"

    # ---- honest failure path: Gap Board + live fetch ---------------------------
    gap, live_job = None, None
    if confidence in ("low", "none") and record_gaps and mode == "agent":
        t0 = time.time()
        topic = (u.get("topic") or keyword_topic(query)).strip()[:120]
        db = get_db()
        gap = db.record_gap(target_lang, u.get("subject") or "general", topic.lower())
        if gap.get("status") in ("open", "fetch_failed") and (gap.get("hits", 1) == 1 or gap.get("status") == "fetch_failed" or not gap.get("job_id")):
            # Store the query only inside the job payload (for the live fetch), never on the Gap Board.
            live_job = bus.enqueue("ingest_query", {"query": query, "lang": target_lang, "topic": u.get("topic") or topic,
                                                    "subject": u.get("subject"), "gap_id": gap["id"]})
            db.update_gap(int(gap["id"]), status="fetching", job_id=live_job)
            gap["status"] = "fetching"
        trace.step("gap_board", "recorded", t0, topic=topic, live_fetch_job=live_job)

    # ---- assemble ---------------------------------------------------------------
    def card(rank: int, c: dict) -> dict:
        m = res_meta.get(c["rid"], {})
        return {
            "access": m.get("access") or "open",
            "policy": access_label(principal, m),
            "managed": m.get("origin") == "managed",
            "page_no": c.get("page_no"),
            "snippet_kind": c.get("pkind"),
            "rank": rank,
            "id": c["rid"],
            "title": m.get("title", c["title"]),
            "source": m.get("source"),
            "kind": m.get("kind"),
            "url": m.get("url"),
            "lang": c["lang"],
            "lang_name": LANG_NAMES.get(c["lang"], c["lang"]),
            "cross_lingual": c["lang"] != target_lang,
            "licence": m.get("licence"),
            "licence_url": m.get("licence_url"),
            "attribution": m.get("attribution"),
            "author": m.get("author"),
            "topic_key": m.get("topic_key"),
            "passage": c["passage"],
            "passage_id": c["pid"],
            "scores": {"fused": c["fused"], "bm25": c["bm25"], "semantic": c["semantic"],
                       "rerank": c.get("rerank_score")},
            "reason": c.get("reason"),
        }

    results = [card(i, c) for i, c in enumerate(shown, 1)]
    restricted_results = [card(i, c) for i, c in enumerate(shown_r, 1)]

    decision = {
        "high": "Relevant open resources found and explained with verified citations." if explanation["status"] in ("verified", "partial")
        else "Relevant open resources found.",
        "medium": "Resources found by retrieval only (AI reranking unavailable); judge relevance yourself.",
        "low": "Nothing in the library answers this well yet. We did not generate an answer. The topic was added to the Knowledge Gap Board and a live fetch from open libraries was started.",
        "none": "No matching resources. We did not generate an answer. The topic was added to the Knowledge Gap Board and a live fetch was started.",
    }[confidence]
    if confidence in ("low", "none") and not live_job and gap:
        decision = decision.replace(" and a live fetch from open libraries was started", "").replace(" and a live fetch was started", "") + " A live fetch for this topic already ran."
    if restricted_results:
        n = len(restricted_results)
        decision += (f" {n} more resource{'s match' if n > 1 else ' matches'} but {'are' if n > 1 else 'is'} not readable here for you "
                     "(second column): only the catalogue record is shown; follow the access route on each card.")

    out = {
        "query": query,
        "mode": mode,
        "language": target_lang,
        "intent": u.get("intent"),
        "subject": u.get("subject"),
        "topic": u.get("topic"),
        "expansions": queries,
        "confidence": confidence,
        "decision": decision,
        "results": results,
        "restricted_results": restricted_results,
        "explanation": explanation,
        "gap": {"id": gap["id"], "topic": gap["topic"], "status": gap["status"], "hits": gap["hits"]} if gap else None,
        "live_fetch_job": live_job,
        "trace": trace.steps,
        "ai": {"provider": llm.provider if use_llm else "none", "model": llm.model if use_llm else None,
               "data_sent_to_google": bool(use_llm and llm.provider == "gemini" and mode == "agent")},
        "index_version": index.version,
        "served_by": settings.instance_id,
        "cached": False,
        "timings": {"total_ms": int((time.time() - t_start) * 1000)},
    }
    out["viewer"] = "signed-in" if principal.authenticated else "anonymous"
    if use_cache and confidence not in ("low", "none"):
        bus.cache_set(ck, out, settings.cache_ttl_search_s)
    return out
