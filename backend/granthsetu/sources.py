"""Live source adapters for open digital libraries.

Each adapter calls the source's public API at ingestion time and returns
normalised records with the licence captured PER RECORD from the source
itself (MediaWiki rightsinfo, DOAJ journal licence, Gutenberg copyright flag,
Open Library ebook_access). Nothing here is hardcoded content.
"""
from __future__ import annotations

import logging
import urllib.parse
from functools import lru_cache
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("granthsetu.sources")

WIKI_LANGS = ("en", "hi", "kn")
MAX_WIKI_CHARS = 6000


def _client() -> httpx.Client:
    return httpx.Client(timeout=20, headers={"User-Agent": settings.user_agent}, follow_redirects=True)


def _get(url: str, params: dict | None = None) -> Any:
    with _client() as c:
        for attempt in range(2):
            try:
                r = c.get(url, params=params)
                r.raise_for_status()
                return r.json()
            except httpx.HTTPError as exc:
                if attempt == 1:
                    raise
                log.warning("retrying %s: %s", url, exc)


# ---------------------------------------------------------------------------
# MediaWiki: Wikipedia + Wikibooks
# ---------------------------------------------------------------------------
@lru_cache(maxsize=32)
def mediawiki_rights(host: str) -> tuple[str, str]:
    data = _get(f"https://{host}/w/api.php", {"action": "query", "meta": "siteinfo", "siprop": "rightsinfo",
                                              "format": "json", "formatversion": "2"})
    ri = data["query"]["rightsinfo"]
    return ri.get("text") or "see source", ri.get("url") or f"https://{host}"


def _mw_page(host: str, title: str, with_langlinks: bool = False) -> dict | None:
    params = {"action": "query", "format": "json", "formatversion": "2", "prop": "extracts|info",
              "explaintext": "1", "exsectionformat": "wiki", "inprop": "url", "redirects": "1", "titles": title}
    if with_langlinks:
        params["prop"] += "|langlinks"
        params["lllimit"] = "500"
    data = _get(f"https://{host}/w/api.php", params)
    pages = data.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing") or not pages[0].get("extract"):
        return None
    return pages[0]


def _trim_wiki(text: str) -> str:
    # Drop reference-style tail sections; keep the teaching content.
    for marker in ("\n== References", "\n== See also", "\n== External links", "\n== Notes", "\n== Further reading",
                   "\n== सन्दर्भ", "\n== इन्हें भी देखें", "\n== ಉಲ್ಲೇಖಗಳು", "\n== ಬಾಹ್ಯ ಕೊಂಡಿಗಳು"):
        i = text.find(marker)
        if i > 0:
            text = text[:i]
    return text[:MAX_WIKI_CHARS]


def _wiki_record(page: dict, lang: str, host: str, topic_key: str, kind: str, source: str) -> dict:
    licence, licence_url = mediawiki_rights(host)
    title = page["title"]
    return {
        "key": f"{source}:{lang}:{title}",
        "topic_key": topic_key,
        "title": title,
        "source": source,
        "kind": kind,
        "url": page.get("fullurl") or f"https://{host}/wiki/{urllib.parse.quote(title.replace(' ', '_'))}",
        "lang": lang,
        "licence": licence,
        "licence_url": licence_url,
        "attribution": f"“{title}”, {source.title()} contributors ({lang}.{host.split('.', 1)[1]}), text under {licence}",
        "author": f"{source.title()} contributors",
        "subject": None,
        "text": _trim_wiki(page["extract"]),
    }


def wikipedia_topic(en_title: str, langs: tuple[str, ...] = WIKI_LANGS, subject: str | None = None) -> list[dict]:
    """Fetch an English article and its Hindi/Kannada counterparts via langlinks.
    topic_key = English title, so the same topic is linked across languages."""
    host = "en.wikipedia.org"
    page = _mw_page(host, en_title, with_langlinks=True)
    if not page:
        return []
    topic = page["title"]
    out = [_wiki_record(page, "en", host, topic, "encyclopedia article", "wikipedia")] if "en" in langs else []
    links = {ll["lang"]: ll["title"] for ll in page.get("langlinks", [])}
    for lang in langs:
        if lang == "en" or lang not in links:
            continue
        h = f"{lang}.wikipedia.org"
        try:
            p = _mw_page(h, links[lang])
            if p:
                out.append(_wiki_record(p, lang, h, topic, "encyclopedia article", "wikipedia"))
        except httpx.HTTPError as exc:
            log.warning("wikipedia %s:%s failed: %s", lang, links[lang], exc)
    for r in out:
        r["subject"] = subject
    return out


