"""Test harness: temp SQLite + in-memory bus + hash embedder + a scripted fake Gemma.
The fake LLM is clearly a test double; real Gemma calls are exercised by
`scripts/smoke_live.py` when GEMINI_API_KEY is set."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("AUTO_SEED", "false")
os.environ.setdefault("LLM_PROVIDER", "none")
os.environ.setdefault("EMBEDDING_PROVIDER", "hash")

from granthsetu import bus as busmod, db as dbmod, embeddings, llm as llmmod, retrieval  # noqa: E402
from granthsetu.ingest import ingest_records  # noqa: E402
from granthsetu.llm import LLMError  # noqa: E402

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "records.json").read_text(encoding="utf-8"))["records"]


class FakeGemma:
    """Scripted stand-in for Gemma. `fail` makes every call raise like an API outage."""

    provider = "fake"
    model = "fake-gemma"
    vision_model = "fake-gemma"
    available = True
    last_error = None

    def __init__(self, fail: bool = False, explain_override: list | None = None) -> None:
        self.fail = fail
        self.explain_override = explain_override
        self.calls: list[str] = []

    def info(self) -> dict:
        return {"provider": "fake", "model": self.model}

    def generate_json(self, prompt: str, image: bytes | None = None, **_):
        if self.fail:
            raise LLMError("simulated API outage")
        if "Analyse the learner's query" in prompt:
            self.calls.append("understand")
            q = prompt.split('"""')[1]
            if "ದ್ಯುತಿ" in q or "photosynth" in q.lower() or "प्रकाश" in q:
                return {"language": "kn" if "ದ" in q else "hi" if "प्र" in q else "en", "intent": "learn photosynthesis",
                        "subject": "science", "topic": "Photosynthesis",
                        "expansions": {"en": ["photosynthesis plants sunlight glucose"], "hi": [], "kn": []}}
            return {"language": "en", "intent": "unknown", "subject": "general", "topic": "Quantum chromodynamics",
                    "expansions": {"en": ["quantum chromodynamics quarks"], "hi": [], "kn": []}}
        if "strict relevance judge" in prompt:
            self.calls.append("rerank")
            ids = [int(x) for x in re.findall(r"^\[(\d+)\]", prompt, flags=re.M)]
            q = prompt.split('"""')[1].lower()
            about_photo = "photosynth" in q or "ದ್ಯುತಿ" in q or "प्रकाश" in q
            out = []
            for i in ids:
                line = re.search(rf"^\[{i}\].*$", prompt, flags=re.M).group(0).lower()
                rel = about_photo and ("photosynth" in line or "ದ್ಯುತಿ" in line)
                out.append({"id": i, "score": 9 if rel else 1, "reason": "matches topic" if rel else "different topic"})
            out.append({"id": 99999, "score": 10, "reason": "invented id must be dropped"})
            return out
        if "Write a short explanation" in prompt:
            self.calls.append("explain")
            if self.explain_override is not None:
                return {"sentences": self.explain_override}
            pid = int(re.search(r"<P(\d+) ", prompt).group(1))
            return {"sentences": [
                {"pid": pid, "quote": "green plants use sunlight, water and carbon dioxide to make glucose",
                 "source_text": "Green plants use sunlight, water and carbon dioxide to make glucose.",
                 "display_text": "ಹಸಿರು ಸಸ್ಯಗಳು ಸೂರ್ಯನ ಬೆಳಕು, ನೀರು ಮತ್ತು ಇಂಗಾಲದ ಡೈಆಕ್ಸೈಡ್ ಬಳಸಿ ಗ್ಲುಕೋಸ್ ತಯಾರಿಸುತ್ತವೆ."},
                {"pid": pid, "quote": "photosynthesis was discovered on Mars in 2050",
                 "source_text": "Photosynthesis was discovered on Mars in 2050.",
                 "display_text": "unsupported"},
            ]}
        if "photo of a textbook page" in prompt:
            return {"language": "kn", "subject": "science", "topic": "Photosynthesis", "key_terms": ["ದ್ಯುತಿಸಂಶ್ಲೇಷಣೆ"],
                    "questions": [], "extracted_text": "ದ್ಯುತಿಸಂಶ್ಲೇಷಣೆ", "search_query": "ದ್ಯುತಿಸಂಶ್ಲೇಷಣೆ", "readability": "good"}
        raise LLMError("unexpected prompt")


PG_TEST_URL = os.getenv("PG_TEST_URL", "")  # e.g. postgresql://gs:gs@localhost:5432/gs_test (the DB is wiped per test)


def _fresh_db(tmp_path):
    if not PG_TEST_URL:
        return dbmod.Database(sqlite_path=str(tmp_path / "t.db"))
    import psycopg

    with psycopg.connect(PG_TEST_URL, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
    return dbmod.Database(PG_TEST_URL)


@pytest.fixture()
def env(tmp_path):
    db = _fresh_db(tmp_path)
    db.init_schema()
    dbmod.set_db(db)
    busmod.set_bus(busmod.Bus(""))
    embeddings.set_embedder(embeddings.HashEmbedder())
    llmmod.set_llm(type("NoLLM", (), {"available": False, "provider": "none", "model": "", "vision_model": "",
                                      "info": lambda self: {"provider": "none"}})())
    retrieval.set_index(retrieval.HybridIndex())
    ingest_records(FIXTURE)
    db.bump_index_version()
    retrieval.get_index().ensure_fresh()
    yield db
    db.close()
