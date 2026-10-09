import { useEffect, useState } from "react";
import { api, ApiError, subscribe } from "../api";

type ModeSummary = {
  n: number; hit_at_5?: number; mrr_at_5?: number; precision_at_5?: number; cross_lingual_hit_at_5?: number | null;
  latency_p50_ms?: number; latency_p95_ms?: number; verifier_pass_rate?: number | null; errors?: number;
};
type ModeRow = { hit: boolean; rr: number; ms: number; top: string[]; error: string | null; pass_rate: number | null };
type Run = {
  id: number; created_at: string;
  config: { queries: number; answerable: number; modes: string[]; embedder: string; embedder_semantic: boolean; llm: { model: string } | null; passages: number; note: string };
  summary: Record<string, ModeSummary>;
  details: { id: string; query: string; lang: string; answerable: boolean; relevant_topics: string[]; modes: Record<string, ModeRow> }[];
};

const LABEL: Record<string, string> = { keyword: "Keyword (BM25)", semantic: "Meaning (embeddings)", hybrid: "Hybrid (RRF)", agent: "Hybrid + Gemma rerank" };
const pct = (v?: number | null) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);

export default function EvalPage() {
  const [run, setRun] = useState<Run | null>(null);
  const [job, setJob] = useState<string | null>(null);
  const [progress, setProgress] = useState<string>("");
  const [err, setErr] = useState<string | null>(null);

  const load = () => api.get<{ run: Run | null }>("/eval/latest").then((r) => setRun(r.run));
  useEffect(() => { load(); }, []);
  useEffect(() => {
    if (!job) return;
    return subscribe((e) => {
      if (e.job_id !== job) return;
      if (e.progress) setProgress(String(e.progress));
      if (e.status === "done") { setJob(null); setProgress(""); load(); }
      if (e.status === "failed") { setJob(null); setErr(String(e.error || "evaluation failed")); }
    });
  }, [job]);

  const start = async (useLlm: boolean) => {
    setErr(null);
    try {
      const r = await api.post<{ job_id: string }>("/eval/run", { use_llm: useLlm });
      setJob(r.job_id);
      setProgress("queued");
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Could not start the evaluation.");
    }
  };

  const modes = run ? run.config.modes : [];
  const metrics: [keyof ModeSummary, string, boolean][] = [
    ["hit_at_5", "Relevant in top 5", true], ["mrr_at_5", "MRR@5", false], ["precision_at_5", "Precision@5", true],
    ["cross_lingual_hit_at_5", "Cross-language hit (hi/kn queries)", true], ["latency_p50_ms", "Latency p50", false],
    ["latency_p95_ms", "Latency p95", false], ["verifier_pass_rate", "Verifier pass rate", true],
  ];

  return (
    <div className="stack">
      <h1>Evaluation</h1>
      <p className="lede" style={{ marginBottom: 0 }}>
        An ablation on labelled Kannada, Hindi and English queries: keyword vs meaning vs hybrid vs hybrid + Gemma rerank. Every
        number below comes from a real run against the current live index. Nothing is precomputed.
      </p>
      <div className="row">
        <button className="btn primary" disabled={!!job} onClick={() => start(true)}>{job ? `Running… ${progress}` : "Run evaluation"}</button>
        <button className="btn" disabled={!!job} onClick={() => start(false)}>Run without Gemma (faster)</button>
        {err && <span className="err">{err}</span>}
      </div>
      {!run && <div className="card empty">No evaluation has been run yet on this library.</div>}
      {run && (
        <>
          <div className="card">
            <div className="spread">
              <h2 style={{ margin: 0 }}>Run #{run.id}</h2>
              <span className="tiny muted">{run.created_at} · {run.config.answerable}/{run.config.queries} queries answerable from the index · {run.config.passages} passages · embeddings {run.config.embedder}{run.config.embedder_semantic ? "" : " (lexical fallback)"}{run.config.llm ? ` · ${run.config.llm.model}` : ""}</span>
            </div>
            <div className="scroll" style={{ marginTop: 12 }}><table>
              <thead><tr><th>Metric</th>{modes.map((m) => <th key={m}>{LABEL[m] || m}</th>)}</tr></thead>
              <tbody>{metrics.map(([k, label, isPct]) => (
                <tr key={k}>
                  <td>{label}</td>
                  {modes.map((m) => {
                    const v = run.summary[m]?.[k] as number | null | undefined;
                    return <td key={m}>{isPct ? (
                      <div className="row" style={{ gap: 8, flexWrap: "nowrap" }}><span style={{ minWidth: 38 }}>{pct(v)}</span>{v !== null && v !== undefined && <div className="bar-viz" style={{ flex: 1 }}><span style={{ width: `${v * 100}%` }} /></div>}</div>
                    ) : k === "mrr_at_5" ? (v ?? "—") : v !== undefined && v !== null ? `${v} ms` : "—"}</td>;
                  })}
                </tr>
              ))}</tbody>
            </table></div>
            <p className="tiny muted" style={{ marginBottom: 0 }}>{run.config.note}</p>
          </div>
          <details className="card">
            <summary>Per-query results ({run.details.length})</summary>
            <div className="scroll" style={{ marginTop: 10 }}><table>
              <thead><tr><th>Query</th><th>Relevant topic</th>{modes.map((m) => <th key={m}>{m}</th>)}</tr></thead>
              <tbody>{run.details.map((d) => (
                <tr key={d.id}>
                  <td>{d.query}<div className="tiny muted">{d.lang}{d.answerable ? "" : " · not in index"}</div></td>
                  <td className="small">{d.relevant_topics.join(", ")}</td>
                  {modes.map((m) => {
                    const r = d.modes[m];
                    return <td key={m} className="small" title={r?.top.join("\n")}>{r ? (r.error ? <span className="badge bad">error</span> : r.hit ? <span className="badge ok">hit @{Math.round(1 / r.rr)}</span> : <span className="badge">miss</span>) : "—"}</td>;
                  })}
                </tr>
              ))}</tbody>
            </table></div>
          </details>
        </>
      )}
    </div>
  );
}
