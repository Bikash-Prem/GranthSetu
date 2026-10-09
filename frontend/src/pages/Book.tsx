import { useEffect, useState } from "react";
import { api, ApiError, LANGS, POLICY_LABEL, type AccessInfo } from "../api";
import { useSession } from "../session";

type Card = {
  id: number; title: string; subtitle: string | null; author: string | null; lang: string; kind: string; source: string;
  publisher: string | null; pub_year: number | null; subject: string | null; licence: string; licence_url: string | null;
  url: string | null; origin: string; access: AccessInfo;
};
type Book = Card & {
  description: string | null; categories: string | null; isbn: string | null; edition: string | null; rights_holder: string | null;
  attribution: string | null; status: string; published_at: string | null;
  chapters: { chapter_no: number; title: string; start_page: number; end_page: number }[];
  pages: number; excerpts: string[]; file: { mime: string; size_bytes: number } | null;
  progress_page?: number | null; bookmarks?: { id: number; page_no: number; note: string | null }[];
};
type Page = { page_no: number; chapter_no: number | null; text: string; ocr: boolean; pages: number };

export function PolicyBadge({ a }: { a: AccessInfo }) {
  const [cls, label] = POLICY_LABEL[a.policy] || ["", a.policy];
  return <span className={`badge ${cls}`} title={a.policy_text}>{label}</span>;
}

