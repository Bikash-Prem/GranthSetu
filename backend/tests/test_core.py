from __future__ import annotations

import os
import io

import pytest
from fastapi.testclient import TestClient

from conftest import FakeGemma
from granthsetu import agent, ai_tasks, llm as llmmod, sources
from granthsetu.bus import get_bus
from granthsetu.llm import LLMError, parse_json_loose
from granthsetu.retrieval import HybridIndex, get_index
from granthsetu.text import chunk, detect_lang
from granthsetu.verifier import verify_sentence

PHOTO_EN = "Photosynthesis is the process by which green plants use sunlight, water and carbon dioxide to make glucose and release oxygen."


# ---------------- text ----------------------------------------------------
def test_detect_lang():
    assert detect_lang("ದ್ಯುತಿಸಂಶ್ಲೇಷಣೆ ಎಂದರೇನು?") == "kn"
    assert detect_lang("प्रकाश संश्लेषण क्या है?") == "hi"
    assert detect_lang("what is photosynthesis") == "en"


def test_chunk_keeps_text():
    text = ("Sentence number one is here. " * 60).strip()
    parts = chunk(text, max_chars=300)
    assert len(parts) > 1 and all(len(p) <= 400 for p in parts)


def test_parse_json_loose():
    assert parse_json_loose('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_loose('Sure! [{"id": 2}] hope this helps') == [{"id": 2}]
    with pytest.raises(LLMError):
        parse_json_loose("no json here")


# ---------------- retrieval -------------------------------------------------
@pytest.mark.parametrize("q,expect_lang,expect_topic", [
    ("ದ್ಯುತಿಸಂಶ್ಲೇಷಣೆ", "kn", "Photosynthesis"),
    ("जल चक्र", "hi", "Water cycle"),
    ("how do plants use sunlight to make glucose", "en", "Photosynthesis"),
])
def test_monolingual_retrieval(env, q, expect_lang, expect_topic):
    out = agent.run(q, mode="hybrid", use_cache=False, record_gaps=False)
    top = out["results"][0]
    assert top["topic_key"] == expect_topic and top["lang"] == expect_lang


def test_rrf_fusion_math():
    fused = HybridIndex.rrf([(1, 9.0), (2, 5.0)], [(2, 0.9), (3, 0.8)], k=60)
    order = [i for i, _ in fused]
    assert order[0] == 2  # appears in both lists -> wins
    assert set(order) == {1, 2, 3}


def test_hybrid_uses_both_signals(env):
    r = get_index().search(["gravity force mass"], mode="hybrid")
    assert r["keyword_hits"] > 0 and r["semantic_hits"] > 0
    assert r["candidates"][0]["topic_key"] == "Gravity"


# ---------------- agent with (fake) Gemma ------------------------------------
def test_cross_lingual_agent_with_verified_explanation(env):
    fake = FakeGemma()
    llmmod.set_llm(fake)
    out = agent.run("ದ್ಯುತಿಸಂಶ್ಲೇಷಣೆ ಎಂದರೇನು?", mode="agent", use_cache=False)
    assert fake.calls[:2] == ["understand", "rerank"]
    assert out["confidence"] == "high"
    langs = {r["lang"] for r in out["results"] if r["topic_key"] == "Photosynthesis"}
    assert "en" in langs  # Kannada question surfaced the English resource
    assert any(r["cross_lingual"] for r in out["results"])
    sents = out["explanation"]["sentences"]
    assert [s["verified"] for s in sents] == [True, False]  # the Mars sentence is rejected
    assert out["explanation"]["status"] == "partial"
    assert all(r["licence"] for r in out["results"])
    assert all(isinstance(r["scores"]["rerank"], float) or r["scores"]["rerank"] is None for r in out["results"])


def test_rerank_drops_invented_ids(env):
    llmmod.set_llm(FakeGemma())
    cands = [{"rid": 1, "title": "Photosynthesis", "lang": "en", "source": "x", "passage": "photosynthesis"},
             {"rid": 2, "title": "Gravity", "lang": "en", "source": "x", "passage": "gravity"}]
    scores = ai_tasks.rerank("photosynthesis", "", cands)
    assert set(scores) == {1, 2} and scores[1]["score"] == 9


def test_low_confidence_records_gap_and_starts_live_fetch(env):
    llmmod.set_llm(FakeGemma())
    out = agent.run("explain quantum chromodynamics", mode="agent", use_cache=False)
    assert out["confidence"] in ("low", "none")
    assert out["explanation"]["status"] == "skipped"  # no answer invented
    assert out["gap"]["topic"] == "quantum chromodynamics"
    assert out["live_fetch_job"] and get_bus().get_job(out["live_fetch_job"])["status"] == "queued"
    again = agent.run("explain quantum chromodynamics", mode="agent", use_cache=False)
    assert again["gap"]["hits"] == 2 and again["live_fetch_job"] is None  # no duplicate fetch


def test_api_failure_falls_back_to_retrieval(env):
    llmmod.set_llm(FakeGemma(fail=True))
    out = agent.run("how do plants make glucose with sunlight", mode="agent", use_cache=False, record_gaps=False)
    steps = {s["step"]: s["status"] for s in out["trace"]}
    assert steps["understand"] == "fallback" and steps["rerank"] == "fallback"
    assert out["results"] and out["results"][0]["topic_key"] == "Photosynthesis"


def test_cache_hit(env):
    a = agent.run("gravity force", mode="hybrid")
    b = agent.run("gravity force", mode="hybrid")
    assert a["cached"] is False and b["cached"] is True


# ---------------- verifier ------------------------------------------------------
def test_verifier_accepts_supported_sentence(env):
    s = {"pid": 1, "quote": "green plants use sunlight, water and carbon dioxide",
         "source_text": "Green plants use sunlight, water and carbon dioxide to make glucose."}
    assert verify_sentence(s, PHOTO_EN)["verified"]


def test_verifier_rejects_unsupported_and_wrong_numbers(env):
    fake_quote = {"pid": 1, "quote": "plants were invented by scientists in a lab",
                  "source_text": "Plants were invented by scientists in a lab."}
    r = verify_sentence(fake_quote, PHOTO_EN)
    assert not r["verified"] and "quoted phrase not found" in r["rejection_reason"]
    nums = {"pid": 1, "quote": "green plants use sunlight, water and carbon dioxide",
            "source_text": "Green plants use sunlight, water and carbon dioxide for 12 hours."}
    r2 = verify_sentence(nums, PHOTO_EN)
    assert not r2["verified"] and r2["checks"]["missing_numbers"] == ["12"]


# ---------------- API -------------------------------------------------------------
@pytest.fixture()
def client(env):
    from granthsetu.api import app

    return TestClient(app)


def test_api_search_shows_source_and_licence(client):
    r = client.post("/api/search", json={"query": "water evaporates clouds rain", "mode": "hybrid"})
    assert r.status_code == 200
    top = r.json()["results"][0]
    assert top["topic_key"] == "Water cycle" and top["url"].startswith("https://") and "FIXTURE" in top["licence"]
    assert r.headers["X-Served-By"]


def test_api_rejects_malformed_input(client):
    assert client.post("/api/search", json={"query": ""}).status_code == 422
    assert client.post("/api/search", json={"query": "x" * 600}).status_code == 422
    assert client.post("/api/search", json={}).status_code == 422


def test_api_scan_validation(client):
    bad = client.post("/api/scan", files={"file": ("a.jpg", b"not an image", "image/jpeg")})
    assert bad.status_code == 415
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(buf, "PNG")
    no_llm = client.post("/api/scan", files={"file": ("a.png", buf.getvalue(), "image/png")})
    assert no_llm.status_code == 503  # honest: photo reading needs Gemma
    llmmod.set_llm(FakeGemma())
    ok = client.post("/api/scan", files={"file": ("a.png", buf.getvalue(), "image/png")})
    assert ok.status_code == 200 and ok.json()["topic"] == "Photosynthesis" and "not stored" in ok.json()["privacy"]


def test_api_rate_limit(client, monkeypatch):
    from granthsetu import config

    object.__setattr__(config.settings, "rate_search_per_min", 2)
    codes = [client.post("/api/search", json={"query": "gravity", "mode": "keyword"}).status_code for _ in range(3)]
    object.__setattr__(config.settings, "rate_search_per_min", 30)
    assert codes[-1] == 429


def test_system_endpoint(client):
    s = client.get("/api/system").json()
    assert s["database"]["engine"] == ("postgres" if os.getenv("PG_TEST_URL") else "sqlite") and s["index"]["passages"] > 0


# ---------------- source adapters (recorded API shapes, no network) -----------------
def test_wikipedia_adapter_captures_licence_and_langlinks(monkeypatch):
    sources.mediawiki_rights.cache_clear()

    def fake_get(url, params=None):
        if params and params.get("meta") == "siteinfo":
            return {"query": {"rightsinfo": {"text": "Creative Commons Attribution-Share Alike 4.0",
                                             "url": "https://creativecommons.org/licenses/by-sa/4.0/"}}}
        host = url.split("/")[2]
        if host.startswith("en."):
            return {"query": {"pages": [{"title": "Photosynthesis", "extract": "Photosynthesis is a process.\n\n== References ==\nx",
                                         "fullurl": "https://en.wikipedia.org/wiki/Photosynthesis",
                                         "langlinks": [{"lang": "kn", "title": "ದ್ಯುತಿಸಂಶ್ಲೇಷಣೆ"}]}]}}
        return {"query": {"pages": [{"title": "ದ್ಯುತಿಸಂಶ್ಲೇಷಣೆ", "extract": "ದ್ಯುತಿಸಂಶ್ಲೇಷಣೆ ಒಂದು ಕ್ರಿಯೆ.",
                                     "fullurl": "https://kn.wikipedia.org/wiki/x"}]}}

    monkeypatch.setattr(sources, "_get", fake_get)
    recs = sources.wikipedia_topic("Photosynthesis")
    assert [r["lang"] for r in recs] == ["en", "kn"]
    assert all(r["topic_key"] == "Photosynthesis" for r in recs)
    assert recs[0]["licence"].startswith("Creative Commons") and "References" not in recs[0]["text"]


# ---------------- evaluation ------------------------------------------------------
def test_evaluation_runs_on_real_query_file(env):
    from granthsetu.evaluation import run_evaluation

    llmmod.set_llm(FakeGemma())
    run_id = run_evaluation(use_llm=True)
    run = env.latest_eval()
    assert run["id"] == run_id and run["config"]["modes"] == ["keyword", "semantic", "hybrid", "agent"]
    assert 0 < run["config"]["answerable"] < run["config"]["queries"]  # fixture covers only a few topics
    hyb = run["summary"]["hybrid"]
    assert 0 <= hyb["hit_at_5"] <= 1 and hyb["n"] == run["config"]["answerable"]
    assert run["summary"]["agent"]["verifier_pass_rate"] is not None


def test_research_adapters_parse_real_response_shapes(monkeypatch):
    long = "Photosynthesis converts light energy into chemical energy in plants. " * 6
    inv: dict = {}
    for i, w in enumerate(long.split()):
        inv.setdefault(w, []).append(i)

    def fake_get(url, params=None):
        if "openalex" in url:
            return {"results": [
                {"id": "https://openalex.org/W1", "display_name": "Light capture in leaves", "language": "en",
                 "abstract_inverted_index": inv, "open_access": {}, "best_oa_location": {
                     "landing_page_url": "https://example.org/p1", "license": "cc-by", "source": {"display_name": "Plant J"}},
                 "authorships": [{"author": {"display_name": "A. Rao"}}]},
                {"id": "https://openalex.org/W2", "display_name": "Fotosintesis", "language": "es",
                 "abstract_inverted_index": inv, "best_oa_location": {"landing_page_url": "https://example.org/p2"}}]}
        if "europepmc" in url:
            return {"resultList": {"result": [{"id": "PMC1", "source": "PMC", "title": "Chloroplast study.", "abstractText": "<p>" + long + "</p>",
                                                 "license": "cc by", "journalTitle": "J Bot", "authorString": "Rao A."}]}}
        if "archive.org" in url:
            return {"response": {"docs": [{"identifier": "bk1", "title": "Botany", "description": [long], "licenseurl": "https://creativecommons.org/licenses/by/4.0/",
                                           "language": ["eng"], "creator": "X"},
                                          {"identifier": "bk2", "title": "No licence", "description": [long]}]}}
        raise AssertionError(url)

    monkeypatch.setattr(sources, "_get", fake_get)
    oa = sources.openalex_search("photosynthesis")
    assert [r["key"] for r in oa] == ["openalex:W1"]  # Spanish record skipped, not mislabelled
    assert oa[0]["licence"] == "CC BY" and oa[0]["text"].startswith("Light capture in leaves. Photosynthesis converts")
    pmc = sources.europepmc_search("photosynthesis")
    assert pmc[0]["licence"] == "CC BY" and "<p>" not in pmc[0]["text"]
    ia = sources.internetarchive_search("botany")
    assert [r["key"] for r in ia] == ["ia:bk1"]  # item without a licence is excluded

    xml = ("<feed xmlns='http://www.w3.org/2005/Atom'><entry><id>http://arxiv.org/abs/2101.00001v1</id>"
           f"<title>Light harvesting</title><summary>{long}</summary><author><name>B. Das</name></author></entry></feed>")
    monkeypatch.setattr(sources, "_get_text", lambda url, params=None: xml)
    ax = sources.arxiv_search("photosynthesis")
    assert ax[0]["url"].startswith("https://arxiv.org/abs/") and ax[0]["author"] == "B. Das"


def _paywalled_record():
    return {"key": "openalex:W9", "topic_key": "Photosynthesis", "title": "Advances in photosynthesis research", "source": "openalex",
            "kind": "subscription paper (abstract only)", "url": "https://publisher.example/p9", "lang": "en",
            "licence": "Subscription: full text via your institution's login or purchase at the publisher", "licence_url": None,
            "attribution": "Plant Sci", "author": None, "subject": "science", "access": "authorised",
            "text": "Photosynthesis research has advanced rapidly, with new work on chloroplast efficiency, light harvesting and carbon fixation in crop plants."}


def test_restricted_resources_in_second_list_and_never_explained(env):
    from granthsetu.ingest import ingest_records

    ingest_records([_paywalled_record()])
    env.bump_index_version()
    get_index().ensure_fresh()
    llmmod.set_llm(FakeGemma())
    out = agent.run("ದ್ಯುತಿಸಂಶ್ಲೇಷಣೆ ಎಂದರೇನು?", mode="agent", use_cache=False)
    assert out["results"] and all(r["access"] == "open" for r in out["results"])
    assert [r["title"] for r in out["restricted_results"]] == ["Advances in photosynthesis research"]
    restricted_pid = out["restricted_results"][0]["passage_id"]
    assert restricted_pid not in {s["pid"] for s in out["explanation"]["sentences"]}  # no explanation from paywalled text
    assert "not readable here for you" in out["decision"]


def test_only_restricted_match_still_triggers_gap_and_lists_them(env):
    from granthsetu.ingest import ingest_records

    rec = _paywalled_record() | {"key": "openalex:W10", "title": "Quantum chromodynamics on the lattice", "topic_key": "QCD",
                                 "text": "Quantum chromodynamics on the lattice gives non-perturbative access to quark confinement and hadron masses in the strong interaction."}
    ingest_records([rec])
    env.bump_index_version()
    get_index().ensure_fresh()
    llmmod.set_llm(FakeGemma())
    out = agent.run("explain quantum chromodynamics", mode="agent", use_cache=False)
    assert out["results"] == [] or out["confidence"] in ("low", "none")
    assert out["gap"] is not None  # nothing free and open: still honest about it
    assert out["restricted_results"] == [] or out["restricted_results"][0]["access"] == "authorised"


def test_lesson_pack_rejects_restricted(env):
    from fastapi.testclient import TestClient
    from granthsetu.api import app
    from granthsetu.ingest import ingest_records

    (rec,) = ingest_records([_paywalled_record()])
    r = TestClient(app).post("/api/lesson-pack", json={"query": "x y", "resource_ids": [rec["id"]], "lang": "en"})
    assert r.status_code == 403


def test_paid_and_borrowable_adapters(monkeypatch):
    long = "A thorough introduction to plant biology covering photosynthesis, respiration and growth for school students. " * 3

    def fake_get(url, params=None):
        if "googleapis" in url:
            return {"items": [
                {"id": "g1", "volumeInfo": {"title": "Plant Biology", "description": long, "language": "en", "authors": ["R. K."],
                                           "publisher": "Pub", "infoLink": "https://books.google.com/g1"},
                 "saleInfo": {"saleability": "FOR_SALE", "buyLink": "https://play.google.com/store/books/details?id=g1",
                              "listPrice": {"amount": 499, "currencyCode": "INR"}}},
                {"id": "g2", "volumeInfo": {"title": "Free one", "description": long, "language": "en"}, "saleInfo": {"saleability": "FREE"}}]}
        if "openlibrary" in url:
            return {"docs": [{"key": "/works/OL1W", "title": "Botany for All", "author_name": ["A"], "subject": ["Botany", "Plants"], "ebook_access": "borrowable",
                              "first_sentence": ["Plants make food from light."], "language": ["eng"]},
                             {"key": "/works/OL2W", "title": "Open Botany", "subject": ["Botany"], "ebook_access": "public",
                              "first_sentence": ["All about open botany for school learners."]}]}
        if "openalex" in url:
            assert params["filter"].startswith("open_access.is_oa:false")
            return {"results": [{"id": "https://openalex.org/W5", "display_name": "Subscription paper", "language": "en",
                                 "abstract_inverted_index": {w: [i] for i, w in enumerate(dict.fromkeys(long.split()))} | {"x%d" % i: [100 + i] for i in range(40)},
                                 "primary_location": {"landing_page_url": "https://publisher.example/w5", "source": {"display_name": "J"}}, "authorships": []}]}
        raise AssertionError(url)

    monkeypatch.setattr(sources, "_get", fake_get)
    gb = sources.googlebooks_search("plant biology")
    assert [r["key"] for r in gb] == ["googlebooks:g1"] and gb[0]["access"] == "paid" and "INR 499" in gb[0]["licence"]
    ob = sources.openlibrary_borrow_search("botany")
    assert [r["key"] for r in ob] == ["openlibrary:/works/OL1W"] and ob[0]["access"] == "authorised"
    pub = sources.openlibrary_search("botany")
    assert [r["key"] for r in pub] == ["openlibrary:/works/OL2W"] and pub[0].get("access", "open") == "open"
    pw = sources.openalex_paywalled_search("plants")
    assert pw[0]["access"] == "authorised" and pw[0]["url"] == "https://publisher.example/w5"
