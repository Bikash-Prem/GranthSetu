import { useEffect, useRef, useState } from "react";
import { api, ApiError, LANGS, POLICY_LABEL, subscribe, type Result, type SearchResponse } from "../api";
import type { LessonRequest } from "./Lesson";

const EXAMPLES = [
  "ದ್ಯುತಿಸಂಶ್ಲೇಷಣೆ ಎಂದರೇನು?",
  "जल चक्र को समझाइए",
  "How do earthquakes happen?",
  "ವಿಜಯನಗರ ಸಾಮ್ರಾಜ್ಯದ ಇತಿಹಾಸ",
  "महात्मा गांधी के बारे में बताइए",
];

const STAGES = ["Understand", "Expand", "Retrieve", "Rerank", "Explain", "Verify"];
const MODES: [string, string][] = [
  ["agent", "Agent (hybrid + Gemma)"],
  ["hybrid", "Hybrid only"],
  ["keyword", "Keyword (BM25)"],
  ["semantic", "Meaning (embeddings)"],
];
const VOICE: [string, string][] = [
  ["kn-IN", "ಕನ್ನಡ"],
  ["hi-IN", "हिन्दी"],
  ["en-IN", "English"],
];

const MAX_REC_MS = 20000;

// The server transcribes 16 kHz mono WAV; browsers record webm/ogg, so convert here.
async function toWav(blob: Blob): Promise<Blob> {
  const AC = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
  const ctx = new AC();
  const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
  void ctx.close();
  const rate = 16000;
  const off = new OfflineAudioContext(1, Math.max(1, Math.ceil(decoded.duration * rate)), rate);
  const src = off.createBufferSource();
  src.buffer = decoded;
  src.connect(off.destination);
  src.start();
  const pcm = (await off.startRendering()).getChannelData(0);
  const buf = new ArrayBuffer(44 + pcm.length * 2);
  const v = new DataView(buf);
  const str = (o: number, s: string) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  str(0, "RIFF"); v.setUint32(4, 36 + pcm.length * 2, true); str(8, "WAVE"); str(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  str(36, "data"); v.setUint32(40, pcm.length * 2, true);
  for (let i = 0; i < pcm.length; i++) v.setInt16(44 + i * 2, Math.max(-1, Math.min(1, pcm[i])) * 0x7fff, true);
  return new Blob([buf], { type: "audio/wav" });
}

// Keep the last answer so "Back to results" from the lesson pack doesn't lose it.
let lastResponse: SearchResponse | null = null;

export default function SearchPage({ initial, onLesson }: { initial: string; onLesson: (l: LessonRequest) => void }) {
  const [q, setQ] = useState(initial || lastResponse?.query || "");
  const [mode, setMode] = useState("agent");
  const [loading, setLoading] = useState(false);
  const [stage, setStage] = useState(0);
  const [res, setResState] = useState<SearchResponse | null>(initial ? null : lastResponse);
  const setRes = (r: SearchResponse | null) => { lastResponse = r; setResState(r); };
  const [err, setErr] = useState<string | null>(null);
  const [voiceLang, setVoiceLang] = useState("kn-IN");
  const [listening, setListening] = useState(false);
  const [voiceNote, setVoiceNote] = useState<string | null>(null);
  const [picked, setPicked] = useState<number[]>([]);
  const [liveMsg, setLiveMsg] = useState<string | null>(null);
  const [transcribing, setTranscribing] = useState(false);
  const recRef = useRef<MediaRecorder | null>(null);
  const lastQuery = useRef("");

  const run = async (query = q, m = mode) => {
    const text = query.trim();
    if (text.length < 2) return;
    lastQuery.current = text;
    setLoading(true);
    setErr(null);
    setStage(0);
    setPicked([]);
    setLiveMsg(null);
    const timer = window.setInterval(() => setStage((s) => Math.min(s + 1, STAGES.length - 1)), 900);
    try {
      const r = await api.post<SearchResponse>("/search", { query: text, mode: m, explain: true });
      setRes(r);
      if (r.live_fetch_job) setLiveMsg("Searching open libraries live for this topic…");
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Could not reach the GranthSetu server.");
    } finally {
      window.clearInterval(timer);
      setLoading(false);
    }
  };

  useEffect(() => {
    if (initial) run(initial);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initial]);

  // When the agent started a live fetch, wait for it and re-run the search automatically.
  useEffect(() => {
    const job = res?.live_fetch_job;
    if (!job) return;
    return subscribe((e) => {
      if (e.job_id !== job || e.type !== "job") return;
      if (e.status === "done") {
        const added = Number(e.added || 0);
        if (added > 0) {
          setLiveMsg(`Found ${added} new open resource${added > 1 ? "s" : ""}. Searching again…`);
          window.setTimeout(() => run(lastQuery.current, mode), 1200);
        } else {
          const errs = (e.errors as string[] | undefined) || [];
          setLiveMsg(errs.length ? "Live fetch could not reach the open libraries right now. The topic stays on the Gap Board." :
            "Open libraries had nothing suitable either. The topic stays on the Gap Board for contributors.");
        }
      } else if (e.status === "failed") {
        setLiveMsg("Live fetch failed. The topic stays on the Gap Board.");
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [res?.live_fetch_job]);

  const startVoice = async () => {
    if (listening) {
      recRef.current?.stop();
      return;
    }
    if (transcribing) return;
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setVoiceNote("This browser can't record audio. Please type instead.");
      return;
    }
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      const name = (e as DOMException).name;
      setVoiceNote(
        name === "NotAllowedError" ? "Microphone permission was denied. Allow the microphone for this site in the address bar, then try again."
        : name === "NotFoundError" ? "No microphone was found. Check that one is connected and enabled in Windows sound settings."
        : "Could not open the microphone. Close other apps that are using it and try again.");
      return;
    }
    const lang = voiceLang.slice(0, 2);
    const chunks: Blob[] = [];
    const rec = new MediaRecorder(stream);
    const autoStop = window.setTimeout(() => { if (rec.state === "recording") rec.stop(); }, MAX_REC_MS);
    rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
    rec.onstop = async () => {
      window.clearTimeout(autoStop);
      stream.getTracks().forEach((t) => t.stop());
      setListening(false);
      setTranscribing(true);
      setVoiceNote("Transcribing your question…");
      try {
        const wav = await toWav(new Blob(chunks, { type: rec.mimeType }));
        const r = await api.upload<{ text: string }>(`/transcribe?lang=${lang}`, new File([wav], "question.wav", { type: "audio/wav" }));
        if (r.text) {
          setQ(r.text);
          setVoiceNote("Check the transcript, edit if needed, then search.");
        } else {
          setVoiceNote("No speech was heard. Press the mic and try again.");
        }
      } catch (e) {
        setVoiceNote(e instanceof ApiError ? e.message : "Could not process the recording. You can type instead.");
      } finally {
        setTranscribing(false);
      }
    };
    recRef.current = rec;
    rec.start();
    setListening(true);
    setVoiceNote("Recording… press the mic again when you finish (20 seconds max).");
  };

  const toggle = (id: number) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : p.length < 5 ? [...p, id] : p));

  return (
    <div>
      <h1>Learn from open libraries, in your language.</h1>
      <p className="lede">
        Ask in ಕನ್ನಡ, हिन्दी or English. GranthSetu searches openly licensed books, encyclopaedias and journals, explains what it finds, and
        checks every sentence against the source before showing it.
      </p>

      <div className="searchbox">
        <textarea
          aria-label="Your question"
          placeholder="Type or speak your question…"
          value={q}
          rows={2}
          maxLength={500}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              run();
            }
          }}
        />
        <div className="bar">
          <button className={`btn icon ${listening ? "rec" : ""}`} onClick={startVoice} disabled={transcribing} title={listening ? "Stop recording" : transcribing ? "Transcribing…" : "Speak"} aria-label="Voice input">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="9" y="2" width="6" height="12" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v4" /></svg>
          </button>
          <select className="field" value={voiceLang} onChange={(e) => setVoiceLang(e.target.value)} aria-label="Voice language">
            {VOICE.map(([v, l]) => <option key={v} value={v}>Speak: {l}</option>)}
          </select>
          <select className="field" value={mode} onChange={(e) => setMode(e.target.value)} aria-label="Search mode">
            {MODES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
          <span style={{ flex: 1 }} />
          <a className="btn ghost" href="#/scan">📷 Scan a page</a>
          <button className="btn primary" disabled={loading || q.trim().length < 2} onClick={() => run()}>
            {loading ? "Searching…" : "Search"}
          </button>
        </div>
      </div>
      {voiceNote && <p className="small muted">{voiceNote}</p>}
      {!res && !loading && (
        <div className="chips">
          {EXAMPLES.map((e) => (
            <button key={e} className="chip" onClick={() => { setQ(e); run(e); }}>{e}</button>
          ))}
        </div>
      )}

      {loading && (
        <div className="steps" aria-live="polite">
          {STAGES.map((s, i) => (
            <span key={s} className={`step ${i < stage ? "done" : i === stage ? "on" : ""}`}>{s}</span>
          ))}
        </div>
      )}
      {err && <div className="err" style={{ marginTop: 16 }}>{err}</div>}
      {res && !loading && <Results res={res} picked={picked} toggle={toggle} liveMsg={liveMsg}
        onLesson={() => onLesson({ query: res.query, lang: res.language, ids: picked, explanation: res.explanation, results: res.results.filter((r) => picked.includes(r.id)) })} />}
    </div>
  );
}

