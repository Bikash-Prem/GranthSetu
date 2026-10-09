"""Central configuration. Every tunable lives here and is read from the environment."""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field

try:  # .env is optional; docker compose passes env vars directly
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # --- storage -------------------------------------------------------
    # Writes go to the primary, reads to the replica (read/write split).
    # Empty DATABASE_URL -> local SQLite file (dev mode, no docker needed).
    database_url: str = os.getenv("DATABASE_URL", "")
    database_read_url: str = os.getenv("DATABASE_READ_URL", "")
    sqlite_path: str = os.getenv("SQLITE_PATH", "data/granthsetu.db")
    # Empty REDIS_URL -> in-process cache, queue and pub/sub (dev mode).
    redis_url: str = os.getenv("REDIS_URL", "")

    # --- AI ------------------------------------------------------------
    llm_provider: str = os.getenv("LLM_PROVIDER", "auto")  # auto|openai|gemini|ollama|none
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "") or os.getenv("OPENAI_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "")  # empty -> auto-discover
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemma_model: str = os.getenv("GEMMA_MODEL", "")  # empty -> auto-discover
    gemma_vision_model: str = os.getenv("GEMMA_VISION_MODEL", "")
    ollama_url: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
    ollama_llm_model: str = os.getenv("OLLAMA_LLM_MODEL", "gemma4")
    embedding_provider: str = os.getenv("EMBEDDING_PROVIDER", "auto")  # auto|ollama|sentence_transformers|gemini|hash
    ollama_embed_model: str = os.getenv("OLLAMA_EMBED_MODEL", "embeddinggemma")
    st_embed_model: str = os.getenv("ST_EMBED_MODEL", "google/embeddinggemma-300m")
    llm_timeout_s: float = _f("LLM_TIMEOUT_S", 45)

    # --- agent thresholds ----------------------------------------------
    candidate_k: int = _i("CANDIDATE_K", 12)
    result_k: int = _i("RESULT_K", 5)
    rrf_k: int = _i("RRF_K", 60)
    rerank_min_score: float = _f("RERANK_MIN_SCORE", 5.0)  # 0-10 scale
    semantic_floor: float = _f("SEMANTIC_FLOOR", 0.08)  # ignore near-random dense neighbours
    semantic_min_sim: float = _f("SEMANTIC_MIN_SIM", 0.30)
    verify_min_lexical: float = _f("VERIFY_MIN_LEXICAL", 0.35)
    verify_min_semantic: float = _f("VERIFY_MIN_SEMANTIC", 0.70)

    # --- accounts and library management --------------------------------
    session_ttl_hours: int = _i("SESSION_TTL_HOURS", 12)
    cookie_secure: bool = os.getenv("COOKIE_SECURE", "false") == "true"  # set true behind HTTPS
    registration_open: bool = os.getenv("REGISTRATION_OPEN", "true") == "true"
    librarian_can_verify_rights: bool = os.getenv("LIBRARIAN_CAN_VERIFY_RIGHTS", "true") == "true"
    require_separate_reviewer: bool = os.getenv("REQUIRE_SEPARATE_REVIEWER", "false") == "true"
    storage_dir: str = os.getenv("STORAGE_DIR", "data/private_files")  # never inside the web root
    max_book_bytes: int = _i("MAX_BOOK_BYTES", 50 * 1024 * 1024)
    max_pdf_pages: int = _i("MAX_PDF_PAGES", 2000)
    ocr_langs: str = os.getenv("OCR_LANGS", "eng")  # e.g. eng+kan+hin once those traineddata files are installed
    cors_origins: str = os.getenv("CORS_ORIGINS", "")  # comma-separated; empty = same-origin only
    rate_login_per_min: int = _i("RATE_LOGIN_PER_MIN", 10)
    job_stale_after_s: int = _i("JOB_STALE_AFTER_S", 900)

    # --- platform --------------------------------------------------------
    # Scoped token for automation (n8n/cron): may only trigger open-library harvesting.
    admin_token: str = os.getenv("AUTOMATION_TOKEN", os.getenv("ADMIN_TOKEN", ""))
    cache_ttl_search_s: int = _i("CACHE_TTL_SEARCH_S", 600)
    cache_ttl_llm_s: int = _i("CACHE_TTL_LLM_S", 86400)
    rate_search_per_min: int = _i("RATE_SEARCH_PER_MIN", 30)
    rate_scan_per_min: int = _i("RATE_SCAN_PER_MIN", 8)
    rate_ingest_per_min: int = _i("RATE_INGEST_PER_MIN", 4)
    max_upload_bytes: int = _i("MAX_UPLOAD_BYTES", 5 * 1024 * 1024)
    run_worker_inline: bool = os.getenv("RUN_WORKER_INLINE", "auto") != "false"
    auto_seed: bool = os.getenv("AUTO_SEED", "true") == "true"
    refresh_interval_s: int = _i("REFRESH_INTERVAL_S", 0)  # 0 = off (n8n or cron can trigger instead)
    user_agent: str = os.getenv(
        "HTTP_USER_AGENT", "GranthSetu/1.0 (open-source education search; https://github.com/)"
    )
    instance_id: str = field(default_factory=lambda: os.getenv("INSTANCE_ID", socket.gethostname()))

    @property
    def mode(self) -> str:
        return "distributed" if self.database_url and self.redis_url else "lite"


settings = Settings()
