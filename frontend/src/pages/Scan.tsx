import { useRef, useState } from "react";
import { api, ApiError, LANGS, type ScanResponse } from "../api";
import { go } from "../nav";

export default function ScanPage({ aiOn }: { aiOn: boolean }) {
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState<ScanResponse | null>(null);
  const [query, setQuery] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [consent, setConsent] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  const pick = (f: File | undefined) => {
    if (!f) return;
    if (!/^image\/(jpeg|png|webp)$/.test(f.type)) return setErr("Please choose a JPEG, PNG or WEBP photo.");
    if (f.size > 5 * 1024 * 1024) return setErr("Photo is larger than 5 MB. Please use a smaller one.");
    setErr(null);
    setOut(null);
    setFile(f);
    if (preview) URL.revokeObjectURL(preview);
    setPreview(URL.createObjectURL(f));
  };

  const read = async () => {
    if (!file) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await api.upload<ScanResponse>("/scan", file);
      setOut(r);
      setQuery(r.search_query);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Could not read the photo.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <h1>Scan to learn</h1>
      <p className="lede">
        Photograph a textbook page, a worksheet or your notes. Gemma reads it (Kannada, Hindi or English), finds the topic and
        key terms, and turns it into a search you can check and edit.
      </p>

      <div className="card" style={{ marginBottom: 14 }}>
        <div className="small">
          <strong>Privacy:</strong> {aiOn ? "your photo is sent to the configured Gemma model for reading" : "photo reading needs Gemma, which is not configured on this server"}.
          GranthSetu strips location data, never saves the photo, and never puts it in the cache or logs. Please avoid photos showing
          names, faces or addresses.
        </div>
        <label className="row small" style={{ marginTop: 10 }}>
          <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} /> I understand and want to continue
        </label>
      </div>

      <div className="grid2">
        <div className="stack">
          <div className="drop" onClick={() => input.current?.click()}
            onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); pick(e.dataTransfer.files[0]); }}>
            {preview ? <img src={preview} className="preview" alt="Selected page" /> : (
              <>
                <div style={{ fontSize: 28 }}>📷</div>
                <div>Tap to take a photo or choose one</div>
                <div className="tiny muted">JPEG, PNG or WEBP · up to 5 MB</div>
              </>
            )}
          </div>
          <input ref={input} type="file" accept="image/jpeg,image/png,image/webp" capture="environment" hidden onChange={(e) => pick(e.target.files?.[0])} />
          <button className="btn primary" disabled={!file || busy || !consent || !aiOn} onClick={read}>
            {busy ? "Reading the page…" : "Read this page"}
          </button>
          {!aiOn && <div className="small muted">AI is off on this server. You can still <a href="#/">type your question</a>.</div>}
          {err && <div className="err">{err}</div>}
        </div>

        <div className="card stack">
          {!out && <div className="muted small">What Gemma reads from the page will appear here. You can correct it before searching.</div>}
          {out && (
            <>
              <div className="row">
                <span className={`badge ${out.readability === "good" ? "ok" : out.readability === "partial" ? "warn" : "bad"}`}>readability: {out.readability}</span>
                <span className="badge info">{LANGS[out.language] || out.language}</span>
                <span className="badge">{out.subject}</span>
                <span className="tiny muted">{out.model} · {out.ms} ms</span>
              </div>
              {out.readability === "unreadable" && <div className="err">The page could not be read. Try better light, or type the text below.</div>}
              <div><label className="lbl">Topic</label><div>{out.topic || "—"}</div></div>
              {out.key_terms.length > 0 && <div><label className="lbl">Key terms</label><div className="row" style={{ gap: 6 }}>{out.key_terms.map((k) => <span key={k} className="badge">{k}</span>)}</div></div>}
              {out.questions.length > 0 && <div><label className="lbl">Questions on the page</label><ul className="small" style={{ margin: 0 }}>{out.questions.map((qq) => <li key={qq}><a href="#" onClick={(e) => { e.preventDefault(); setQuery(qq); }}>{qq}</a></li>)}</ul></div>}
              {out.extracted_text && <details><summary>Text Gemma read</summary><div className="passage" style={{ marginTop: 6 }}>{out.extracted_text}</div></details>}
              <div>
                <label className="lbl" htmlFor="sq">Search (edit if anything is wrong)</label>
                <textarea id="sq" className="field" value={query} onChange={(e) => setQuery(e.target.value)} />
              </div>
              <button className="btn accent" disabled={query.trim().length < 2} onClick={() => go("/", { q: query.trim() })}>Find open resources →</button>
              <div className="tiny muted">{out.privacy}</div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
