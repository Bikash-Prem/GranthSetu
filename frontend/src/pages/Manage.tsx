import { useEffect, useRef, useState } from "react";
import { api, ApiError, POLICY_LABEL } from "../api";
import { go } from "../nav";
import { useSession } from "../session";

type Options = {
  policies: Record<string, string>; rights_bases: Record<string, string>; catalogue: string[]; statuses: string[];
  languages: string[]; types: string[]; institutions: { id: number; name: string }[]; groups: { id: number; name: string }[];
  transitions: Record<string, { from: string[]; to: string; permission: string | null; reason_required: boolean }>;
  max_upload_mb: number; separate_reviewer_required: boolean;
};
type Res = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
type Detail = {
  resource: Res; files: Res[]; events: Res[]; reviews: Res[]; jobs: Res[]; errors: Res[]; entitlements: Res[]; pages: number;
  problems: string[]; allowed_actions: string[]; can: { verify_rights: boolean; edit_policy: boolean; entitlements: boolean; reprocess: boolean };
};

const STATUS_CLS: Record<string, string> = {
  DRAFT: "", SUBMITTED: "info", UNDER_REVIEW: "info", NEEDS_INFORMATION: "warn", APPROVED: "ok", REJECTED: "bad",
  PROCESSING: "accent", PROCESSING_FAILED: "bad", PUBLISHED: "ok", WITHDRAWN: "warn", ARCHIVED: "",
};
const ACTION_LABEL: Record<string, string> = {
  submit: "Submit for review", start_review: "Start review", request_info: "Request information", reject: "Reject",
  approve: "Approve", publish: "Publish (process and index)", retry: "Retry processing", withdraw: "Withdraw",
  reinstate: "Reinstate for review", revise: "Revise as draft", archive: "Archive",
};

export function StatusBadge({ s }: { s: string }) {
  return <span className={`badge ${STATUS_CLS[s] ?? ""}`}>{s.replace(/_/g, " ").toLowerCase()}</span>;
}

function useOptions() {
  const [o, setO] = useState<Options | null>(null);
  useEffect(() => { api.get<Options>("/manage/options").then(setO).catch(() => setO(null)); }, []);
  return o;
}

function Guard({ perm, children }: { perm: string; children: React.ReactNode }) {
  const { user, loaded, can } = useSession();
  if (!loaded) return <div className="empty">Loading…</div>;
  if (!user) return <div className="card empty" style={{ marginTop: 40 }}>Library management needs an account. <a href={`#/login?next=${encodeURIComponent(location.hash.slice(1))}`}>Sign in</a></div>;
  if (!can(perm)) return <div className="card empty" style={{ marginTop: 40 }}>Your account does not have permission to use library management. Ask an administrator.</div>;
  return <>{children}</>;
}

function SubNav({ here }: { here: string }) {
  const { can } = useSession();
  const items: [string, string, string][] = [
    ["/manage", "Dashboard", "resource.create"], ["/manage/new", "Add resource", "resource.create"],
    ["/manage/list", "Manage resources", "resource.create"], ["/manage/list?status=SUBMITTED", "Review queue", "submission.review"],
    ["/manage/jobs", "Processing jobs", "jobs.manage"], ["/manage/audit", "Audit history", "audit.view"], ["/admin", "Users & institutions", "entitlement.manage"],
  ];
  return (
    <nav className="row noprint" style={{ gap: 6, marginTop: 24 }} aria-label="Library management">
      {items.filter(([, , p]) => can(p) || (p === "entitlement.manage" && can("user.manage"))).map(([h, l]) => (
        <a key={h} href={`#${h}`} className={`btn ${here === h ? "primary" : ""}`}>{l}</a>
      ))}
    </nav>
  );
}

