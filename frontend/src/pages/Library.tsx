import { useEffect, useState } from "react";
import { api, ApiError, LANGS, subscribe, type LiveEvent } from "../api";

type Res = { id: number; title: string; source: string; kind: string; url: string; lang: string; licence: string; fetched_at: string; access: string };
type Lib = { resources: Res[]; stats: { resources_by_lang: Record<string, number>; resources_by_source: Record<string, number>; passages: number } };

function describe(e: LiveEvent): string {
  if (e.type === "index_updated") return `Index v${e.version}: +${e.added} resources${e.topic ? ` for “${e.topic}”` : ""}`;
  if (e.type === "job") {
    const extra = e.progress ? ` ${e.progress}` : "";
    const topic = e.topic ? ` · ${e.topic}` : "";
    return `Job ${String(e.kind || "")} ${e.status}${extra}${topic}${e.added !== undefined && e.status === "done" ? ` · added ${e.added}` : ""}`;
  }
  return e.type;
}

export default function LibraryPage() {
  const [lib, setLib] = useState<Lib | null>(null);
  const [lang, setLang] = useState("");
  const [feed, setFeed] = useState<{ t: string; e: LiveEvent }[]>([]);
  const [topic, setTopic] = useState("");
  const [msg, setMsg] = useState<string | null>(null);

  const load = () => api.get<Lib>(`/resources?limit=50${lang ? `&lang=${lang}` : ""}`).then(setLib).catch(() => setLib(null));
  useEffect(() => { load(); }, [lang]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => subscribe((e) => {
    setFeed((f) => [{ t: new Date().toLocaleTimeString(), e }, ...f].slice(0, 60));
    if (e.type === "index_updated") load();
  }), []); // eslint-disable-line react-hooks/exhaustive-deps

  const request = async () => {
    try {
      const r = await api.post<{ job_id: string }>("/ingest/topic", { topic: topic.trim() });
      setMsg(`Queued live fetch (job ${r.job_id}). Watch the live feed.`);
      setTopic("");
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : "Could not queue the request.");
    }
  };

  const total = lib ? Object.values(lib.stats.resources_by_lang).reduce((a, b) => a + b, 0) : 0;

  return (
    <div className="stack">
      <h1>Live library</h1>
      <p className="lede" style={{ marginBottom: 0 }}>
        Resources are pulled live from Wikipedia, Wikibooks, Open Library, Project Gutenberg and DOAJ through a job queue, then
        indexed and pushed to every server instantly. Nothing here is hand-written.
      </p>
      <div className="grid3">
        <div className="card"><div className="lbl">Resources</div><div className="stat">{total}</div></div>
        <div className="card"><div className="lbl">Searchable passages</div><div className="stat">{lib?.stats.passages ?? "—"}</div></div>
        <div className="card">
          <div className="lbl">By language</div>
          {lib && Object.entries(lib.stats.resources_by_lang).map(([l, n]) => <div key={l} className="spread small"><span>{LANGS[l] || l}</span><span>{n}</span></div>)}
        </div>
        <div className="card">
          <div className="lbl">By source</div>
          {lib && Object.entries(lib.stats.resources_by_source).map(([s, n]) => <div key={s} className="spread small"><span>{s}</span><span>{n}</span></div>)}
        </div>
      </div>

      <div className="grid2">
        <div className="card stack">
          <h2 style={{ margin: 0 }}>Ask the library to fetch a topic</h2>
          <div className="row">
            <input className="field" style={{ flex: 1 }} placeholder="e.g. Tipu Sultan, Fractions, Acid rain" value={topic} maxLength={120}
              onChange={(e) => setTopic(e.target.value)} onKeyDown={(e) => e.key === "Enter" && topic.trim().length > 1 && request()} />
            <button className="btn primary" disabled={topic.trim().length < 2} onClick={request}>Fetch live</button>
          </div>
          {msg && <div className="small muted">{msg}</div>}
        </div>
        <div className="card">
          <h2>Live feed</h2>
          <div className="feed" aria-live="polite">
            {feed.length === 0 && <div className="small muted">Waiting for events… (ingestion jobs and index updates appear here)</div>}
            {feed.map((f, i) => <div key={i} className="ev"><span className="muted mono">{f.t}</span> {describe(f.e)}</div>)}
          </div>
        </div>
      </div>

      <div className="card">
        <div className="spread">
          <h2 style={{ margin: 0 }}>Recently added</h2>
          <select className="field" value={lang} onChange={(e) => setLang(e.target.value)} aria-label="Filter language">
            <option value="">All languages</option>
            {Object.entries(LANGS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </div>
        {lib && lib.resources.length === 0 && <div className="empty">The library is empty. The seed job fills it from live sources on first start; this needs internet access.</div>}
        {lib && lib.resources.length > 0 && (
          <div className="scroll"><table>
            <thead><tr><th>Title</th><th>Lang</th><th>Access</th><th>Source</th><th>Licence</th><th>Fetched</th></tr></thead>
            <tbody>{lib.resources.map((r) => (
              <tr key={r.id}>
                <td><a href={r.url} target="_blank" rel="noreferrer">{r.title}</a><div className="tiny muted">{r.kind}</div></td>
                <td>{LANGS[r.lang] || r.lang}</td><td><span className={`badge ${r.access === "open" ? "ok" : r.access === "paid" ? "warn" : "info"}`}>{r.access}</span></td><td>{r.source}</td><td className="tiny">{r.licence}</td>
                <td className="tiny muted">{r.fetched_at.replace("T", " ").slice(0, 16)}</td>
              </tr>))}
            </tbody></table></div>
        )}
      </div>
    </div>
  );
}