def wikipedia_search(query: str, lang: str = "en", limit: int = 2) -> list[str]:
    host = f"{lang}.wikipedia.org"
    data = _get(f"https://{host}/w/api.php", {"action": "query", "list": "search", "srsearch": query,
                                              "srlimit": str(limit), "format": "json", "formatversion": "2"})
    return [h["title"] for h in data.get("query", {}).get("search", [])]


def wikipedia_page(title: str, lang: str, subject: str | None = None) -> list[dict]:
    """A single page in any language; links back to the English topic when possible."""
    if lang == "en":
        return wikipedia_topic(title, subject=subject)
    host = f"{lang}.wikipedia.org"
    page = _mw_page(host, title, with_langlinks=True)
    if not page:
        return []
    en = next((ll["title"] for ll in page.get("langlinks", []) if ll["lang"] == "en"), None)
    if en:
        return wikipedia_topic(en, subject=subject)
    rec = _wiki_record(page, lang, host, page["title"], "encyclopedia article", "wikipedia")
    rec["subject"] = subject
    return [rec]


def wikibooks_search(query: str, limit: int = 1, subject: str | None = None) -> list[dict]:
    host = "en.wikibooks.org"
    data = _get(f"https://{host}/w/api.php", {"action": "query", "list": "search", "srsearch": query,
                                              "srlimit": str(limit), "srnamespace": "0", "format": "json",
                                              "formatversion": "2"})
    out = []
    for hit in data.get("query", {}).get("search", []):
        p = _mw_page(host, hit["title"])
        if p:
            r = _wiki_record(p, "en", host, query, "open textbook chapter", "wikibooks")
            r["subject"] = subject
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# Open Library (metadata for public-access books)
# ---------------------------------------------------------------------------
def openlibrary_search(query: str, limit: int = 3, subject: str | None = None) -> list[dict]:
    return _openlibrary(query, limit, subject, "public")


def openlibrary_borrow_search(query: str, limit: int = 3, subject: str | None = None) -> list[dict]:
    """Books that can be borrowed (controlled digital lending). Needs a free Internet Archive login."""
    return _openlibrary(query, limit, subject, "borrowable")


def _openlibrary(query: str, limit: int, subject: str | None, want: str) -> list[dict]:
    data = _get("https://openlibrary.org/search.json", {
        "q": query, "limit": "15",
        "fields": "key,title,author_name,first_publish_year,subject,first_sentence,ebook_access,language"})
    out = []
    for d in data.get("docs", []):
        if d.get("ebook_access") != want:
            continue  # "public": openly readable scans; "borrowable": lend-only
        subjects = ", ".join((d.get("subject") or [])[:12])
        first = d.get("first_sentence")
        first = first[0] if isinstance(first, list) and first else (first or "")
        lang = "hi" if "hin" in (d.get("language") or []) else "kn" if "kan" in (d.get("language") or []) else "en"
        text = f"{d['title']}. " + (f"First sentence: {first} " if first else "") + (f"Subjects: {subjects}." if subjects else "")
        if len(text) < 60:
            continue
        out.append({
            "key": f"openlibrary:{d['key']}",
            "topic_key": query,
            "title": d["title"],
            "source": "openlibrary",
            "access": "open" if want == "public" else "authorised",
            "kind": "book (catalogue record)" if want == "public" else "book (borrowable, free account)",
            "url": f"https://openlibrary.org{d['key']}",
            "lang": lang,
            "licence": ("Catalogue metadata CC0 1.0; book marked 'public' access on Internet Archive (check item rights)"
                        if want == "public" else
                        "Borrowable through Internet Archive controlled digital lending: free login required, one reader at a time"),
            "licence_url": "https://openlibrary.org/help/faq/using",
            "attribution": "Open Library / Internet Archive",
            "author": ", ".join((d.get("author_name") or [])[:3]) or None,
            "subject": subject,
            "text": text,
        })
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Project Gutenberg via Gutendex
# ---------------------------------------------------------------------------
def gutenberg_search(query: str, limit: int = 2, subject: str | None = None) -> list[dict]:
    data = _get("https://gutendex.com/books", {"search": query})
    out = []
    for b in data.get("results", []):
        if b.get("copyright") is not False:
            continue  # only books Gutenberg marks as not copyrighted
        summary = " ".join(b.get("summaries") or [])
        subjects = "; ".join(b.get("subjects") or [])
        text = f"{b['title']}. {summary} Subjects: {subjects}".strip()
        if len(text) < 80:
            continue
        lang = (b.get("languages") or ["en"])[0]
        out.append({
            "key": f"gutenberg:{b['id']}",
            "topic_key": query,
            "title": b["title"],
            "source": "gutenberg",
            "kind": "public-domain book",
            "url": f"https://www.gutenberg.org/ebooks/{b['id']}",
            "lang": lang if lang in ("en", "hi", "kn") else "en",
            "licence": "Public domain in the USA (Project Gutenberg); check local laws",
            "licence_url": "https://www.gutenberg.org/policy/license.html",
            "attribution": "Project Gutenberg",
            "author": ", ".join(a["name"] for a in (b.get("authors") or [])[:3]) or None,
            "subject": subject,
            "text": text[:3000],
        })
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# DOAJ (open-access journal articles)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=256)
def doaj_journal_licence(issn: str) -> tuple[str, str | None]:
    try:
        data = _get(f"https://doaj.org/api/search/journals/issn:{issn}")
        lic = data["results"][0]["bibjson"]["license"][0]
        return lic.get("type") or "Open access", lic.get("url")
    except Exception:
        return "Open access (DOAJ-listed journal); licence not stated in record", None