// ---------------------------------------------------------------------------
export function ManageDashboard() {
  const [d, setD] = useState<{ by_status: Record<string, number>; jobs: Record<string, number>; pending_rights: number; outbox_pending: number | null } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { api.get<typeof d>("/manage/dashboard").then(setD).catch((e) => setErr(e.message)); }, []);
  return (
    <Guard perm="resource.create">
      <SubNav here="/manage" />
      <h1>Library management</h1>
      {err && <div className="err">{err}</div>}
      {d && (
        <div className="stack">
          <div className="grid3">
            {["DRAFT", "SUBMITTED", "UNDER_REVIEW", "NEEDS_INFORMATION", "APPROVED", "PROCESSING_FAILED", "PUBLISHED", "WITHDRAWN"].map((s) => (
              <a key={s} className="card" href={`#/manage/list?status=${s}`} style={{ textDecoration: "none" }}>
                <div className="lbl">{s.replace(/_/g, " ").toLowerCase()}</div>
                <div className="stat">{d.by_status[s] ?? 0}</div>
              </a>
            ))}
          </div>
          {d.outbox_pending !== null && (
            <div className="card small">
              Rights checks waiting: <b>{d.pending_rights}</b> · Processing jobs: {Object.entries(d.jobs).map(([k, v]) => `${k} ${v}`).join(" · ") || "none"} ·
              Index updates waiting to be delivered: <b>{d.outbox_pending}</b>
            </div>
          )}
        </div>
      )}
    </Guard>
  );
}

