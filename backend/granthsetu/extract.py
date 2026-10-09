"""Upload validation and text extraction for books.

Supported formats: PDF (text layer, plus OCR of scanned pages when Tesseract is
installed and the licence allows machine processing) and UTF-8 plain text.
Parsing untrusted PDFs runs in a separate process with a time limit, so a
malformed or hostile file cannot hang or crash the worker.
"""
from __future__ import annotations

import io
import logging
import multiprocessing as mp
import re
import shutil
import unicodedata

from .config import settings

log = logging.getLogger("granthsetu.extract")

MIME_PDF, MIME_TXT = "application/pdf", "text/plain"


class ExtractionError(Exception):
    pass


def sniff(data: bytes) -> str:
    """Decide the type from the bytes, never from the file name or the client's content-type."""
    if data[:5] == b"%PDF-":
        return MIME_PDF
    head = data[:65536]
    if b"\x00" in head:
        raise ValueError("Unsupported file type. Upload a PDF or a UTF-8 text file.")
    try:
        (head[3:] if head.startswith(b"\xef\xbb\xbf") else head).decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.start < len(head) - 4:  # a multi-byte char cut at the 64 KiB boundary is fine
            raise ValueError("Text files must be UTF-8 encoded.")
    return MIME_TXT


def safe_filename(name: str) -> str:
    base = re.split(r"[\\/]", name or "")[-1]
    base = unicodedata.normalize("NFC", base)
    base = re.sub(r"[^\w.\- ()ऀ-ॿಀ-೿]", "_", base).strip(" .")
    return base[:120] or "upload"


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFC", t.replace("\r\n", "\n").replace("\r", "\n"))
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"-\n(?=[a-z])", "", t)  # re-join hyphenated line breaks
    return re.sub(r"\n{3,}", "\n\n", t).strip()


HEADING = re.compile(r"^\s*(chapter|unit|lesson|part|ಅಧ್ಯಾಯ|ಪಾಠ|अध्याय|पाठ)\s*[\dIVXLC०-९೦-೯]+[^\n]{0,80}$",
                     re.IGNORECASE | re.MULTILINE)


def ocr_available() -> bool:
    if not shutil.which("tesseract"):
        return False
    try:
        import pytesseract  # noqa: F401
        return True
    except ImportError:
        return False


def _ocr_page_images(page) -> str:
    import pytesseract
    from PIL import Image

    texts = []
    for img in list(page.images)[:4]:
        try:
            im = Image.open(io.BytesIO(img.data))
            im.load()
            texts.append(pytesseract.image_to_string(im.convert("L"), lang=settings.ocr_langs, timeout=60))
        except Exception as exc:  # one unreadable image must not fail the page
            log.warning("ocr failed on an image: %s", exc)
    return "\n".join(texts)


def _extract_pdf(data: bytes, allow_ocr: bool) -> dict:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
    except Exception as exc:
        raise ExtractionError(f"not a readable PDF ({type(exc).__name__})")
    if reader.is_encrypted:
        # We never try to break encryption or DRM.
        raise ExtractionError("the PDF is encrypted or DRM-protected; upload an unprotected copy you are permitted to use")
    n = len(reader.pages)
    if n == 0:
        raise ExtractionError("the PDF has no pages")
    if n > settings.max_pdf_pages:
        raise ExtractionError(f"the PDF has {n} pages (limit {settings.max_pdf_pages})")
    pages, ocr_pages, empty, warnings = [], 0, 0, []
    can_ocr = allow_ocr and ocr_available()
    for i, page in enumerate(reader.pages, 1):
        try:
            t = page.extract_text() or ""
        except Exception as exc:
            t, _ = "", warnings.append(f"page {i}: text extraction failed ({type(exc).__name__})")
        used_ocr = False
        if len(t.strip()) < 20 and can_ocr:
            o = _ocr_page_images(page)
            if len(o.strip()) >= 20:
                t, used_ocr = o, True
                ocr_pages += 1
        t = _norm(t)
        if not t:
            empty += 1
        pages.append({"page_no": i, "text": t, "ocr": used_ocr})
    chapters = []
    try:
        for item in reader.outline:
            if isinstance(item, list):
                continue
            pn = reader.get_destination_page_number(item)
            if pn is not None and pn >= 0:
                chapters.append({"title": str(item.title)[:200], "start_page": pn + 1})
    except Exception:
        chapters = []
    if empty == n:
        if not allow_ocr:
            raise ExtractionError("no text layer, and the licence does not allow OCR")
        if not ocr_available():
            raise ExtractionError("no text layer and OCR is not installed (install tesseract-ocr and pytesseract)")
        raise ExtractionError("no readable text found, even with OCR")
    if empty:
        warnings.append(f"{empty} of {n} pages had no readable text")
    return {"pages": pages, "chapters": chapters, "ocr_pages": ocr_pages, "warnings": warnings, "paged": "pdf"}