def doaj_search(query: str, limit: int = 2, subject: str | None = None) -> list[dict]:
    data = _get(f"https://doaj.org/api/search/articles/{urllib.parse.quote(query)}", {"pageSize": "10"})
    out = []
    for r in data.get("results", []):
        bj = r.get("bibjson", {})
        abstract = (bj.get("abstract") or "").strip()
        if len(abstract) < 200:
            continue
        link = next((l.get("url") for l in bj.get("link", []) if l.get("type") == "fulltext"), None)
        if not link:
            continue
        issn = next((i["id"] for i in bj.get("identifier", []) if i.get("type") in ("eissn", "pissn")), None)
        lic, lic_url = doaj_journal_licence(issn) if issn else ("Open access; licence not stated", None)
        langs = (bj.get("journal") or {}).get("language") or ["EN"]
        lang = {"HI": "hi", "KN": "kn"}.get(langs[0].upper(), "en")
        out.append({
            "key": f"doaj:{r.get('id')}",
            "topic_key": query,
            "title": bj.get("title", "Untitled"),
            "source": "doaj",
            "kind": "open-access journal article",
            "url": link,
            "lang": lang,
            "licence": lic,
            "licence_url": lic_url,
            "attribution": (bj.get("journal") or {}).get("title"),
            "author": ", ".join(a.get("name", "") for a in (bj.get("author") or [])[:3]) or None,
            "subject": subject,
            "text": f"{bj.get('title', '')}. {abstract}"[:3000],
        })
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# Research papers and more books. All are legal open-access sources; records
# without a stated licence are labelled "licence not stated" rather than guessed.
# ---------------------------------------------------------------------------
def _get_text(url: str, params: dict | None = None) -> str:
    with _client() as c:
        r = c.get(url, params=params)
        r.raise_for_status()
        return r.text


def _lang_ok(code: str | None) -> str | None:
    """Map to our three languages; skip other languages instead of mislabelling them."""
    c = (code or "en").lower()[:2]
    return c if c in ("en", "hi", "kn") else None


def arxiv_search(query: str, limit: int = 2, subject: str | None = None) -> list[dict]:
    import xml.etree.ElementTree as ET

    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(_get_text("https://export.arxiv.org/api/query",
                                   {"search_query": f"all:{query}", "max_results": str(limit + 2), "sortBy": "relevance"}))
    out = []
    for e in root.findall("a:entry", ns):
        title = " ".join((e.findtext("a:title", "", ns) or "").split())
        abstract = " ".join((e.findtext("a:summary", "", ns) or "").split())
        url = (e.findtext("a:id", "", ns) or "").replace("http://", "https://")
        if len(abstract) < 200 or not url:
            continue
        out.append({
            "key": f"arxiv:{url.rsplit('/abs/', 1)[-1]}", "topic_key": query, "title": title, "source": "arxiv",
            "kind": "preprint (abstract)", "url": url, "lang": "en",
            "licence": "arXiv metadata/abstract CC0; the paper's own licence is on its arXiv page (default: arXiv non-exclusive licence, free to read)",
            "licence_url": url, "attribution": "arXiv.org",
            "author": ", ".join(a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)[:3]) or None,
            "subject": subject, "text": f"{title}. {abstract}"[:3000]})
        if len(out) >= limit:
            break
    return out


