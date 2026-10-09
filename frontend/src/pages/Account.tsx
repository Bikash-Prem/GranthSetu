import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { go } from "../nav";
import { refreshSession, useSession } from "../session";

export function LoginPage({ next }: { next: string }) {
  const { registrationOpen } = useSession();
  const [mode, setMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setErr(null);
    if (mode === "register" && password.length < 10) return setErr("Use at least 10 characters for your password.");
    setBusy(true);
    try {
      await api.post(mode === "login" ? "/auth/login" : "/auth/register",
        mode === "login" ? { email, password } : { email, password, display_name: name });
      await refreshSession();
      go(next || "/");
    } catch (x) {
      setErr(x instanceof ApiError ? x.message : "Could not reach the server.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ maxWidth: 420, margin: "0 auto" }}>
      <h1>{mode === "login" ? "Sign in" : "Create an account"}</h1>
      <p className="lede">Signing in lets you read books your library or institution has given you access to, and keeps your bookmarks and reading progress.</p>
      <form className="card stack" onSubmit={submit} noValidate>
        {mode === "register" && (
          <div>
            <label className="lbl" htmlFor="nm">Name</label>
            <input id="nm" className="field" value={name} maxLength={80} onChange={(e) => setName(e.target.value)} autoComplete="name" />
          </div>
        )}
        <div>
          <label className="lbl" htmlFor="em">Email</label>
          <input id="em" className="field" type="email" required value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" />
        </div>
        <div>
          <label className="lbl" htmlFor="pw">Password</label>
          <input id="pw" className="field" type="password" required value={password} onChange={(e) => setPassword(e.target.value)}
            autoComplete={mode === "login" ? "current-password" : "new-password"} />
          {mode === "register" && <div className="tiny muted" style={{ marginTop: 4 }}>At least 10 characters.</div>}
        </div>
        {err && <div className="err" role="alert">{err}</div>}
        <button className="btn primary" disabled={busy || !email || !password}>{busy ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}</button>
        {registrationOpen && (
          <button type="button" className="btn ghost" onClick={() => { setMode(mode === "login" ? "register" : "login"); setErr(null); }}>
            {mode === "login" ? "New here? Create an account" : "Have an account? Sign in"}
          </button>
        )}
        {!registrationOpen && <div className="tiny muted">Accounts are created by your librarian.</div>}
      </form>
    </div>
  );
}

type MyLib = {
  reading: { resource_id: number; page_no: number; updated_at: string; title: string | null; can_read: boolean }[];
  bookmarks: { id: number; resource_id: number; page_no: number; note: string | null; title: string | null; can_read: boolean }[];
};

export function AccountPage() {
  const { user, loaded } = useSession();
  const [lib, setLib] = useState<MyLib | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [pw, setPw] = useState({ current_password: "", new_password: "" });
  const [msg, setMsg] = useState<string | null>(null);

  const load = () => api.get<MyLib>("/me/library").then(setLib).catch((e) => setErr(e.message));
  useEffect(() => { if (user) load(); }, [user?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!loaded) return <div className="empty">Loading…</div>;
  if (!user) return <div className="card empty" style={{ marginTop: 40 }}>You are signed out. <a href="#/login?next=/account">Sign in</a></div>;

  const logout = async () => {
    await api.post("/auth/logout", {});
    await refreshSession();
    go("/");
  };
  const changePw = async () => {
    setMsg(null);
    try {
      await api.post("/auth/password", pw);
      setMsg("Password changed. Other devices were signed out.");
      setPw({ current_password: "", new_password: "" });
    } catch (e) {
      setMsg(e instanceof ApiError ? e.message : "Failed.");
    }
  };
  const wipe = async () => {
    if (!window.confirm("Delete your reading history and bookmarks? This cannot be undone.")) return;
    await api.del("/me/data");
    load();
  };

  return (
    <div className="stack">
      <h1>My library</h1>
      <div className="spread">
        <div>
          <div><b>{user.display_name}</b> · {user.email}</div>
          <div className="row" style={{ gap: 6, marginTop: 6 }}>{user.roles.map((r) => <span key={r} className="badge">{r.replace("_", " ")}</span>)}</div>
        </div>
        <button className="btn" onClick={logout}>Sign out</button>
      </div>
      {err && <div className="err">{err}</div>}
      <div className="grid2">
        <div className="card">
          <h2>Continue reading</h2>
          {lib && lib.reading.length === 0 && <div className="small muted">Nothing yet. Open a book from the digital library.</div>}
          {lib?.reading.map((r) => (
            <div key={r.resource_id} className="spread small" style={{ padding: "6px 0", borderBottom: "1px solid var(--line)" }}>
              {r.title ? <a href={`#/book/${r.resource_id}?page=${r.page_no}`}>{r.title}</a> : <span className="muted">A resource you no longer have access to</span>}
              <span className="muted">page {r.page_no}{!r.can_read && r.title ? " · access ended" : ""}</span>
            </div>
          ))}
        </div>
        <div className="card">
          <h2>Bookmarks</h2>
          {lib && lib.bookmarks.length === 0 && <div className="small muted">No bookmarks yet.</div>}
          {lib?.bookmarks.map((b) => (
            <div key={b.id} className="spread small" style={{ padding: "6px 0", borderBottom: "1px solid var(--line)" }}>
              <span>
                {b.title && b.can_read ? <a href={`#/book/${b.resource_id}?page=${b.page_no}`}>{b.title}, p. {b.page_no}</a>
                  : <span className="muted">{b.title || "Unavailable resource"} (access ended)</span>}
                {b.note && <div className="tiny muted">{b.note}</div>}
              </span>
              <button className="btn ghost small" onClick={() => api.del(`/me/bookmarks/${b.id}`).then(load)} aria-label="Delete bookmark">✕</button>
            </div>
          ))}
        </div>
      </div>
      <div className="grid2">
        <div className="card stack">
          <h2 style={{ margin: 0 }}>Change password</h2>
          <input className="field" type="password" placeholder="Current password" aria-label="Current password" autoComplete="current-password"
            value={pw.current_password} onChange={(e) => setPw({ ...pw, current_password: e.target.value })} />
          <input className="field" type="password" placeholder="New password (10+ characters)" aria-label="New password" autoComplete="new-password"
            value={pw.new_password} onChange={(e) => setPw({ ...pw, new_password: e.target.value })} />
          <button className="btn" disabled={!pw.current_password || pw.new_password.length < 10} onClick={changePw}>Change password</button>
          {msg && <div className="small">{msg}</div>}
        </div>
        <div className="card stack">
          <h2 style={{ margin: 0 }}>Your data</h2>
          <p className="small muted" style={{ margin: 0 }}>GranthSetu stores your reading position and bookmarks. Search queries are not stored with your account. Photos you scan are never stored.</p>
          <button className="btn" onClick={wipe}>Delete my reading history and bookmarks</button>
        </div>
      </div>
    </div>
  );
}
