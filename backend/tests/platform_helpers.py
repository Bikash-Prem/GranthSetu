"""Helpers for platform tests: real PDFs, a synchronous worker drain, users and clients."""
from __future__ import annotations

import io
import zlib

from fastapi.testclient import TestClient

from granthsetu import auth, bus as busmod, library
from granthsetu.api import app
from granthsetu.ingest import handle


def make_pdf(pages: list[str], outline: list[tuple[str, int]] | None = None) -> bytes:
    """A small but genuine PDF with a text layer (Helvetica), built by hand so tests need no extra library."""
    objs: list[bytes] = []

    def add(b: bytes) -> int:
        objs.append(b)
        return len(objs)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pages_id = len(objs) + 1 + 2 * len(pages)
    page_ids = []
    for text in pages:
        lines = [ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for ln in text.split("\n")]
        stream = "BT /F1 11 Tf 50 780 Td 14 TL " + " ".join(f"({ln}) Tj T*" for ln in lines) + " ET"
        data = zlib.compress(stream.encode("latin-1"))
        c = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(data) + data + b"\nendstream")
        page_ids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 %d 0 R >> >> "
                            b"/Contents %d 0 R >>" % (pages_id, font, c)))
    assert add(b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % i for i in page_ids) + b"] /Count %d >>" % len(page_ids)) == pages_id
    cat_extra = b""
    if outline:
        first = len(objs) + 2
        out_id = len(objs) + 1
        items = []
        for i, (title, pn) in enumerate(outline):
            me = first + i
            prev = b" /Prev %d 0 R" % (me - 1) if i else b""
            nxt = b" /Next %d 0 R" % (me + 1) if i + 1 < len(outline) else b""
            items.append(b"<< /Title (%s) /Parent %d 0 R /Dest [%d 0 R /Fit]%s%s >>" % (title.encode(), out_id, page_ids[pn - 1], prev, nxt))
        add(b"<< /Type /Outlines /First %d 0 R /Last %d 0 R /Count %d >>" % (first, first + len(outline) - 1, len(outline)))
        for it in items:
            add(it)
        cat_extra = b" /Outlines %d 0 R" % out_id
    cat = add(b"<< /Type /Catalog /Pages %d 0 R%s >>" % (pages_id, cat_extra))
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(buf.tell())
        buf.write(b"%d 0 obj\n" % i + o + b"\nendobj\n")
    xref = buf.tell()
    buf.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        buf.write(b"%010d 00000 n \n" % off)
    buf.write(b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, cat, xref))
    return buf.getvalue()


def make_scanned_pdf(text: str) -> bytes:
    """A PDF page that is only an image (no text layer), like a scanned book page."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1400, 500), "white")
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=44)
    y = 40
    for line in text.split("\n"):
        d.text((40, y), line, fill="black", font=font)
        y += 70
    buf = io.BytesIO()
    img.save(buf, "PDF", resolution=150)
    return buf.getvalue()


def drain(max_rounds: int = 20) -> int:
    """Run the worker synchronously: deliver the outbox, then handle every queued bus job."""
    b = busmod.get_bus()
    n = 0
    for _ in range(max_rounds):
        library.dispatch_outbox()
        if b._q.empty():
            break
        while not b._q.empty():
            job = b._q.get_nowait()
            b._safe(handle, job)
            n += 1
    return n


def user(email: str, roles: list[str] | None = None, pw: str = "correct horse battery") -> dict:
    uid = auth.create_user(email, pw, email.split("@")[0], roles=roles or ["reader"])
    c = TestClient(app)
    r = c.post("/api/auth/token", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    c.headers["Authorization"] = "Bearer " + r.json()["token"]
    return {"id": uid, "client": c, "email": email}


def anon() -> TestClient:
    return TestClient(app)


BOOK = ("Chapter 1 Light and Life\nGreen plants capture sunlight in chloroplasts.\n"
        "Photosynthesis converts carbon dioxide and water into glucose and oxygen.")


def register_book(lib: dict, policy: str = "OPEN_ACCESS", basis: str = "open_licence", pdf: bytes | None = None,
                  publish: bool = True, title: str = "Plant Energy", **extra) -> int:
    """Full workflow through the HTTP API: create -> upload -> submit -> review -> rights -> approve -> publish -> process."""
    c = lib["client"]
    body = {"title": title, "author": "A. Author", "lang": "en", "subject": "science", "licence": "CC BY 4.0",
            "rights_basis": basis, "policy": policy, "catalogue": extra.pop("catalogue", "discoverable"),
            "description": f"{title}: a short book used in tests.", **extra}
    r = c.post("/api/manage/resources", json=body)
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    if pdf is not False and basis not in ("catalogue_only", "external_provider"):
        data = pdf or make_pdf([BOOK + f"\nUnique marker {title}.", "Chapter 2 Respiration\nCells release energy from glucose."])
        r = c.post(f"/api/manage/resources/{rid}/file", files={"file": ("book.pdf", data, "application/pdf")})
        assert r.status_code == 200, r.text
    for action in ("submit", "start_review"):
        r = c.post(f"/api/manage/resources/{rid}/transition", json={"action": action})
        assert r.status_code == 200, (action, r.text)
    r = c.post(f"/api/manage/resources/{rid}/rights", json={"decision": "verified", "notes": "licence checked"})
    assert r.status_code == 200, r.text
    r = c.post(f"/api/manage/resources/{rid}/transition", json={"action": "approve"})
    assert r.status_code == 200, r.text
    if publish:
        r = c.post(f"/api/manage/resources/{rid}/transition", json={"action": "publish"})
        assert r.status_code == 200, r.text
        drain()
    return rid