def _openalex_abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    words: list[tuple[int, str]] = [(i, w) for w, pos in inv.items() for i in pos]
    return " ".join(w for _, w in sorted(words))


def openalex_search(query: str, limit: int = 3, subject: str | None = None) -> list[dict]:
    """OpenAlex indexes ~250M works; this keeps open-access ones with an abstract and a licence-bearing location."""
    return _openalex(query, limit, subject, open_access=True)


def openalex_paywalled_search(query: str, limit: int = 3, subject: str | None = None) -> list[dict]:
    """Subscription papers: we keep only the metadata + abstract OpenAlex publishes, and link to the publisher.
    Readers sign in with their own institution (or buy); GranthSetu never handles credentials or full text."""
    return _openalex(query, limit, subject, open_access=False)


def _openalex(query: str, limit: int, subject: str | None, open_access: bool) -> list[dict]:
    data = _get("https://api.openalex.org/works", {
        "search": query, "filter": f"open_access.is_oa:{'true' if open_access else 'false'},has_abstract:true", "per-page": "15",
        "mailto": settings.user_agent.split("mailto:")[-1] if "mailto:" in settings.user_agent else "granthsetu@example.org"})
    out = []
    for w in data.get("results", []):
        loc = (w.get("best_oa_location") if open_access else None) or w.get("primary_location") or {}
        lang = _lang_ok(w.get("language"))
        abstract = _openalex_abstract(w.get("abstract_inverted_index"))
        url = loc.get("landing_page_url") or (w.get("open_access") or {}).get("oa_url") or w.get("doi")
        if not lang or len(abstract) < 200 or not url:
            continue
        lic = loc.get("license") if open_access else None
        out.append({
            "key": f"openalex:{w['id'].rsplit('/', 1)[-1]}", "topic_key": query, "title": w.get("display_name") or "Untitled",
            "source": "openalex", "kind": "open-access paper (abstract)" if open_access else "subscription paper (abstract only)",
            "url": url, "lang": lang,
            "access": "open" if open_access else "authorised",
            "licence": (lic.upper().replace("-", " ") if lic else "Open access (licence not stated in OpenAlex record)")
            if open_access else "Subscription: full text via your institution's login or purchase at the publisher",
            "licence_url": f"https://creativecommons.org/licenses/{lic.replace('cc-', '')}/4.0/" if lic and lic.startswith("cc-") else None,
            "attribution": (loc.get("source") or {}).get("display_name"),
            "author": ", ".join((a.get("author") or {}).get("display_name", "") for a in (w.get("authorships") or [])[:3]) or None,
            "subject": subject, "text": f"{w.get('display_name', '')}. {abstract}"[:3000]})
        if len(out) >= limit:
            break
    return out


def europepmc_search(query: str, limit: int = 2, subject: str | None = None) -> list[dict]:
    data = _get("https://www.ebi.ac.uk/europepmc/webservices/rest/search", {
        "query": f"({query}) AND OPEN_ACCESS:y AND HAS_ABSTRACT:y", "format": "json", "resultType": "core", "pageSize": "10"})
    out = []
    for r in (data.get("resultList") or {}).get("result", []):
        abstract = " ".join((r.get("abstractText") or "").replace("<", " <").split())
        import re as _re
        abstract = _re.sub(r"<[^>]+>", "", abstract).strip()
        if len(abstract) < 200 or not r.get("id"):
            continue
        out.append({
            "key": f"europepmc:{r.get('source')}:{r['id']}", "topic_key": query, "title": r.get("title", "Untitled").rstrip("."),
            "source": "europepmc", "kind": "open-access biomedical paper (abstract)",
            "url": f"https://europepmc.org/article/{r.get('source')}/{r['id']}", "lang": "en",
            "licence": (r.get("license") or "Open access (licence not stated)").upper(), "licence_url": None,
            "attribution": r.get("journalTitle"), "author": r.get("authorString"), "subject": subject,
            "text": f"{r.get('title', '')}. {abstract}"[:3000]})
        if len(out) >= limit:
            break
    return out


