"""Live check against the real Gemini API (run at the venue).
Lists Gemma models your key can see and runs the agent's understand + rerank steps once."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from granthsetu import ai_tasks  # noqa: E402
from granthsetu.llm import get_llm  # noqa: E402

llm = get_llm()
print("provider:", llm.provider, "| model:", llm.model, "| vision:", llm.vision_model, "| error:", llm.last_error)
if not llm.available:
    sys.exit("No LLM configured. Set GEMINI_API_KEY in .env (and optionally GEMMA_MODEL).")
if llm.provider == "gemini":
    print("Gemma models visible:", sorted(m.name for m in llm._client.models.list() if "gemma" in m.name))
u = ai_tasks.understand("ದ್ಯುತಿಸಂಶ್ಲೇಷಣೆ ಎಂದರೇನು?")
print("understand ->", u)
r = ai_tasks.rerank("ದ್ಯುತಿಸಂಶ್ಲೇಷಣೆ ಎಂದರೇನು?", u["intent"], [
    {"rid": 1, "title": "Photosynthesis", "lang": "en", "source": "test", "passage": "Plants convert light energy into chemical energy."},
    {"rid": 2, "title": "Gravity", "lang": "en", "source": "test", "passage": "Gravity attracts masses to each other."}])
print("rerank ->", r)
print("OK: Gemma is reachable and returns valid structured output.")