export default function BookPage({ id, startPage }: { id: number; startPage: number | null }) {
  const { user } = useSession();
  const [b, setB] = useState<Book | null>(null);
  const [err, setErr] = useState<{ status: number; msg: string } | null>(null);
  const [page, setPage] = useState<Page | null>(null);
  const [pageErr, setPageErr] = useState<string | null>(null);
  const [size, setSize] = useState(() => { try { return Number(localStorage.getItem("gs_text_size")) || 17; } catch { return 17; } });
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<{ page_no: number; snippet: string }[] | null>(null);
  const [note, setNote] = useState("");

  const load = () => api.get<Book>(`/books/${id}`).then(setB).catch((e) => setErr({ status: e instanceof ApiError ? e.status : 0, msg: e.message }));
  useEffect(() => { setB(null); setErr(null); setPage(null); load(); }, [id, user?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const open = async (n: number) => {
    setPageErr(null);
    try {
      const p = await api.get<Page>(`/books/${id}/pages/${n}`);
      setPage(p);
      if (user) api.put(`/me/progress/${id}`, { page_no: n }).catch(() => undefined);
      window.scrollTo({ top: 0 });
    } catch (e) {
      setPageErr(e instanceof ApiError ? e.message : "Could not load the page.");
    }
  };
  useEffect(() => {
    if (b?.access.can_read && b.pages > 0 && !page) open(startPage || b.progress_page || 1);
  }, [b]); // eslint-disable-line react-hooks/exhaustive-deps

  const setText = (n: number) => { setSize(n); try { localStorage.setItem("gs_text_size", String(n)); } catch { /* optional */ } };
  const search = () => api.get<{ hits: { page_no: number; snippet: string }[] }>(`/books/${id}/search?q=${encodeURIComponent(q)}`)
    .then((r) => setHits(r.hits)).catch((e) => setPageErr(e.message));
  const bookmark = () => page && api.post("/me/bookmarks", { resource_id: id, page_no: page.page_no, note }).then(() => { setNote(""); load(); });

  if (err) {
    return (
      <div className="card empty" style={{ marginTop: 40 }}>
        {err.status === 404 ? "This resource does not exist, or you do not have permission to see it." : err.msg}
      </div>
    );
  }
  if (!b) return <div className="empty">Loading…</div>;

  const a = b.access;
  const chapter = page && b.chapters.find((c) => c.chapter_no === page.chapter_no);
  return (
    <div className="stack">
      <div>
        <h1 style={{ marginBottom: 4 }}>{b.title}</h1>
        {b.subtitle && <div className="lede" style={{ margin: 0 }}>{b.subtitle}</div>}
        <div className="row" style={{ gap: 6, marginTop: 10 }}>
          <PolicyBadge a={a} />
          <span className="badge info">{LANGS[b.lang] || b.lang}</span>
          {b.author && <span className="badge">{b.author}</span>}
          {b.publisher && <span className="badge">{b.publisher}{b.pub_year ? `, ${b.pub_year}` : ""}</span>}
          <span className="badge">{b.kind}</span>
          {b.status !== "PUBLISHED" && <span className="badge bad">{b.status} (staff view)</span>}
        </div>
      </div>

      <div className="grid2">
        <div className="card stack">
          <div className="lbl">Availability</div>
          <div><b>{a.can_read ? "You can read this here." : "You cannot read the full text here."}</b></div>
          <div className="small muted">{a.policy_text}</div>
          {!a.can_read && <div className="small">{a.reason}{(a.needs_login || (!user && !["EXTERNAL_PROVIDER_ACCESS", "OPEN_ACCESS"].includes(a.policy))) && <> · <a href={`#/login?next=/book/${id}`}>Sign in{a.needs_login ? "" : " if you have been given access"}</a></>}</div>}
          {a.external_url && (
            <a className="btn" style={{ justifySelf: "start" }} href={a.external_url} target="_blank" rel="noreferrer noopener">
              Go to the provider ↗
            </a>
          )}
          {a.external_url && <div className="tiny muted">This is a link to the provider's own access flow, not an integrated reader.</div>}
          {a.can_download && b.file
            ? <a className="btn" style={{ justifySelf: "start" }} href={`/api/books/${id}/download`}>Download ({Math.ceil(b.file.size_bytes / 1024)} KB)</a>
            : a.can_read && <div className="tiny muted">Downloads are not permitted by this resource's licence.</div>}
        </div>
        <div className="card stack">
          <div className="lbl">Licence and source</div>
          <div className="small">{b.licence_url ? <a href={b.licence_url} target="_blank" rel="noreferrer">{b.licence}</a> : b.licence || "Not stated"}</div>
          {b.rights_holder && <div className="small">Rights holder: {b.rights_holder}</div>}
          {b.attribution && <div className="small">{b.attribution}</div>}
          <div className="small muted">Source: {b.source}{b.url && !a.external_url ? <> · <a href={b.url} target="_blank" rel="noreferrer">original ↗</a></> : null}</div>
          {b.isbn && <div className="small muted">ISBN {b.isbn}{b.edition ? ` · ${b.edition}` : ""}</div>}
        </div>
      </div>

      {b.description && <div className="card"><div className="lbl">About</div><p style={{ margin: 0, whiteSpace: "pre-wrap" }}>{b.description}</p></div>}

      {a.can_read && b.pages === 0 && b.excerpts.length > 0 && (
        <div className="card stack">
          <div className="lbl">Excerpts held by GranthSetu</div>
          {b.excerpts.map((t, i) => <div key={i} className="passage">{t}</div>)}
          {b.url && <a href={b.url} target="_blank" rel="noreferrer">Read the full text at the source ↗</a>}
        </div>
      )}

      {a.can_read && b.pages > 0 && (
        <div className="reader">
          <aside className="card stack reader-side" aria-label="Contents">
            <div className="lbl">Contents</div>
            {b.chapters.map((c) => (
              <button key={c.chapter_no} className={`btn ghost toc ${page?.chapter_no === c.chapter_no ? "on" : ""}`} onClick={() => open(c.start_page)}>
                {c.title} <span className="tiny muted">p. {c.start_page}</span>
              </button>
            ))}
            <div className="lbl" style={{ marginTop: 8 }}>Search in this book</div>
            <div className="row" style={{ flexWrap: "nowrap" }}>
              <input className="field" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && q.trim().length > 1 && search()} aria-label="Search in this book" />
              <button className="btn" disabled={q.trim().length < 2} onClick={search}>Go</button>
            </div>
            {hits && hits.length === 0 && <div className="tiny muted">No matches.</div>}
            {hits?.map((h, i) => (
              <button key={i} className="btn ghost toc" onClick={() => open(h.page_no)}><span className="tiny">p. {h.page_no}: …{h.snippet}…</span></button>
            ))}
            {user && b.bookmarks && b.bookmarks.length > 0 && <>
              <div className="lbl" style={{ marginTop: 8 }}>Your bookmarks</div>
              {b.bookmarks.map((bm) => <button key={bm.id} className="btn ghost toc" onClick={() => open(bm.page_no)}>p. {bm.page_no}{bm.note ? ` · ${bm.note}` : ""}</button>)}
            </>}
          </aside>
          <article className="card stack">
            <div className="spread noprint">
              <div className="row">
                <button className="btn" disabled={!page || page.page_no <= 1} onClick={() => page && open(page.page_no - 1)} aria-label="Previous page">←</button>
                <span className="small">Page {page?.page_no ?? "…"} of {b.pages}</span>
                <button className="btn" disabled={!page || page.page_no >= b.pages} onClick={() => page && open(page.page_no + 1)} aria-label="Next page">→</button>
              </div>
              <div className="row">
                <span className="tiny muted">Text size</span>
                <button className="btn" onClick={() => setText(Math.max(13, size - 2))} aria-label="Smaller text">A−</button>
                <button className="btn" onClick={() => setText(Math.min(28, size + 2))} aria-label="Larger text">A+</button>
              </div>
            </div>
            {chapter && <div className="tiny muted">{chapter.title}</div>}
            {pageErr && <div className="err">{pageErr}</div>}
            {page && <div className="pagetext" lang={b.lang} style={{ fontSize: size }}>{page.text || <span className="muted">This page has no readable text.</span>}</div>}
            {page?.ocr && <div className="tiny muted">This page was read with OCR and may contain recognition errors.</div>}
            {user && page && (
              <div className="row noprint">
                <input className="field" style={{ flex: 1 }} placeholder="Note for this bookmark (optional)" value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} aria-label="Bookmark note" />
                <button className="btn" onClick={bookmark}>Bookmark page {page.page_no}</button>
              </div>
            )}
          </article>
        </div>
      )}
    </div>
  );
}

type Cat = { items: Card[]; page: number; pages: number; total: number };

export function CataloguePage() {
  const { user } = useSession();
  const [q, setQ] = useState("");
  const [lang, setLang] = useState("");
  const [origin, setOrigin] = useState("managed");
  const [sort, setSort] = useState("recent");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Cat | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const t = window.setTimeout(() => {
      setErr(null);
      api.get<Cat>(`/catalogue?q=${encodeURIComponent(q)}&lang=${lang}&origin=${origin}&sort=${sort}&page=${page}`)
        .then(setData).catch((e) => setErr(e.message));
    }, 200);
    return () => window.clearTimeout(t);
  }, [q, lang, origin, sort, page, user?.id]);

  return (
    <div className="stack">
      <h1>Digital library</h1>
      <p className="lede" style={{ marginBottom: 0 }}>
        Books and documents added by librarians, plus records harvested from open libraries. You see what your account is allowed to discover;
        each card says whether you can read it here.
      </p>
      <div className="card row">
        <input className="field" style={{ flex: "1 1 220px" }} placeholder="Title, author or subject" value={q} onChange={(e) => { setQ(e.target.value); setPage(1); }} aria-label="Filter the catalogue" />
        <select className="field" value={origin} onChange={(e) => { setOrigin(e.target.value); setPage(1); }} aria-label="Collection">
          <option value="managed">Library collection</option>
          <option value="harvest">Harvested open records</option>
          <option value="">Everything</option>
        </select>
        <select className="field" value={lang} onChange={(e) => { setLang(e.target.value); setPage(1); }} aria-label="Language">
          <option value="">All languages</option>
          {Object.entries(LANGS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>
        <select className="field" value={sort} onChange={(e) => setSort(e.target.value)} aria-label="Sort">
          <option value="recent">Newest</option><option value="title">Title</option><option value="year">Year</option>
        </select>
      </div>
      {err && <div className="err">{err}</div>}
      {data && data.items.length === 0 && <div className="card empty">Nothing here yet{origin === "managed" ? ". Librarians add books from Manage → Add resource." : "."}</div>}
      <div className="grid2">
        {data?.items.map((c) => (
          <a key={c.id} href={`#/book/${c.id}`} className="card result" style={{ textDecoration: "none" }}>
            <div className="title">{c.title}</div>
            <div className="small muted">{[c.author, c.publisher, c.pub_year].filter(Boolean).join(" · ")}</div>
            <div className="row" style={{ gap: 6 }}>
              <PolicyBadge a={c.access} />
              <span className="badge info">{LANGS[c.lang] || c.lang}</span>
              <span className={`badge ${c.access.can_read ? "ok" : ""}`}>{c.access.can_read ? "You can read" : c.access.needs_login ? "Sign in to read" : "Catalogue only"}</span>
            </div>
          </a>
        ))}
      </div>
      {data && data.pages > 1 && (
        <div className="row" style={{ justifyContent: "center" }}>
          <button className="btn" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button>
          <span className="small">Page {data.page} of {data.pages} · {data.total} records</span>
          <button className="btn" disabled={page >= data.pages} onClick={() => setPage(page + 1)}>Next</button>
        </div>
      )}
    </div>
  );
}