def internetarchive_search(query: str, limit: int = 2, subject: str | None = None) -> list[dict]:
    data = _get("https://archive.org/advancedsearch.php", {
        "q": f"({query}) AND mediatype:texts AND licenseurl:*", "fl[]": ["identifier", "title", "description", "licenseurl", "language", "creator"],
        "rows": "10", "output": "json"})
    out = []
    for d in (data.get("response") or {}).get("docs", []):
        desc = d.get("description")
        desc = " ".join(desc) if isinstance(desc, list) else (desc or "")
        import re as _re
        desc = _re.sub(r"<[^>]+>", " ", desc)
        desc = " ".join(desc.split())
        lang = _lang_ok(d["language"][0] if isinstance(d.get("language"), list) else d.get("language"))
        lic = d.get("licenseurl")
        if len(desc) < 120 or not lic or not lang:
            continue
        creator = d.get("creator")
        out.append({
            "key": f"ia:{d['identifier']}", "topic_key": query, "title": d.get("title") or d["identifier"], "source": "internetarchive",
            "kind": "open text (Internet Archive)", "url": f"https://archive.org/details/{d['identifier']}", "lang": lang,
            "licence": "See licence link (set by the uploader)", "licence_url": lic if isinstance(lic, str) else lic[0],
            "attribution": "Internet Archive", "author": ", ".join(creator) if isinstance(creator, list) else creator,
            "subject": subject, "text": f"{d.get('title', '')}. {desc}"[:3000]})
        if len(out) >= limit:
            break
    return out


def wikisource_search(query: str, limit: int = 1, subject: str | None = None) -> list[dict]:
    """Public-domain and freely licensed source texts in English, Hindi and Kannada."""
    out = []
    for lang in ("en", "hi", "kn"):
        host = f"{lang}.wikisource.org"
        try:
            hits = _get(f"https://{host}/w/api.php", {"action": "query", "list": "search", "srsearch": query, "srlimit": str(limit),
                                                       "srnamespace": "0", "format": "json", "formatversion": "2"})["query"]["search"]
            for h in hits:
                p = _mw_page(host, h["title"])
                if p and len(p["extract"]) > 200:
                    r = _wiki_record(p, lang, host, query, "source text (Wikisource)", "wikisource")
                    r["subject"] = subject
                    out.append(r)
        except Exception as exc:  # one language failing must not stop the others
            log.warning("wikisource %s failed: %s", lang, exc)
    return out


def googlebooks_search(query: str, limit: int = 3, subject: str | None = None) -> list[dict]:
    """Books on sale. We store the publisher's description and the store link and price; nothing else."""
    import os

    params = {"q": query, "maxResults": "20", "printType": "books", "orderBy": "relevance", "country": "IN"}
    if os.getenv("GOOGLE_BOOKS_API_KEY"):
        params["key"] = os.environ["GOOGLE_BOOKS_API_KEY"]
    data = _get("https://www.googleapis.com/books/v1/volumes", params)
    out = []
    for it in data.get("items", []):
        vi, sale = it.get("volumeInfo", {}), it.get("saleInfo", {})
        desc = " ".join((vi.get("description") or "").split())
        lang = _lang_ok(vi.get("language"))
        url = sale.get("buyLink") or vi.get("infoLink")
        if sale.get("saleability") != "FOR_SALE" or len(desc) < 120 or not lang or not url:
            continue
        price = sale.get("listPrice") or {}
        price_txt = f" ({price.get('currencyCode', '')} {price.get('amount')})" if price.get("amount") is not None else ""
        out.append({
            "key": f"googlebooks:{it['id']}", "topic_key": query, "title": vi.get("title", "Untitled"), "source": "googlebooks",
            "access": "paid", "kind": "book for sale (Google Play Books)", "url": url, "lang": lang,
            "licence": f"Commercial: buy at Google Play Books{price_txt}", "licence_url": vi.get("infoLink"),
            "attribution": vi.get("publisher") or "Google Books", "author": ", ".join((vi.get("authors") or [])[:3]) or None,
            "subject": subject, "text": f"{vi.get('title', '')}. {desc}"[:3000]})
        if len(out) >= limit:
            break
    return out


ADAPTERS = {
    "wikibooks": wikibooks_search,
    "openlibrary": openlibrary_search,
    "gutenberg": gutenberg_search,
    "doaj": doaj_search,
    "arxiv": arxiv_search,
    "openalex": openalex_search,
    "europepmc": europepmc_search,
    "internetarchive": internetarchive_search,
    "wikisource": wikisource_search,
    "openalex_paywalled": openalex_paywalled_search,
    "openlibrary_borrow": openlibrary_borrow_search,
    "googlebooks": googlebooks_search,
}
# Used for live "fill the gap" fetches and ad-hoc topics: breadth over speed.
LIVE_SOURCES = ["wikibooks", "wikisource", "openlibrary", "internetarchive", "doaj", "openalex", "arxiv", "europepmc",
                "openalex_paywalled", "openlibrary_borrow", "googlebooks"]
