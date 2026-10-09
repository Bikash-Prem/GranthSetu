import { useEffect, useState } from "react";
import { api, ApiError, LANGS, type Result, type SearchResponse } from "../api";

export type LessonRequest = {
  query: string;
  lang: string;
  ids: number[];
  explanation: SearchResponse["explanation"];
  results: Result[];
};

type Pack = {
  questions: { pid: number; question: string; answer: string; quote: string; verified: boolean }[];
  questions_status: string;
  passages: { pid: number; rid: number; title: string; text: string }[];
};

export default function LessonPage({ req }: { req: LessonRequest | null }) {
  const [pack, setPack] = useState<Pack | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!req) return;
    api.post<Pack>("/lesson-pack", { query: req.query, resource_ids: req.ids, lang: req.lang })
      .then(setPack)
      .catch((e) => setErr(e instanceof ApiError ? e.message : "Could not build the lesson pack."));
  }, [req]);

  if (!req) return <div className="card empty" style={{ marginTop: 40 }}>Pick results on the Search page first, then choose “Make lesson pack”.</div>;
  const kept = req.explanation.sentences.filter((s) => s.verified);
  const titleOf = (pid: number) => pack?.passages.find((p) => p.pid === pid)?.title;

  return (
    <div className="stack">
      <div className="spread noprint" style={{ marginTop: 28 }}>
        <a className="btn" href="#/">← Back to results</a>
        <button className="btn primary" onClick={() => window.print()}>Print / save as PDF</button>
      </div>
      <div className="card">
        <div className="tiny muted">GranthSetu lesson pack · {LANGS[req.lang] || req.lang} · {new Date().toLocaleDateString()}</div>
        <h1 style={{ marginTop: 8 }}>{req.query}</h1>
        {kept.length > 0 && (
          <>
            <h2>Key ideas (verified against sources)</h2>
            <ul>{kept.map((s, i) => <li key={i}>{s.display_text}</li>)}</ul>
          </>
        )}
        <h2 style={{ marginTop: 18 }}>Read these open resources</h2>
        <ol>
          {req.results.map((r) => (
            <li key={r.id} style={{ marginBottom: 8 }}>
              <strong>{r.title}</strong> ({r.lang_name}, {r.source})<br />
              <span className="small">{r.url}</span><br />
              <span className="tiny muted">Licence: {r.licence}{r.attribution ? ` · ${r.attribution}` : ""}</span>
            </li>
          ))}
        </ol>
        <h2 style={{ marginTop: 18 }}>Practice questions</h2>
        {err && <div className="err">{err}</div>}
        {!pack && !err && <p className="muted small">Writing questions from the sources…</p>}
        {pack && pack.questions.length === 0 && (
          <p className="small muted">
            {pack.questions_status === "skipped" ? "Practice questions need Gemma (AI is off)." : "No question passed source verification, so none are shown."}
          </p>
        )}
        {pack && pack.questions.length > 0 && (
          <ol>
            {pack.questions.map((q, i) => (
              <li key={i} style={{ marginBottom: 10 }}>
                {q.question}
                <div className="small muted">Answer: {q.answer} <span className="tiny">(from “{titleOf(q.pid)}”: “{q.quote}”)</span></div>
              </li>
            ))}
          </ol>
        )}
      </div>
    </div>
  );
}