function Results({ res, picked, toggle, liveMsg, onLesson }: { res: SearchResponse; picked: number[]; toggle: (id: number) => void; liveMsg: string | null; onLesson: () => void }) {
  const kept = res.explanation.sentences.filter((s) => s.verified);
  const dropped = res.explanation.sentences.filter((s) => !s.verified);
  const pidToRank = new Map(res.results.map((r) => [r.passage_id, r.rank]));
  const icon = { high: "✓", medium: "≈", low: "!", none: "∅" }[res.confidence];

  return (
    <div className="stack" style={{ marginTop: 22 }}>
      <div className={`decision ${res.confidence}`} role="status">
        <strong style={{ fontSize: 18 }}>{icon}</strong>
        <div>
          <div>{res.decision}</div>
          <div className="tiny muted" style={{ marginTop: 4 }}>
            {LANGS[res.language] || res.language} query
            {res.topic ? ` · topic: ${res.topic}` : ""}
            {res.subject && res.subject !== "general" ? ` · ${res.subject}` : ""} · {res.timings.total_ms} ms
            {res.cached ? " · from cache" : ""} · served by {res.served_by}
            {res.ai.data_sent_to_google ? " · query processed by Gemma via Google's Gemini API" : ""}
            {res.ai.provider === "openai" && res.mode === "agent" ? " · query processed by OpenAI's API" : ""}
          </div>
          {liveMsg && <div className="small" style={{ marginTop: 6 }}><span className="badge accent">live</span> {liveMsg}</div>}
        </div>
      </div>

      {(kept.length > 0 || dropped.length > 0) && (
        <div className="card">
          <div className="spread">
            <h2 style={{ margin: 0 }}>Explanation</h2>
            <span className={`badge ${res.explanation.status === "verified" ? "ok" : res.explanation.status === "partial" ? "warn" : "bad"}`}>
              {kept.length}/{res.explanation.sentences.length} sentences verified against sources
            </span>
          </div>
          {kept.length === 0 && <p className="small muted">The verifier rejected every generated sentence, so no summary is shown. The sources below are still real.</p>}
          {(res.explanation.translation === "unavailable" || res.explanation.translation === "partial") && (
            <p className="small" style={{ margin: "6px 0" }}><span className="badge warn">not translated</span> Gemma did not return a {LANGS[res.language] || res.language} translation for {res.explanation.translation === "partial" ? "some sentences" : "these sentences"}, so they are shown in the source language.</p>
          )}
          {res.explanation.scope && <p className="tiny muted" style={{ margin: "4px 0" }}>{res.explanation.scope}</p>}
          {kept.map((s, i) => (
            <div key={i} className="sentence ok">
              <span className="mark">✓</span>
              <div className="txt">
                {s.display_text}
                <span className="cite">[{pidToRank.get(s.pid) ?? "?"}]</span>
                {s.translated && <div className="tiny muted">Translated by Gemma · original: {s.source_text}</div>}
              </div>
            </div>
          ))}
          {dropped.length > 0 && (
            <details style={{ marginTop: 8 }}>
              <summary>{dropped.length} sentence{dropped.length > 1 ? "s" : ""} removed by the citation verifier</summary>
              {dropped.map((s, i) => (
                <div key={i} className="sentence no">
                  <span className="mark">✕</span>
                  <div>
                    <div className="txt">{s.display_text}</div>
                    <div className="tiny muted">Why: {s.rejection_reason}</div>
                  </div>
                </div>
              ))}
            </details>
          )}
        </div>
      )}

      {res.results.length > 0 && res.confidence !== "low" && (
        <div className="spread noprint">
          <span className="small muted">{res.results.length} resource{res.results.length > 1 ? "s" : ""} you can read · tick up to 5 to build a lesson pack</span>
          <button className="btn accent" disabled={!picked.length} onClick={onLesson}>Make lesson pack ({picked.length})</button>
        </div>
      )}

      <div className="cols">
        <section className="stack" aria-label="Resources you can read">
          <div className="colhead">
            <h2>You can read <span className="badge ok">{res.confidence === "low" ? 0 : res.results.length}</span></h2>
            <div className="tiny muted">Open resources, plus library books your account is allowed to read. Explanations come only from this column.</div>
          </div>
          {res.confidence !== "low" && res.results.map((r) => <ResultCard key={r.id} r={r} picked={picked.includes(r.id)} toggle={() => toggle(r.id)} />)}
          {res.confidence === "low" && res.results.length > 0 && (
            <details className="card">
              <summary>Show {res.results.length} weak match{res.results.length > 1 ? "es" : ""} anyway (not recommended)</summary>
              <div className="stack" style={{ marginTop: 12 }}>
                {res.results.map((r) => <ResultCard key={r.id} r={r} picked={picked.includes(r.id)} toggle={() => toggle(r.id)} />)}
              </div>
            </details>
          )}
          {(res.results.length === 0 || res.confidence === "low") && <div className="card empty">No good free resource yet. We don't invent answers.</div>}
        </section>

        <section className="stack" aria-label="Restricted, authorised or paid resources">
          <div className="colhead">
            <h2>Restricted, authorised or paid <span className="badge warn">{res.restricted_results.length}</span></h2>
            <div className="tiny muted">Only the catalogue record is shown. Read them with the access route on each card: a library membership, an institution login at the publisher, or a purchase. GranthSetu never sees publisher logins.</div>
          </div>
          <ProxySetting />
          {res.restricted_results.map((r) => <RestrictedCard key={r.id} r={r} />)}
          {res.restricted_results.length === 0 && <div className="card empty">No subscription or paid matches for this query.</div>}
        </section>
      </div>

      <details className="card">
        <summary>Agent trace: {res.trace.length} steps, how this answer was produced</summary>
        <div className="small muted" style={{ marginTop: 8 }}>Search terms used: {res.expansions.join(" · ")}</div>
        <div className="trace">
          {res.trace.map((t, i) => {
            const { step, status, ms, ...rest } = t;
            return (
              <div key={i} className="t">
                <strong>{step}</strong>
                <span className={`badge ${["ok", "high", "verified", "recorded"].includes(status) ? "ok" : ["fallback", "low", "partial", "medium"].includes(status) ? "warn" : status === "skipped" ? "" : "bad"}`}>{status}</span>
                <span className="muted">{ms} ms</span>
                <code className="mono">{Object.keys(rest).length ? JSON.stringify(rest) : ""}</code>
              </div>
            );
          })}
        </div>
      </details>
    </div>
  );
}

const PROXY_KEY = "gs_proxy_prefix";
function readProxy(): string {
  try { return localStorage.getItem(PROXY_KEY) || ""; } catch { return ""; }
}
/** Route a publisher link through the user's own library proxy (EZproxy / OpenAthens redirector), if they set one. */
export function withProxy(url: string, prefix: string): string {
  const p = prefix.trim();
  if (!p) return url;
  return p.includes("{url}") ? p.replace("{url}", encodeURIComponent(url)) : p + url;
}

function ProxySetting() {
  const [v, setV] = useState(readProxy());
  const save = (x: string) => {
    setV(x);
    try { localStorage.setItem(PROXY_KEY, x); } catch { /* private mode: setting just won't persist */ }
    window.dispatchEvent(new Event("gs-proxy"));
  };
  return (
    <details className="card noprint">
      <summary>I have college or library access {v ? "(set)" : ""}</summary>
      <div className="stack" style={{ marginTop: 10 }}>
        <label className="lbl" htmlFor="proxy">Your library's proxy link (optional)</label>
        <input id="proxy" className="field" value={v} onChange={(e) => save(e.target.value)}
          placeholder="https://login.ezproxy.yourcollege.edu/login?url=" />
        <div className="tiny muted">Ask your college library for its "EZproxy" or "OpenAthens redirector" prefix. Links below then open through your college login. Saved only in this browser.</div>
      </div>
    </details>
  );
}

const ACCESS_LABEL: Record<string, [string, string]> = {
  authorised: ["info", "Institution login / free account"],
  paid: ["warn", "Paid"],
};

function RestrictedCard({ r }: { r: Result }) {
  const [proxy, setProxy] = useState(readProxy());
  useEffect(() => {
    const f = () => setProxy(readProxy());
    window.addEventListener("gs-proxy", f);
    return () => window.removeEventListener("gs-proxy", f);
  }, []);
  const [cls, label] = r.managed ? (POLICY_LABEL[r.policy.policy] || ["", r.policy.policy]) : (ACCESS_LABEL[r.access] || ["", r.access]);
  const href = r.managed && !r.policy.external_url ? `#/book/${r.id}` : r.access === "authorised" ? withProxy(r.url, proxy) : r.url;
  const internal = href.startsWith("#");
  return (
    <article className="card result">
      <div className="row" style={{ alignItems: "flex-start", flexWrap: "nowrap" }}>
        <span className="rank">{r.rank}</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <a className="title" href={href} {...(internal ? {} : { target: "_blank", rel: "noreferrer noopener" })}>{r.title}{internal ? "" : " ↗"}</a>
          <div className="row" style={{ marginTop: 6, gap: 6 }}>
            <span className={`badge ${cls}`}>{label}</span>
            <span className="badge">{r.source}</span>
            <span className="badge">{r.kind}</span>
            <span className="badge info">{r.lang_name}</span>
            {r.scores.rerank !== null && <span className="badge">relevance {r.scores.rerank}/10</span>}
          </div>
        </div>
      </div>
      {r.reason && <div className="reason"><b>Why this result:</b> {r.reason}</div>}
      <details>
        <summary>Catalogue description</summary>
        <div className="passage" style={{ marginTop: 8 }}>{r.passage}</div>
      </details>
      <div className="licence">
        {r.licence}{r.attribution ? ` · ${r.attribution}` : ""}{r.author ? ` · ${r.author}` : ""}
      </div>
      {r.managed && <div className="small muted">{r.policy.reason}{r.policy.needs_login ? " · sign in to check your access" : ""}</div>}
      <a className="btn" style={{ justifySelf: "start" }} href={href} {...(internal ? {} : { target: "_blank", rel: "noreferrer noopener" })}>
        {internal ? "View catalogue record" : r.access === "paid" ? "View / buy at the seller ↗" : proxy ? "Open with my college login ↗" : "Open at the publisher ↗"}
      </a>
    </article>
  );
}

function ResultCard({ r, picked, toggle }: { r: Result; picked: boolean; toggle: () => void }) {
  return (
    <article className="card result">
      <div className="row" style={{ alignItems: "flex-start", flexWrap: "nowrap" }}>
        <span className="rank">{r.rank}</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          {r.managed ? <a className="title" href={`#/book/${r.id}`}>{r.title}</a> : <a className="title" href={r.url} target="_blank" rel="noreferrer">{r.title} ↗</a>}
          <div className="row" style={{ marginTop: 6, gap: 6 }}>
            {r.managed && <span className={`badge ${(POLICY_LABEL[r.policy.policy] || ["", ""])[0]}`}>{(POLICY_LABEL[r.policy.policy] || ["", r.policy.policy])[1]}</span>}
            <span className="badge">{r.source}</span>
            {r.page_no && <span className="badge">page {r.page_no}</span>}
            <span className="badge">{r.kind}</span>
            <span className="badge info">{r.lang_name}</span>
            {r.cross_lingual && <span className="badge accent">cross-language match</span>}
            {r.scores.rerank !== null && <span className={`badge ${r.scores.rerank >= 7 ? "ok" : r.scores.rerank >= 5 ? "warn" : ""}`}>relevance {r.scores.rerank}/10</span>}
          </div>
        </div>
        <label className="row small noprint" style={{ gap: 6, cursor: "pointer" }}>
          <input type="checkbox" checked={picked} onChange={toggle} /> pack
        </label>
      </div>
      {r.reason && <div className="reason"><b>Why this result:</b> {r.reason}</div>}
      <details>
        <summary>Show the source passage</summary>
        <div className="passage" style={{ marginTop: 8 }}>{r.passage}</div>
        <div className="tiny muted mono" style={{ marginTop: 6 }}>
          fused {r.scores.fused} · bm25 {r.scores.bm25}{r.scores.semantic !== null ? ` · cosine ${r.scores.semantic}` : ""}
        </div>
      </details>
      <div className="licence">
        Licence:{" "}
        {r.licence_url ? <a href={r.licence_url} target="_blank" rel="noreferrer">{r.licence}</a> : r.licence}
        {r.attribution ? ` · ${r.attribution}` : ""}
        {r.author ? ` · ${r.author}` : ""}
      </div>
    </article>
  );
}
