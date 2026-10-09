import { useEffect, useState } from "react";
import { api, LANGS, subscribe } from "../api";

type Gap = { id: number; lang: string; subject: string; topic: string; hits: number; status: string; added: number; last_seen: string };

const STATUS: Record<string, [string, string]> = {
  open: ["", "open"],
  fetching: ["accent", "fetching live…"],
  resources_added: ["ok", "resources added"],
  needs_contributors: ["warn", "needs contributors"],
  fetch_failed: ["bad", "fetch failed, will retry"],
};

const REPO = (import.meta.env.VITE_REPO_URL as string | undefined) || "";

export default function GapsPage() {
  const [gaps, setGaps] = useState<Gap[] | null>(null);
  const [groups, setGroups] = useState<Record<string, number>>({});
  const load = () => api.get<{ gaps: Gap[]; by_lang_subject: Record<string, number> }>("/gaps").then((r) => { setGaps(r.gaps); setGroups(r.by_lang_subject); }).catch(() => setGaps([]));
  useEffect(() => { load(); }, []);
  useEffect(() => subscribe((e) => { if (e.type === "job" && e.kind === "ingest_query" && e.status !== "queued") load(); }), []);

  return (
    <div className="stack">
      <h1>Knowledge Gap Board</h1>
      <p className="lede" style={{ marginBottom: 0 }}>
        When GranthSetu can't find good open material, it doesn't make something up. It records the topic here (anonymised:
        language, subject and topic only), fetches from open libraries live, and if nothing exists, turns the gap into a
        contribution task.
      </p>
      {Object.keys(groups).length > 0 && (
        <div className="row">{Object.entries(groups).map(([k, n]) => {
          const [l, s] = k.split("/");
          return <span key={k} className="badge info">{LANGS[l] || l} · {s}: {n}</span>;
        })}</div>
      )}
      <div className="card">
        {!gaps && <div className="muted small">Loading…</div>}
        {gaps && gaps.length === 0 && <div className="empty">No gaps yet. Every search so far found good material.</div>}
        {gaps && gaps.length > 0 && (
          <div className="scroll"><table>
            <thead><tr><th>Topic</th><th>Language</th><th>Subject</th><th>Asked</th><th>Status</th><th>Help close it</th></tr></thead>
            <tbody>{gaps.map((g) => {
              const [cls, label] = STATUS[g.status] || ["", g.status];
              return (
                <tr key={g.id}>
                  <td><strong>{g.topic}</strong></td>
                  <td>{LANGS[g.lang] || g.lang}</td>
                  <td>{g.subject}</td>
                  <td>{g.hits}×</td>
                  <td><span className={`badge ${cls}`}>{label}</span>{g.added > 0 && <div className="tiny muted">+{g.added} resources</div>}</td>
                  <td className="small">
                    <a href={`https://${g.lang}.wikipedia.org/w/index.php?search=${encodeURIComponent(g.topic)}`} target="_blank" rel="noreferrer">Write on {LANGS[g.lang] || g.lang} Wikipedia</a>
                    {REPO && <> · <a href={`${REPO}/issues/new?title=${encodeURIComponent(`Knowledge gap: ${g.topic} (${g.lang})`)}&labels=knowledge-gap`} target="_blank" rel="noreferrer">Open issue</a></>}
                  </td>
                </tr>
              );
            })}</tbody>
          </table></div>
        )}
      </div>
    </div>
  );
}
