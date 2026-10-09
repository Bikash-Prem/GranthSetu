"""Gemma access layer.

Primary: Gemma 4 through the Gemini API (google-genai SDK).
Offline:  a Gemma model served locally by Ollama (open weights, data stays local).
None:     the agent still runs retrieval and says plainly that AI steps were skipped.

All model names come from the environment; if GEMMA_MODEL is empty we list the
models the key can see and pick the best available Gemma 4 variant.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
from typing import Any

import httpx

from .bus import get_bus
from .config import settings

log = logging.getLogger("granthsetu.llm")

OPENAI_URL = "https://api.openai.com/v1"
OPENAI_PREFERRED = ["gpt-4.1-mini", "gpt-4o-mini", "gpt-4.1", "gpt-4o"]
PREFERRED = ["gemma-4-31b-it", "gemma-4-26b-a4b-it", "gemma-4-12b-it", "gemma-4-e4b-it", "gemma-3-27b-it"]


class LLMError(RuntimeError):
    """Raised for timeouts, quota errors and malformed outputs. The agent catches it."""


def parse_json_loose(text: str) -> Any:
    """Models sometimes wrap JSON in prose or code fences; recover the object/array."""
    if text is None:
        raise LLMError("empty model output")
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    pairs = sorted((("{", "}"), ("[", "]")), key=lambda oc: t.find(oc[0]) if t.find(oc[0]) != -1 else 10**9)
    for open_c, close_c in pairs:
        s, e = t.find(open_c), t.rfind(close_c)
        if s != -1 and e > s:
            try:
                return json.loads(t[s : e + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError("model did not return valid JSON")


class GemmaClient:
    def __init__(self) -> None:
        self.provider = "none"
        self.model = ""
        self.vision_model = ""
        self._client = None
        self.last_error: str | None = None
        self._choose()

    # ------------------------------------------------------------------
    def _choose(self) -> None:
        want = settings.llm_provider.lower()
        if want in ("auto", "openai") and settings.openai_api_key:
            try:
                self.model = settings.openai_model or self._discover_openai()
                self.vision_model = self.model
                self.provider = "openai"
                return
            except Exception as exc:
                self.last_error = f"OpenAI API init failed: {str(exc)[:200]}"
                log.warning(self.last_error)
        if want in ("auto", "gemini") and settings.gemini_api_key:
            try:
                from google import genai

                self._client = genai.Client(api_key=settings.gemini_api_key)
                self.model = settings.gemma_model or self._discover()
                self.vision_model = settings.gemma_vision_model or self.model
                self.provider = "gemini"
                return
            except Exception as exc:
                self.last_error = f"Gemini API init failed: {exc}"
                log.warning(self.last_error)
        if want in ("auto", "ollama"):
            local = self._ollama_model()
            if local:
                self.provider, self.model, self.vision_model = "ollama", local, local
                return
        self.provider = "none"

    @staticmethod
    def _ollama_model() -> str:
        try:
            r = httpx.get(f"{settings.ollama_url}/api/tags", timeout=3)
            names = [m["name"] for m in r.json().get("models", [])]
        except Exception:
            return ""
        want = settings.ollama_llm_model
        if want in names:
            return want
        match = [n for n in names if n.split(":")[0] == want.split(":")[0]]
        return match[0] if match else ""

    @staticmethod
    def _discover_openai() -> str:
        r = httpx.get(f"{OPENAI_URL}/models", headers={"Authorization": f"Bearer {settings.openai_api_key}"}, timeout=10)
        r.raise_for_status()
        names = {m["id"] for m in r.json().get("data", [])}
        for p in OPENAI_PREFERRED:
            if p in names:
                return p
        raise LLMError("no supported chat model visible to this OpenAI key")

    def _discover(self) -> str:
        names = [m.name.removeprefix("models/") for m in self._client.models.list()]
        gemma = [n for n in names if "gemma" in n]
        for p in PREFERRED:
            if p in gemma:
                return p
        g4 = [n for n in gemma if "gemma-4" in n]
        if g4 or gemma:
            return sorted(g4 or gemma)[-1]
        raise LLMError("no Gemma model visible to this API key")

    @property
    def available(self) -> bool:
        return self.provider != "none"

    def info(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "vision_model": self.vision_model,
            "data_leaves_device": self.provider in ("gemini", "openai"),
            "last_error": self.last_error,
        }

    # ------------------------------------------------------------------
    def generate_json(self, prompt: str, image: bytes | None = None, mime: str = "image/jpeg",
                      cache: bool = True, retries: int = 1) -> Any:
        if not self.available:
            raise LLMError("no language model configured")
        key = None
        if cache and image is None:  # never cache anything derived from user photos
            key = "llm:" + hashlib.sha256(f"{self.provider}|{self.model}|{prompt}".encode()).hexdigest()
            hit = get_bus().cache_get(key)
            if hit is not None:
                return hit
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                text = self._call(prompt, image, mime)
                data = parse_json_loose(text)
                if key:
                    get_bus().cache_set(key, data, settings.cache_ttl_llm_s)
                self.last_error = None
                return data
            except LLMError as exc:  # malformed JSON: ask once more, stricter
                last = exc
                prompt = prompt + "\n\nIMPORTANT: Reply with valid JSON only. No prose, no code fences."
            except Exception as exc:
                last = exc
                msg = str(exc)
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "503" in msg or "UNAVAILABLE" in msg:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
        self.last_error = f"{type(last).__name__}: {str(last)[:200]}"
        raise LLMError(self.last_error)

    def _call(self, prompt: str, image: bytes | None, mime: str) -> str:
        if self.provider == "gemini":
            from google.genai import types

            parts: list[Any] = []
            if image is not None:
                parts.append(types.Part.from_bytes(data=image, mime_type=mime))
            parts.append(prompt)
            # Gemma on the Gemini API does not take system instructions, so all
            # instructions live in the user turn.
            resp = self._client.models.generate_content(
                model=self.vision_model if image is not None else self.model,
                contents=parts,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=2048,
                    http_options=types.HttpOptions(timeout=int(settings.llm_timeout_s * 1000)),
                ),
            )
            return resp.text or ""
        if self.provider == "openai":
            content: Any = prompt
            if image is not None:
                url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
                content = [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": url}}]
            r = httpx.post(
                f"{OPENAI_URL}/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={"model": self.model, "messages": [{"role": "user", "content": content}],
                      "temperature": 0.1, "max_tokens": 2048},
                timeout=settings.llm_timeout_s,
            )
            if r.status_code >= 400:
                # Never raise_for_status here: keep the provider's reason, drop the request (and its key).
                raise RuntimeError(f"OpenAI {r.status_code}: {r.text[:200]}")
            return r.json()["choices"][0]["message"]["content"] or ""
        return self._ollama_chat(self.model, prompt, image)

    @staticmethod
    def _ollama_chat(model: str, prompt: str, media: bytes | None) -> str:
        """Local, offline. Ollama takes images and WAV audio alike in the `images` field."""
        msg: dict[str, Any] = {"role": "user", "content": prompt}
        if media is not None:
            msg["images"] = [base64.b64encode(media).decode()]
        r = httpx.post(
            f"{settings.ollama_url}/api/chat",
            # Thinking is on by default for Gemma 4 and multiplies CPU latency; the JSON tasks don't need it.
            json={"model": model, "messages": [msg], "stream": False, "format": "json", "think": False,
                  "keep_alive": "30m", "options": {"temperature": 0.1}},
            timeout=settings.llm_timeout_s * 3,
        )
        r.raise_for_status()
        return r.json()["message"]["content"]

    def audio_model(self) -> str:
        """Hosted Gemma on the Gemini API rejects audio, so speech always goes to a local Gemma in Ollama."""
        return self.model if self.provider == "ollama" else self._ollama_model()

    def transcribe_json(self, prompt: str, wav: bytes) -> Any:
        model = self.audio_model()
        if not model:
            raise LLMError("no local Gemma model in Ollama for voice input")
        try:
            return parse_json_loose(self._ollama_chat(model, prompt, wav))
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"{type(exc).__name__}: {str(exc)[:200]}")


_llm: GemmaClient | None = None
_llm_built_at = 0.0


def get_llm() -> GemmaClient:
    """Build once; if startup failed (network blip, Ollama not up yet), retry every 60 s."""
    global _llm, _llm_built_at
    retry = (isinstance(_llm, GemmaClient) and _llm.provider == "none" and settings.llm_provider != "none"
             and time.time() - _llm_built_at > 60)
    if _llm is None or retry:
        _llm, _llm_built_at = GemmaClient(), time.time()
        log.info("LLM provider=%s model=%s", _llm.provider, _llm.model)
    return _llm


def set_llm(llm: Any) -> None:  # tests
    global _llm
    _llm = llm
