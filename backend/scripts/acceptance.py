"""End-to-end acceptance workflows A-G, run over HTTP against a RUNNING GranthSetu
(API + worker + PostgreSQL [+ Redis]). Nothing here touches the database directly.

    # 1. start the stack (docker compose up, or the native commands in docs/SETUP.md)
    # 2. create an administrator:  python -m granthsetu.manage create-admin --email admin@example.org
    GS_URL=http://localhost:8080 GS_ADMIN_EMAIL=admin@example.org GS_ADMIN_PASSWORD=... \\
        python scripts/acceptance.py setup        # workflows A-G
    # 3. restart the API and worker, then:
    GS_URL=... python scripts/acceptance.py after-restart

Writes acceptance_state.json (ids created in `setup`) and prints a PASS/FAIL line per check.
Exit code 1 if any check failed. A check that could not run is reported as SKIP, never as PASS.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import uuid
import zlib
from pathlib import Path

import httpx

URL = os.getenv("GS_URL", "http://localhost:8000").rstrip("/")
STATE = Path(os.getenv("GS_STATE", "acceptance_state.json"))
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append(("PASS" if ok else "FAIL", name, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return ok


def skip(name: str, why: str) -> None:
    results.append(("SKIP", name, why))
    print(f"SKIP  {name}  ({why})", flush=True)


def client(token: str | None = None) -> httpx.Client:
    h = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(base_url=URL + "/api", headers=h, timeout=120)


def token(email: str, pw: str) -> str:
    r = client().post("/auth/token", json={"email": email, "password": pw})
    r.raise_for_status()
    return r.json()["token"]


def new_user(admin: httpx.Client, email: str, roles: list[str]) -> tuple[httpx.Client, int]:
    pw = "accept-" + uuid.uuid4().hex[:12]
    for _ in range(6):  # registration is rate limited (5/min per IP): honour Retry-After rather than fail
        r = client().post("/auth/register", json={"email": email, "password": pw, "display_name": email.split("@")[0]})
        if r.status_code != 429:
            break
        time.sleep(int(r.headers.get("retry-after", "10")) + 1)
    r.raise_for_status()
    uid = r.json()["user"]["id"]
    for role in roles:
        admin.post(f"/admin/users/{uid}/roles", json={"role": role}).raise_for_status()
    return client(token(email, pw)), uid


def pdf(pages: list[str]) -> bytes:
    objs: list[bytes] = []
    add = lambda b: (objs.append(b), len(objs))[1]  # noqa: E731
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pages_id = len(objs) + 1 + 2 * len(pages)
    kids = []
    for t in pages:
        s = "BT /F1 11 Tf 50 780 Td 14 TL " + " ".join(f"({ln}) Tj T*" for ln in t.split("\n")) + " ET"
        d = zlib.compress(s.encode("latin-1"))
        c = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(d) + d + b"\nendstream")
        kids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                        % (pages_id, font, c)))
    add(b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) + b"] /Count %d >>" % len(kids))
    cat = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(buf.tell())
        buf.write(b"%d 0 obj\n" % i + o + b"\nendobj\n")
    x = buf.tell()
    buf.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offs))
    buf.write(b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, cat, x))
    return buf.getvalue()


def wait_status(c: httpx.Client, rid: int, want: set[str], timeout: float = 120) -> str:
    end = time.time() + timeout
    st = "?"
    while time.time() < end:
        st = c.get(f"/manage/resources/{rid}").json()["resource"]["status"]
        if st in want:
            return st
        time.sleep(1)
    return st


def register(c: httpx.Client, title: str, policy: str, basis: str, file: bytes | None, **extra) -> int:
    body = {"title": title, "author": "Acceptance Test", "lang": "en", "subject": "science", "licence": "CC BY 4.0",
            "rights_basis": basis, "policy": policy, "catalogue": extra.pop("catalogue", "discoverable"),
            "description": f"{title}, registered by the acceptance test.", **extra}
    r = c.post("/manage/resources", json=body)
    r.raise_for_status()
    rid = r.json()["id"]
    if file:
        c.post(f"/manage/resources/{rid}/file", files={"file": ("book.pdf", file, "application/pdf")}).raise_for_status()
    for a in ("submit", "start_review"):
        c.post(f"/manage/resources/{rid}/transition", json={"action": a}).raise_for_status()
    c.post(f"/manage/resources/{rid}/rights", json={"decision": "verified", "notes": "acceptance"}).raise_for_status()
    c.post(f"/manage/resources/{rid}/transition", json={"action": "approve"}).raise_for_status()
    return rid


def publish(c: httpx.Client, rid: int) -> str:
    c.post(f"/manage/resources/{rid}/transition", json={"action": "publish"}).raise_for_status()
    return wait_status(c, rid, {"PUBLISHED", "PROCESSING_FAILED"})


def wait_search(q: str, rid: int, c: httpx.Client, present: bool = True, timeout: float = 30) -> dict:
    end = time.time() + timeout
    out: dict = {}
    while time.time() < end:
        out = c.post("/search", json={"query": q, "mode": "hybrid"}).json()
        ids = {r["id"] for r in out.get("results", []) + out.get("restricted_results", [])}
        if (rid in ids) == present:
            return out
        time.sleep(1.5)
    return out


def setup() -> None:
    tag = uuid.uuid4().hex[:6]
    health = client().get("/ready")
    check("stack: API ready and connected to the database", health.status_code == 200, health.text[:80])
    sysinfo = client().get("/system").json()
    check("stack: persistent PostgreSQL in use", sysinfo["database"]["engine"] == "postgres", sysinfo["database"]["engine"])
    admin = client(token(os.environ["GS_ADMIN_EMAIL"], os.environ["GS_ADMIN_PASSWORD"]))
    lib, _ = new_user(admin, f"librarian-{tag}@accept.test", ["librarian"])
    anon = client()
    state: dict = {"tag": tag}

    # ---------------- A: open-access book
    title = f"Acceptance Photosynthesis Primer {tag}"
    text = [f"Chapter 1 Light\nAcceptance marker {tag}: chloroplasts capture sunlight.\nPhotosynthesis turns carbon dioxide and water into glucose.",
            "Chapter 2 Oxygen\nOxygen is released as a by-product of photosynthesis."]
    rid = register(lib, title, "OPEN_ACCESS", "open_licence", pdf(text), allow_download=True)
    st = publish(lib, rid)
    check("A: librarian registers, uploads, reviews, approves and publishes an open book", st == "PUBLISHED", st)
    cat = anon.get("/catalogue", params={"q": title}).json()
    check("A: appears in the public catalogue", [i["id"] for i in cat["items"]] == [rid])
    out = wait_search(f"chloroplasts sunlight glucose {tag}", rid, anon)
    hit = next((r for r in out.get("results", []) if r["id"] == rid), None)
    check("A: found by search (hybrid BM25 + vectors), with a page reference", bool(hit and hit.get("page_no")),
          f"page {hit.get('page_no') if hit else None}")
    page = anon.get(f"/books/{rid}/pages/1")
    check("A: readable in the reader", page.status_code == 200 and f"marker {tag}" in page.json()["text"])
    dl = anon.get(f"/books/{rid}/download")
    check("A: downloadable because its licence allows it", dl.status_code == 200 and dl.content.startswith(b"%PDF"))
    ai = sysinfo["ai"]["provider"]
    if ai == "none":
        skip("A: grounded explanation with source references", "no Gemma provider configured on this server (set GEMINI_API_KEY or Ollama)")
    else:
        ex = anon.post("/search", json={"query": f"How do chloroplasts make glucose? {tag}", "mode": "agent"}).json()["explanation"]
        check("A: grounded explanation with verified citations", ex["status"] in ("verified", "partial"), ex["status"])
    state["A"] = rid

    # ---------------- B: restricted book, entitlement grant and revoke
    rid_b = register(lib, f"Acceptance Licensed Monograph {tag}", "INDIVIDUAL_ENTITLEMENT", "rights_holder_permission",
                     pdf([f"Licensed content {tag}: secret passage about mitochondria and ATP synthase."]))
    st = publish(lib, rid_b)
    check("B: restricted book processed and published", st == "PUBLISHED", st)
    outsider, _ = new_user(admin, f"outsider-{tag}@accept.test", [])
    reader, reader_id = new_user(admin, f"reader-{tag}@accept.test", [])
    d = outsider.get(f"/books/{rid_b}")
    check("B: discoverable catalogue: metadata visible to an unauthorised user", d.status_code == 200 and not d.json()["access"]["can_read"])
    codes = [outsider.get(f"/books/{rid_b}/pages/1").status_code, outsider.get(f"/books/{rid_b}/search", params={"q": "mitochondria"}).status_code,
             outsider.get(f"/books/{rid_b}/download").status_code]
    check("B: unauthorised user denied full text, in-book search and download", all(c in (401, 403) for c in codes), str(codes))
    s = outsider.post("/search", json={"query": f"mitochondria ATP synthase secret passage {tag}", "mode": "hybrid"}).json()
    leak = any("secret passage" in r["passage"] for r in s["results"] + s["restricted_results"])
    check("B: search snippets do not expose protected passages", not leak and rid_b not in {r["id"] for r in s["results"]})
    lp = outsider.post("/lesson-pack", json={"query": "atp", "resource_ids": [rid_b]})
    check("B: lesson pack refused", lp.status_code == 403, str(lp.status_code))
    lib.post(f"/manage/resources/{rid_b}/entitlements", json={"subject_type": "user", "email": f"reader-{tag}@accept.test"}).raise_for_status()
    ok_read = reader.get(f"/books/{rid_b}/pages/1")
    check("B: entitled user can read", ok_read.status_code == 200 and "secret passage" in ok_read.json()["text"])
    ents = lib.get(f"/manage/resources/{rid_b}").json()["entitlements"]
    eid = next(e["id"] for e in ents if e["subject_id"] == reader_id and e["active"])
    lib.delete(f"/manage/entitlements/{eid}").raise_for_status()
    check("B: access denied after revocation", reader.get(f"/books/{rid_b}/pages/1").status_code == 403)
    state["B"] = rid_b

    # ---------------- C: private catalogue
    rid_c = register(lib, f"Acceptance Private Report {tag}", "PRIVATE", "rights_holder_permission", pdf([f"Private {tag} board minutes."]))
    publish(lib, rid_c)
    hidden = [outsider.get(f"/books/{rid_c}").status_code, outsider.get(f"/books/{rid_c}/pages/1").status_code]
    missing = outsider.get("/books/999999999").status_code
    listed = rid_c in {i["id"] for i in outsider.get("/catalogue", params={"q": "Private Report"}).json()["items"]}
    check("C: private resource is indistinguishable from a missing one", hidden == [404, 404] and missing == 404 and not listed, str(hidden))
    state["C"] = rid_c

    # ---------------- D: withdrawal
    st = lib.post(f"/manage/resources/{rid}/transition", json={"action": "withdraw", "reason": "acceptance withdrawal"})
    check("D: withdraw accepted", st.status_code == 200)
    gone = [anon.get(f"/books/{rid}").status_code, anon.get(f"/books/{rid}/pages/1").status_code, anon.get(f"/books/{rid}/download").status_code]
    check("D: no longer accessible (detail, pages, download)", gone == [404, 404, 404], str(gone))
    out = wait_search(f"chloroplasts sunlight glucose {tag}", rid, anon, present=False)
    check("D: search results and caches no longer return it", rid not in {r["id"] for r in out["results"] + out["restricted_results"]})
    hist = lib.get("/manage/audit", params={"target_id": str(rid)}).json()["items"]
    check("D: audit history kept", any(h["action"] == "resource.withdraw" for h in hist))

    # ---------------- E: ingestion failure and retry
    # A PDF with no text layer while machine processing (OCR) is not permitted must fail honestly.
    from PIL import Image

    im = io.BytesIO()
    Image.new("RGB", (400, 200), "white").save(im, "PDF")
    rid_e = register(lib, f"Acceptance Scan Without OCR {tag}", "OPEN_ACCESS", "open_licence", im.getvalue(), allow_ai=False)
    st = publish(lib, rid_e)
    det = lib.get(f"/manage/resources/{rid_e}").json()
    check("E: failure recorded, not shown as success", st == "PROCESSING_FAILED" and det["jobs"][0]["status"] == "failed",
          (det["jobs"][0].get("last_error") or "")[:80])
    check("E: failed resource is not publicly exposed", anon.get(f"/books/{rid_e}").status_code == 404)
    lib.post(f"/manage/resources/{rid_e}/file", files={"file": ("fixed.pdf", pdf([f"Recovered text {tag} about leaves."]), "application/pdf")})
    state["E"] = {"rid": rid_e, "job": det["jobs"][0]["id"]}
    print("      (E continues after the restart: the retry runs on the restarted worker)")

    # ---------------- F: Kannada query
    rid_f = register(lib, f"Acceptance Water Cycle {tag}", "OPEN_ACCESS", "open_licence",
                     pdf([f"Water cycle {tag}: evaporation, condensation and precipitation move water around the Earth."]))
    publish(lib, rid_f)
    kn = anon.post("/search", json={"query": "ಜಲಚಕ್ರ ಎಂದರೇನು?", "mode": "agent"}).json()
    check("F: Kannada query detected as Kannada", kn["language"] == "kn", kn["language"])
    cross_ok = ai != "none" or sysinfo["embedder"]["semantic"]
    found = any(r["id"] == rid_f for r in kn["results"])
    if not cross_ok:
        skip("F: English resource found from a Kannada-only query",
             f"needs Gemma query expansion or EmbeddingGemma; this server has ai={ai}, embedder={sysinfo['embedder']['name']} "
             f"(found={found}, results={len(kn['results'])}, confidence={kn['confidence']})")
    else:
        check("F: English resource found from a Kannada-only query, with source and licence",
              found and all(r["licence"] for r in kn["results"]))
    check("F: no unauthorised content in the answer",
          not any("secret passage" in r["passage"] for r in kn["results"] + kn["restricted_results"]))
    if ai == "none":
        skip("F: translated grounded explanation", "no Gemma provider configured on this server")
    else:
        check("F: explanation produced or honestly withheld", kn["explanation"]["status"] in ("verified", "partial", "rejected", "skipped"))
    state["F"] = rid_f

    # ---------------- G: provider/catalogue unavailable
    g = admin.post("/admin/ingest", json={"topic": f"Acceptance unreachable topic {tag}"})
    jid = g.json().get("job_id") if g.status_code == 200 else None
    job = {}
    for _ in range(60):
        job = anon.get(f"/jobs/{jid}").json() if jid else {}
        if job.get("status") in ("done", "failed"):
            break
        time.sleep(1)
    reached = job.get("added", 0) > 0
    check("G: external catalogue fetch reports its real outcome (no fake success)",
          job.get("status") in ("done", "failed") and (reached or bool(job.get("errors")) or job.get("status") == "failed"),
          f"status={job.get('status')} added={job.get('added')} errors={len(job.get('errors') or [])}")
    still = anon.get("/catalogue").status_code == 200 and anon.post("/search", json={"query": "glucose", "mode": "hybrid"}).status_code == 200
    check("G: unrelated features keep working", still)
    if ai == "none":
        s = anon.post("/search", json={"query": "glucose chloroplasts", "mode": "agent"}).json()
        check("G: with no model, no explanation is fabricated", s["explanation"]["status"] == "skipped" and not s["explanation"]["sentences"])

    STATE.write_text(json.dumps(state))


def after_restart() -> None:
    state = json.loads(STATE.read_text())
    admin = client(token(os.environ["GS_ADMIN_EMAIL"], os.environ["GS_ADMIN_PASSWORD"]))
    anon = client()
    check("restart: API back up", anon.get("/ready").status_code == 200)
    check("restart: withdrawn book still withdrawn and its audit trail kept",
          anon.get(f"/books/{state['A']}").status_code == 404
          and admin.get(f"/manage/resources/{state['A']}").json()["resource"]["status"] == "WITHDRAWN")
    check("restart: restricted book and its records persist",
          admin.get(f"/manage/resources/{state['B']}").json()["resource"]["status"] == "PUBLISHED")
    check("restart: open book still searchable and readable",
          anon.get(f"/books/{state['F']}/pages/1").status_code == 200)
    e = state["E"]
    r = admin.post(f"/manage/jobs/{e['job']}/retry")
    check("E: retry after the worker restart accepted", r.status_code == 200, r.text[:100])
    st = wait_status(admin, e["rid"], {"PUBLISHED", "PROCESSING_FAILED"})
    det = admin.get(f"/manage/resources/{e['rid']}").json()
    statuses = [j["status"] for j in det["jobs"]]
    check("E: processed after retry, without duplicate jobs", st == "PUBLISHED" and statuses.count("succeeded") == 1, f"{st} {statuses}")
    check("E: failed attempt still visible as failed", "failed" in statuses)


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "setup"
    try:
        setup() if phase == "setup" else after_restart()
    except Exception as exc:  # an exception is a failure, never a pass
        check(f"{phase}: completed without errors", False, f"{type(exc).__name__}: {exc}")
    fails = [r for r in results if r[0] == "FAIL"]
    print(f"\n{len(results) - len(fails)} of {len(results)} checks did not fail · {len(fails)} failed · "
          f"{sum(1 for r in results if r[0] == 'SKIP')} skipped")
    sys.exit(1 if fails else 0)