export function ManageList({ status }: { status: string }) {
  const [q, setQ] = useState("");
  const [st, setSt] = useState(status);
  const [page, setPage] = useState(1);
  const [d, setD] = useState<{ items: Res[]; total: number; pages: number } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const o = useOptions();
  useEffect(() => setSt(status), [status]);
  useEffect(() => {
    api.get<typeof d>(`/manage/resources?status=${st}&q=${encodeURIComponent(q)}&page=${page}`).then(setD).catch((e) => setErr(e.message));
  }, [st, q, page]);
  return (
    <Guard perm="resource.create">
      <SubNav here={status === "SUBMITTED" ? "/manage/list?status=SUBMITTED" : "/manage/list"} />
      <h1>{st === "SUBMITTED" ? "Review queue" : "Manage resources"}</h1>
      <div className="card row">
        <input className="field" style={{ flex: 1 }} placeholder="Filter by title" value={q} onChange={(e) => { setQ(e.target.value); setPage(1); }} aria-label="Filter by title" />
        <select className="field" value={st} onChange={(e) => { setSt(e.target.value); setPage(1); }} aria-label="Status">
          <option value="">All statuses</option>
          {o?.statuses.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </div>
      {err && <div className="err">{err}</div>}
      {d && d.items.length === 0 && <div className="card empty">No resources match.</div>}
      {d && d.items.length > 0 && (
        <div className="card scroll">
          <table>
            <thead><tr><th>Title</th><th>Status</th><th>Policy</th><th>Rights</th><th>Updated</th></tr></thead>
            <tbody>{d.items.map((r) => (
              <tr key={r.id}>
                <td><a href={`#/manage/res/${r.id}`}>{r.title}</a><div className="tiny muted">{r.author || ""} · {r.lang}</div></td>
                <td><StatusBadge s={r.status} /></td>
                <td><span className={`badge ${(POLICY_LABEL[r.policy] || [""])[0]}`}>{(POLICY_LABEL[r.policy] || ["", r.policy])[1]}</span>{r.catalogue === "private" && <div className="tiny muted">private catalogue</div>}</td>
                <td className="small">{r.rights_basis}<div className={`tiny ${r.rights_status === "verified" ? "" : "muted"}`}>{r.rights_status}</div></td>
                <td className="tiny muted">{(r.updated_at || "").slice(0, 16).replace("T", " ")}</td>
              </tr>))}
            </tbody>
          </table>
        </div>
      )}
      {d && d.pages > 1 && (
        <div className="row" style={{ justifyContent: "center", marginTop: 10 }}>
          <button className="btn" disabled={page <= 1} onClick={() => setPage(page - 1)}>Previous</button>
          <span className="small">Page {page} of {d.pages}</span>
          <button className="btn" disabled={page >= d.pages} onClick={() => setPage(page + 1)}>Next</button>
        </div>
      )}
    </Guard>
  );
}

// ---------------------------------------------------------------------------
const TEXT_FIELDS: [string, string, number][] = [
  ["title", "Title *", 300], ["subtitle", "Subtitle", 300], ["author", "Author(s)", 400], ["isbn", "ISBN", 20],
  ["publisher", "Publisher", 200], ["edition", "Edition", 60], ["subject", "Subject", 120], ["categories", "Categories (comma separated)", 300],
  ["source", "Source", 120], ["url", "Source URL / provider link", 1000], ["provider_id", "Provider identifier", 200],
  ["licence", "Licence or access terms *", 200], ["licence_url", "Licence URL", 1000], ["rights_holder", "Rights holder", 200],
  ["attribution", "Attribution line", 400],
];

function ResourceForm({ o, initial, onSave, canPolicy, busy }: { o: Options; initial: Res; onSave: (v: Res) => void; canPolicy: boolean; busy: boolean }) {
  const [v, setV] = useState<Res>(initial);
  useEffect(() => setV(initial), [initial]);
  const set = (k: string, x: unknown) => setV({ ...v, [k]: x });
  const f = (k: string, label: string, max: number) => (
    <div key={k}>
      <label className="lbl" htmlFor={`f-${k}`}>{label}</label>
      <input id={`f-${k}`} className="field" maxLength={max} value={v[k] ?? ""} onChange={(e) => set(k, e.target.value)} />
    </div>
  );
  const valid = (v.title || "").trim() && v.lang;
  return (
    <form className="stack" onSubmit={(e) => { e.preventDefault(); if (valid) onSave(v); }}>
      <div className="grid2">{TEXT_FIELDS.slice(0, 8).map(([k, l, m]) => f(k, l, m))}</div>
      <div className="grid3">
        <div>
          <label className="lbl" htmlFor="f-lang">Language *</label>
          <select id="f-lang" className="field" style={{ width: "100%" }} value={v.lang || ""} onChange={(e) => set("lang", e.target.value)}>
            <option value="">Choose…</option>{o.languages.map((l) => <option key={l} value={l}>{l}</option>)}
          </select>
        </div>
        <div>
          <label className="lbl" htmlFor="f-kind">Resource type</label>
          <select id="f-kind" className="field" style={{ width: "100%" }} value={v.kind || "book"} onChange={(e) => set("kind", e.target.value)}>
            {o.types.map((l) => <option key={l} value={l}>{l}</option>)}
          </select>
        </div>
        <div>
          <label className="lbl" htmlFor="f-year">Publication year</label>
          <input id="f-year" className="field" inputMode="numeric" value={v.pub_year ?? ""} onChange={(e) => set("pub_year", e.target.value.replace(/\D/g, "").slice(0, 4))} />
        </div>
      </div>
      <div>
        <label className="lbl" htmlFor="f-desc">Description (shown in the public catalogue record)</label>
        <textarea id="f-desc" className="field" maxLength={4000} value={v.description ?? ""} onChange={(e) => set("description", e.target.value)} />
      </div>
      <h2 style={{ margin: "8px 0 0" }}>Source, licence and rights</h2>
      <div className="grid2">{TEXT_FIELDS.slice(8).map(([k, l, m]) => f(k, l, m))}</div>
      <div className="grid2">
        <div>
          <label className="lbl" htmlFor="f-basis">Rights or permission basis *</label>
          <select id="f-basis" className="field" style={{ width: "100%" }} value={v.rights_basis || "unverified"} onChange={(e) => set("rights_basis", e.target.value)}>
            {Object.entries(o.rights_bases).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
          </select>
        </div>
        <div>
          <label className="lbl" htmlFor="f-notes">Rights evidence / notes</label>
          <input id="f-notes" className="field" maxLength={2000} value={v.rights_notes ?? ""} onChange={(e) => set("rights_notes", e.target.value)} placeholder="e.g. Permission letter dated…, CC licence page" />
        </div>
      </div>
      <fieldset className="card stack" style={{ margin: 0 }}>
        <legend className="lbl">What the licence permits GranthSetu to do</legend>
        {[["allow_display", "Display the full text to eligible readers"], ["allow_download", "Let eligible readers download the file"],
          ["allow_ai", "Machine processing: OCR, search indexing of the full text, embeddings and AI explanations"]].map(([k, l]) => (
          <label key={k} className="row small" style={{ gap: 8 }}>
            <input type="checkbox" checked={!!v[k] && v[k] !== 0} onChange={(e) => set(k, e.target.checked)} /> {l}
          </label>
        ))}
      </fieldset>
      <h2 style={{ margin: "8px 0 0" }}>Access policy</h2>
      {!canPolicy && <div className="small muted">A librarian or rights manager sets the access policy during review.</div>}
      <div className="grid3">
        <div>
          <label className="lbl" htmlFor="f-policy">Policy</label>
          <select id="f-policy" className="field" style={{ width: "100%" }} disabled={!canPolicy} value={v.policy || "UNPUBLISHED"} onChange={(e) => set("policy", e.target.value)}>
            {Object.keys(o.policies).map((k) => <option key={k} value={k}>{(POLICY_LABEL[k] || ["", k])[1]}</option>)}
          </select>
          <div className="tiny muted" style={{ marginTop: 4 }}>{o.policies[v.policy || "UNPUBLISHED"]}</div>
        </div>
        <div>
          <label className="lbl" htmlFor="f-cat">Catalogue for people who cannot read it</label>
          <select id="f-cat" className="field" style={{ width: "100%" }} disabled={!canPolicy} value={v.catalogue || "private"} onChange={(e) => set("catalogue", e.target.value)}>
            <option value="discoverable">Discoverable: show permitted metadata</option>
            <option value="private">Private: hide it completely</option>
          </select>
        </div>
        <div>
          {(v.policy === "INSTITUTION_ONLY") && <>
            <label className="lbl" htmlFor="f-inst">Institution</label>
            <select id="f-inst" className="field" style={{ width: "100%" }} disabled={!canPolicy} value={v.institution_id ?? ""} onChange={(e) => set("institution_id", e.target.value || null)}>
              <option value="">Choose…</option>{o.institutions.map((i) => <option key={i.id} value={i.id}>{i.name}</option>)}
            </select></>}
          {(v.policy === "GROUP_RESTRICTED") && <>
            <label className="lbl" htmlFor="f-grp">Group</label>
            <select id="f-grp" className="field" style={{ width: "100%" }} disabled={!canPolicy} value={v.group_id ?? ""} onChange={(e) => set("group_id", e.target.value || null)}>
              <option value="">Choose…</option>{o.groups.map((i) => <option key={i.id} value={i.id}>{i.name}</option>)}
            </select></>}
        </div>
      </div>
      <button className="btn primary" style={{ justifySelf: "start" }} disabled={!valid || busy}>{busy ? "Saving…" : "Save"}</button>
    </form>
  );
}

const POLICY_KEYS = ["policy", "catalogue", "institution_id", "group_id"];

export function ManageNew() {
  const o = useOptions();
  const { can } = useSession();
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const save = async (v: Res) => {
    setBusy(true);
    setErr(null);
    const body = Object.fromEntries(Object.entries(v).filter(([k, x]) => x !== "" && x !== undefined && (can("policy.edit") || !POLICY_KEYS.includes(k))));
    try {
      const r = await api.post<{ id: number }>("/manage/resources", body);
      go(`/manage/res/${r.id}`);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Could not save.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Guard perm="resource.create">
      <SubNav here="/manage/new" />
      <h1>Add resource</h1>
      <p className="lede">Register the catalogue record first. You can then upload the file (PDF or UTF-8 text), or leave it as a catalogue-only or external-provider record when that is all you may legally provide. Nothing is public until it is reviewed, approved and published.</p>
      {err && <div className="err">{err}</div>}
      {o && <div className="card"><ResourceForm o={o} busy={busy} canPolicy={can("policy.edit")} onSave={save}
        initial={{ lang: "en", kind: "book", rights_basis: "unverified", policy: "UNPUBLISHED", catalogue: "private", allow_display: true, allow_download: false, allow_ai: true }} /></div>}
    </Guard>
  );
}

// ---------------------------------------------------------------------------
export function ManageResource({ id }: { id: number }) {
  const o = useOptions();
  const [d, setD] = useState<Detail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [reason, setReason] = useState("");
  const [edit, setEdit] = useState(false);
  const [grant, setGrant] = useState({ subject_type: "user", email: "", subject_id: "", expires_at: "", reason: "" });
  const fileRef = useRef<HTMLInputElement>(null);

  const load = () => api.get<Detail>(`/manage/resources/${id}`).then((x) => { setD(x); setErr(null); }).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, [id]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {  // follow processing live
    if (d?.resource.status !== "PROCESSING" && !d?.jobs.some((j) => ["queued", "running"].includes(j.status))) return;
    const t = window.setInterval(load, 2000);
    return () => window.clearInterval(t);
  }, [d?.resource.status, d?.jobs]); // eslint-disable-line react-hooks/exhaustive-deps

  const run = async (fn: () => Promise<unknown>, ok: string) => {
    setBusy(true);
    setMsg(null);
    setErr(null);
    try {
      const out = await fn();
      if (out && typeof out === "object" && "resource" in (out as object)) setD(out as Detail);
      else await load();
      setMsg(ok);
      setReason("");
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "Request failed.");
    } finally {
      setBusy(false);
    }
  };

  if (err && !d) return <Guard perm="resource.create"><div className="card empty" style={{ marginTop: 40 }}>{err}</div></Guard>;
  if (!d || !o) return <div className="empty">Loading…</div>;
  const r = d.resource;
  const needsReason = d.allowed_actions.some((a) => o.transitions[a]?.reason_required);
  const current = d.files.find((f) => f.is_current);

  return (
    <Guard perm="resource.create">
      <SubNav here="" />
      <div className="spread" style={{ marginTop: 24 }}>
        <div>
          <h1 style={{ margin: 0 }}>{r.title}</h1>
          <div className="row" style={{ gap: 6, marginTop: 8 }}>
            <StatusBadge s={r.status} />
            <span className={`badge ${(POLICY_LABEL[r.policy] || [""])[0]}`}>{(POLICY_LABEL[r.policy] || ["", r.policy])[1]}</span>
            <span className={`badge ${r.rights_status === "verified" ? "ok" : r.rights_status === "rejected" ? "bad" : "warn"}`}>rights {r.rights_status}</span>
            {r.status === "PUBLISHED" && <a className="badge info" href={`#/book/${r.id}`}>public page →</a>}
          </div>
        </div>
      </div>
      {msg && <div className="card small" role="status" style={{ background: "var(--ok-soft)" }}>{msg}</div>}
      {err && <div className="err" role="alert">{err}</div>}

      <div className="grid2" style={{ marginTop: 14 }}>
        <div className="card stack">
          <h2 style={{ margin: 0 }}>Workflow</h2>
          {d.problems.length > 0 && r.status !== "PUBLISHED" && (
            <div className="small"><b>Before it can be approved and published:</b><ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{d.problems.map((p) => <li key={p}>{p}</li>)}</ul></div>
          )}
          {needsReason && <textarea className="field" placeholder="Reason or required corrections (needed to reject, request information, withdraw or reinstate)" value={reason} maxLength={2000} onChange={(e) => setReason(e.target.value)} aria-label="Reason" />}
          <div className="row">
            {d.allowed_actions.length === 0 && <span className="small muted">No actions available to you in this state.</span>}
            {d.allowed_actions.map((a) => (
              <button key={a} className={`btn ${["approve", "publish", "submit"].includes(a) ? "primary" : ["reject", "withdraw"].includes(a) ? "rec" : ""}`}
                disabled={busy || (o.transitions[a]?.reason_required && !reason.trim())}
                onClick={() => run(() => api.post(`/manage/resources/${id}/transition`, { action: a, reason }), `${ACTION_LABEL[a] || a}: done.`)}>
                {ACTION_LABEL[a] || a}
              </button>
            ))}
          </div>
          {d.can.verify_rights && !["PUBLISHED", "ARCHIVED"].includes(r.status) && (
            <div className="row">
              <span className="small">Rights review:</span>
              <button className="btn" disabled={busy} onClick={() => run(() => api.post(`/manage/resources/${id}/rights`, { decision: "verified", notes: reason }), "Rights marked verified.")}>Mark rights verified</button>
              <button className="btn" disabled={busy} onClick={() => run(() => api.post(`/manage/resources/${id}/rights`, { decision: "rejected", notes: reason }), "Rights marked rejected.")}>Reject rights</button>
            </div>
          )}
          {r.status === "PUBLISHED" && d.can.reprocess && (
            <button className="btn" style={{ justifySelf: "start" }} disabled={busy} onClick={() => run(() => api.post(`/manage/resources/${id}/reprocess`, {}), "Reprocessing queued.")}>Reprocess and re-index</button>
          )}
          {o.separate_reviewer_required && <div className="tiny muted">This deployment requires a different person to review and verify rights for your own submissions.</div>}
        </div>

        <div className="card stack">
          <h2 style={{ margin: 0 }}>File</h2>
          {current ? (
            <div className="small">
              <b>{current.filename}</b> · {current.mime} · {Math.ceil(current.size_bytes / 1024)} KB
              <div className="tiny muted mono">sha256 {current.sha256.slice(0, 20)}… · malware scan: {current.scan_status === "not_configured" ? "no scanner configured" : current.scan_status}</div>
              {d.pages > 0 && <div className="tiny muted">{d.pages} pages extracted</div>}
            </div>
          ) : <div className="small muted">{r.rights_basis === "catalogue_only" ? "Catalogue-only record: no file is hosted." : "No file uploaded yet."}</div>}
          {r.rights_basis !== "catalogue_only" && !["WITHDRAWN", "ARCHIVED", "PROCESSING"].includes(r.status) && (
            <div className="row">
              <input ref={fileRef} type="file" accept="application/pdf,text/plain,.pdf,.txt" aria-label="Choose a PDF or text file" />
              <button className="btn" disabled={busy} onClick={() => {
                const f = fileRef.current?.files?.[0];
                if (!f) return setErr("Choose a file first.");
                if (f.size > o.max_upload_mb * 1024 * 1024) return setErr(`The file is larger than ${o.max_upload_mb} MB.`);
                run(() => api.upload(`/manage/resources/${id}/file`, f), current ? "File replaced." : "File uploaded and checksummed.");
              }}>{current ? "Replace file" : "Upload"}</button>
            </div>
          )}
          {current && <a className="small" href={`/api/manage/resources/${id}/file`}>Download for review (audited)</a>}
        </div>
      </div>

      <div className="card" style={{ marginTop: 14 }}>
        <div className="spread"><h2 style={{ margin: 0 }}>Catalogue record, rights and access policy</h2>
          <button className="btn" onClick={() => setEdit(!edit)}>{edit ? "Close" : "Edit"}</button></div>
        {!edit && (
          <div className="grid3 small" style={{ marginTop: 10 }}>
            {[["Author", r.author], ["Language", r.lang], ["Type", r.kind], ["Publisher", r.publisher], ["Year", r.pub_year], ["ISBN", r.isbn],
              ["Licence", r.licence], ["Rights basis", o.rights_bases[r.rights_basis]], ["Rights holder", r.rights_holder],
              ["Catalogue", r.catalogue], ["Display / download / AI", `${r.allow_display ? "yes" : "no"} / ${r.allow_download ? "yes" : "no"} / ${r.allow_ai ? "yes" : "no"}`],
              ["Source URL", r.url]].map(([k, val]) => <div key={k as string}><div className="lbl">{k}</div>{val || <span className="muted">—</span>}</div>)}
          </div>
        )}
        {edit && <div style={{ marginTop: 10 }}><ResourceForm o={o} initial={r} busy={busy} canPolicy={d.can.edit_policy}
          onSave={(v) => {
            const changed = Object.fromEntries(Object.entries(v).filter(([k, x]) => x !== r[k] && k in r && (d.can.edit_policy || !POLICY_KEYS.includes(k))));
            run(() => api.patch(`/manage/resources/${id}`, changed), "Saved.").then(() => setEdit(false));
          }} /></div>}
      </div>

      {d.can.entitlements && (
        <div className="card stack" style={{ marginTop: 14 }}>
          <h2 style={{ margin: 0 }}>Entitlements</h2>
          <div className="small muted">Grant reading access to a person, a group or an institution. Revoking or expiring an entitlement takes effect on the next request, including search and AI answers.</div>
          <div className="row">
            <select className="field" value={grant.subject_type} onChange={(e) => setGrant({ ...grant, subject_type: e.target.value })} aria-label="Grant to">
              <option value="user">Person (email)</option><option value="group">Group</option><option value="institution">Institution</option>
            </select>
            {grant.subject_type === "user"
              ? <input className="field" style={{ flex: 1 }} placeholder="reader@college.edu" value={grant.email} onChange={(e) => setGrant({ ...grant, email: e.target.value })} aria-label="Email" />
              : <select className="field" value={grant.subject_id} onChange={(e) => setGrant({ ...grant, subject_id: e.target.value })} aria-label="Who">
                  <option value="">Choose…</option>
                  {(grant.subject_type === "group" ? o.groups : o.institutions).map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
                </select>}
            <input className="field" type="date" value={grant.expires_at} onChange={(e) => setGrant({ ...grant, expires_at: e.target.value })} aria-label="Expires on (optional)" />
            <button className="btn" disabled={busy} onClick={() => run(() => api.post(`/manage/resources/${id}/entitlements`, {
              subject_type: grant.subject_type, email: grant.subject_type === "user" ? grant.email : null,
              subject_id: grant.subject_type === "user" ? null : Number(grant.subject_id) || null,
              expires_at: grant.expires_at ? new Date(grant.expires_at + "T23:59:59").toISOString() : null, reason: grant.reason,
            }), "Entitlement granted.")}>Grant</button>
          </div>
          {d.entitlements.length > 0 && (
            <table><thead><tr><th>Who</th><th>Expires</th><th>Status</th><th></th></tr></thead>
              <tbody>{d.entitlements.map((e) => (
                <tr key={e.id}>
                  <td>{e.subject_type} #{e.subject_id}</td><td className="tiny">{e.expires_at ? e.expires_at.slice(0, 10) : "never"}</td>
                  <td>{e.active ? <span className="badge ok">active</span> : <span className="badge">{e.revoked_at ? "revoked" : "expired"}</span>}</td>
                  <td>{e.active && <button className="btn ghost small" onClick={() => run(() => api.del(`/manage/entitlements/${e.id}`), "Entitlement revoked.")}>Revoke</button>}</td>
                </tr>))}
              </tbody></table>
          )}
        </div>
      )}

      <div className="grid2" style={{ marginTop: 14 }}>
        <div className="card">
          <h2>Processing</h2>
          {d.jobs.length === 0 && <div className="small muted">No processing yet. Publishing starts it.</div>}
          {d.jobs.map((j) => (
            <div key={j.id} className="small" style={{ padding: "6px 0", borderBottom: "1px solid var(--line)" }}>
              <div className="spread"><span>#{j.id} {j.kind} <span className={`badge ${j.status === "succeeded" ? "ok" : j.status === "failed" ? "bad" : "accent"}`}>{j.status}</span></span>
                <span className="tiny muted">attempt {j.attempts}/{j.max_attempts} · {j.stage}</span></div>
              {j.last_error && <div className="tiny" style={{ color: "var(--bad)" }}>{j.last_error}</div>}
              {j.result && <div className="tiny muted">{j.result.pages} pages · {j.result.chapters} chapters · {j.result.content_chunks} indexed passages{j.result.ocr_pages ? ` · ${j.result.ocr_pages} OCR pages` : ""}{j.result.warnings?.length ? ` · ${j.result.warnings.join("; ")}` : ""}</div>}
            </div>
          ))}
        </div>
        <div className="card">
          <h2>History</h2>
          {d.events.map((e) => (
            <div key={e.id} className="small" style={{ padding: "4px 0" }}>
              <span className="tiny muted">{(e.created_at || "").slice(0, 16).replace("T", " ")}</span> <b>{e.action}</b> → {e.to_status}
              {e.actor && <span className="muted"> by {e.actor}</span>}{e.reason && <div className="tiny">“{e.reason}”</div>}
            </div>
          ))}
          {d.reviews.length > 0 && <>
            <div className="lbl" style={{ marginTop: 10 }}>Review decisions</div>
            {d.reviews.map((rv) => <div key={rv.id} className="tiny">{rv.decision} by {rv.reviewer || "?"}{rv.notes ? `: ${rv.notes}` : ""}</div>)}
          </>}
        </div>
      </div>
    </Guard>
  );
}

// ---------------------------------------------------------------------------
export function ManageJobs() {
  const [st, setSt] = useState("");
  const [jobs, setJobs] = useState<Res[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = () => api.get<{ jobs: Res[] }>(`/manage/jobs?status=${st}`).then((r) => setJobs(r.jobs)).catch((e) => setErr(e.message));
  useEffect(() => { load(); }, [st]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <Guard perm="jobs.manage">
      <SubNav here="/manage/jobs" />
      <h1>Processing jobs</h1>
      <div className="card row">
        <select className="field" value={st} onChange={(e) => setSt(e.target.value)} aria-label="Status">
          <option value="">All</option>{["queued", "running", "succeeded", "failed", "cancelled"].map((s) => <option key={s}>{s}</option>)}
        </select>
        <button className="btn" onClick={load}>Refresh</button>
      </div>
      {err && <div className="err">{err}</div>}
      {jobs && jobs.length === 0 && <div className="card empty">No jobs.</div>}
      {jobs && jobs.length > 0 && (
        <div className="card scroll"><table>
          <thead><tr><th>Job</th><th>Resource</th><th>Status</th><th>Detail</th><th></th></tr></thead>
          <tbody>{jobs.map((j) => (
            <tr key={j.id}>
              <td className="small">#{j.id} {j.kind}<div className="tiny muted">{(j.created_at || "").slice(0, 16).replace("T", " ")}</div></td>
              <td><a href={`#/manage/res/${j.resource_id}`}>{j.title}</a></td>
              <td><span className={`badge ${j.status === "succeeded" ? "ok" : j.status === "failed" ? "bad" : "accent"}`}>{j.status}</span><div className="tiny muted">attempt {j.attempts}/{j.max_attempts}</div></td>
              <td className="tiny">{j.last_error || (j.result ? `${j.result.pages} pages, ${j.result.chunks} chunks` : j.stage)}</td>
              <td>{j.status === "failed" && <button className="btn" onClick={() => api.post(`/manage/jobs/${j.id}/retry`, {}).then(load).catch((e) => setErr(e.message))}>Retry</button>}</td>
            </tr>))}
          </tbody></table></div>
      )}
    </Guard>
  );
}

export function ManageAudit() {
  const [items, setItems] = useState<Res[] | null>(null);
  const [target, setTarget] = useState("");
  const [action, setAction] = useState("");
  const [page, setPage] = useState(1);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.get<{ items: Res[] }>(`/manage/audit?target_id=${encodeURIComponent(target)}&action=${encodeURIComponent(action)}&page=${page}`)
      .then((r) => setItems(r.items)).catch((e) => setErr(e.message));
  }, [target, action, page]);
  return (
    <Guard perm="audit.view">
      <SubNav here="/manage/audit" />
      <h1>Audit history</h1>
      <p className="lede">Administrative and security events. Book content, passwords and search queries are never written here.</p>
      <div className="card row">
        <input className="field" style={{ flex: 1 }} placeholder="Resource or user id" value={target} onChange={(e) => { setTarget(e.target.value); setPage(1); }} aria-label="Target id" />
        <input className="field" style={{ flex: 1 }} placeholder="Action prefix, e.g. access.denied" value={action} onChange={(e) => { setAction(e.target.value); setPage(1); }} aria-label="Action" />
      </div>
      {err && <div className="err">{err}</div>}
      {items && (
        <div className="card scroll"><table>
          <thead><tr><th>When</th><th>Who</th><th>Action</th><th>Target</th><th>Outcome</th></tr></thead>
          <tbody>{items.map((a) => (
            <tr key={a.id}>
              <td className="tiny">{(a.created_at || "").slice(0, 19).replace("T", " ")}</td><td className="small">{a.actor || <span className="muted">system / anonymous</span>}</td>
              <td className="small mono">{a.action}</td><td className="small">{a.target_type} {a.target_id}</td>
              <td><span className={`badge ${a.outcome === "ok" ? "ok" : "warn"}`}>{a.outcome}</span></td>
            </tr>))}
          </tbody></table></div>
      )}
      <div className="row" style={{ justifyContent: "center", marginTop: 10 }}>
        <button className="btn" disabled={page <= 1} onClick={() => setPage(page - 1)}>Newer</button>
        <button className="btn" disabled={!items || items.length < 50} onClick={() => setPage(page + 1)}>Older</button>
      </div>
    </Guard>
  );
}