def _extract_text(data: bytes) -> dict:
    text = _norm(data.decode("utf-8-sig", errors="replace"))
    if len(text) < 20:
        raise ExtractionError("the text file is empty")
    # Plain text has no pages: we make ~3,000-character sections at paragraph breaks.
    paras = re.split(r"\n\s*\n", text)
    pages, buf = [], ""
    for para in paras:
        if buf and len(buf) + len(para) > 3000:
            pages.append(buf)
            buf = ""
        buf += ("\n\n" if buf else "") + para
    if buf:
        pages.append(buf)
    out = [{"page_no": i, "text": t, "ocr": False} for i, t in enumerate(pages, 1)]
    return {"pages": out, "chapters": [], "ocr_pages": 0, "warnings": [], "paged": "sections"}


def _detect_chapters(pages: list[dict]) -> list[dict]:
    """Chapter headings near the top of a page. Running headers repeat the same chapter number on
    every page, so a heading whose 'Chapter N' key equals the previous one does not start a chapter."""
    found, last_key = [], None
    for p in pages:
        m = HEADING.search(p["text"][:400])
        if not m:
            continue
        key = " ".join(m.group(0).lower().split()[:2])
        if key == last_key:
            continue
        last_key = key
        found.append({"title": m.group(0).strip()[:200], "start_page": p["page_no"]})
    return found


def finish_chapters(pages: list[dict], chapters: list[dict]) -> list[dict]:
    chapters = sorted({c["start_page"]: c for c in (chapters or _detect_chapters(pages))}.values(),
                      key=lambda c: c["start_page"])
    last = pages[-1]["page_no"]
    if not chapters:
        chapters = [{"title": "Full text", "start_page": 1}]
    elif chapters[0]["start_page"] > 1:
        chapters.insert(0, {"title": "Front matter", "start_page": 1})
    out = []
    for i, c in enumerate(chapters):
        end = (chapters[i + 1]["start_page"] - 1) if i + 1 < len(chapters) else last
        out.append({"chapter_no": i + 1, "title": c["title"], "start_page": c["start_page"], "end_page": max(end, c["start_page"])})
    for p in pages:
        p["chapter_no"] = next((c["chapter_no"] for c in reversed(out) if c["start_page"] <= p["page_no"]), 1)
    return out


def _run(data: bytes, mime: str, allow_ocr: bool) -> dict:
    out = _extract_pdf(data, allow_ocr) if mime == MIME_PDF else _extract_text(data)
    out["chapters"] = finish_chapters(out["pages"], out["chapters"])
    return out


def _child(q, data, mime, allow_ocr):  # pragma: no cover - runs in a subprocess
    try:
        q.put(("ok", _run(data, mime, allow_ocr)))
    except ExtractionError as exc:
        q.put(("error", str(exc)))
    except Exception as exc:
        q.put(("error", f"extraction crashed: {type(exc).__name__}: {str(exc)[:200]}"))


def extract(data: bytes, mime: str, allow_ocr: bool, timeout_s: int = 600) -> dict:
    """Extract pages and chapters in an isolated process with a time limit."""
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    proc = ctx.Process(target=_child, args=(q, data, mime, allow_ocr), daemon=True)
    proc.start()
    import queue as _queue
    import time as _time

    deadline = _time.monotonic() + timeout_s
    try:
        while True:
            try:
                status, payload = q.get(timeout=1)
                break
            except _queue.Empty:
                if not proc.is_alive():  # crashed (e.g. out of memory) without reporting
                    raise ExtractionError(f"extraction process died (exit code {proc.exitcode})")
                if _time.monotonic() > deadline:
                    proc.kill()
                    raise ExtractionError(f"extraction timed out after {timeout_s}s")
    finally:
        proc.join(5)
    if status != "ok":
        raise ExtractionError(payload)
    return payload
