"""Replaceable embedding providers.

Preferred: EmbeddingGemma (open weights) served by Ollama, or loaded with
sentence-transformers. Fallbacks: Gemini embedding API, then a deterministic
character n-gram hashing vectoriser that is clearly labelled as lexical.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Protocol

import httpx
import numpy as np

from .config import settings
from .text import normalise

log = logging.getLogger("granthsetu.embeddings")


class Embedder(Protocol):
    name: str
    open_weight: bool
    semantic: bool

    def embed(self, texts: list[str], kind: str = "doc") -> np.ndarray: ...


def _l2(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype="float32")
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1
    return m / n


def _prefix(texts: list[str], kind: str) -> list[str]:
    # EmbeddingGemma prompt format from its model card.
    if kind == "query":
        return [f"task: search result | query: {t}" for t in texts]
    return [f"title: none | text: {t}" for t in texts]


class HashEmbedder:
    """Character 3-5 gram hashing. Works offline, script-agnostic, NOT cross-lingual."""

    name = "hash-char-ngram-768"
    open_weight = False  # not a model at all; deterministic fallback
    semantic = False
    dim = 768

    def embed(self, texts: list[str], kind: str = "doc") -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype="float32")
        for i, t in enumerate(texts):
            s = f" {normalise(t)} "
            for n in (3, 4, 5):
                for j in range(len(s) - n + 1):
                    h = int.from_bytes(hashlib.blake2b(s[j : j + n].encode(), digest_size=4).digest(), "little")
                    out[i, h % self.dim] += 1.0 if h & 1 else -1.0
        return _l2(out)


class OllamaEmbedder:
    open_weight = True
    semantic = True

    def __init__(self, model: str, url: str) -> None:
        self.model, self.url = model, url.rstrip("/")
        self.name = f"ollama:{model}"

    def available(self) -> bool:
        try:
            r = httpx.get(f"{self.url}/api/tags", timeout=3)
            names = {m["name"].split(":")[0] for m in r.json().get("models", [])}
            return self.model.split(":")[0] in names
        except Exception:
            return False

    def embed(self, texts: list[str], kind: str = "doc") -> np.ndarray:
        vecs: list[list[float]] = []
        for i in range(0, len(texts), 32):
            r = httpx.post(
                f"{self.url}/api/embed",
                json={"model": self.model, "input": _prefix(texts[i : i + 32], kind)},
                timeout=120,
            )
            r.raise_for_status()
            vecs.extend(r.json()["embeddings"])
        return _l2(np.array(vecs))


class STEmbedder:
    open_weight = True
    semantic = True

    def __init__(self, model: str) -> None:
        from sentence_transformers import SentenceTransformer  # optional heavy dep

        self._m = SentenceTransformer(model)
        self.name = f"st:{model}"

    def embed(self, texts: list[str], kind: str = "doc") -> np.ndarray:
        return _l2(self._m.encode(_prefix(texts, kind), batch_size=32))


class GeminiEmbedder:
    """Hosted fallback. Not open-weight; labelled as such in /api/health."""

    open_weight = False
    semantic = True

    def __init__(self, api_key: str, model: str = "gemini-embedding-001") -> None:
        from google import genai

        self._c = genai.Client(api_key=api_key)
        self.model = model
        self.name = f"gemini:{model}"

    def embed(self, texts: list[str], kind: str = "doc") -> np.ndarray:
        from google.genai import types

        task = "RETRIEVAL_QUERY" if kind == "query" else "RETRIEVAL_DOCUMENT"
        vecs = []
        for i in range(0, len(texts), 50):
            r = self._c.models.embed_content(
                model=self.model, contents=texts[i : i + 50], config=types.EmbedContentConfig(task_type=task)
            )
            vecs.extend(e.values for e in r.embeddings)
        return _l2(np.array(vecs))


_embedder: Embedder | None = None


def build_embedder(choice: str | None = None) -> Embedder:
    choice = (choice or settings.embedding_provider).lower()
    if choice in ("ollama", "auto"):
        o = OllamaEmbedder(settings.ollama_embed_model, settings.ollama_url)
        if o.available():
            return o
        if choice == "ollama":
            log.warning("Ollama embedding model %s not available; using hash fallback", o.model)
    if choice == "sentence_transformers":
        try:
            return STEmbedder(settings.st_embed_model)
        except Exception as exc:
            log.warning("sentence-transformers unavailable (%s); using hash fallback", exc)
    if choice == "gemini" and settings.gemini_api_key:
        return GeminiEmbedder(settings.gemini_api_key)
    return HashEmbedder()


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = build_embedder()
        log.info("embedding provider: %s", _embedder.name)
    return _embedder


def set_embedder(e: Embedder) -> None:  # tests
    global _embedder
    _embedder = e


def to_bytes(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype="float32").tobytes()


def from_bytes(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype="float32")
