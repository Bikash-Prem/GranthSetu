import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { useSession } from "../session";

type U = { id: number; email: string; display_name: string; is_active: boolean; roles: string[]; last_login_at: string | null };
type Member = { id: number; email: string; member_role?: string; expires_at: string | null };
type Org = { id: number; name: string; institution_id?: number | null; members: Member[]; can_manage: boolean };

export default function AdminPage() {
  const { user, loaded, can } = useSession();
  const [q, setQ] = useState("");
  const [users, setUsers] = useState<{ users: U[]; roles: string[]; can_manage_roles: boolean } | null>(null);
  const [orgs, setOrgs] = useState<{ institutions: Org[]; groups: Org[]; can_create: boolean } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [newInst, setNewInst] = useState("");
  const [newGroup, setNewGroup] = useState({ name: "", institution_id: "" });
  const [add, setAdd] = useState<Record<string, { email: string; expires_at: string; admin: boolean }>>({});

  const loadUsers = () => api.get<typeof users>(`/admin/users?q=${encodeURIComponent(q)}`).then(setUsers).catch((e) => setErr(e.message));
  const loadOrgs = () => api.get<typeof orgs>("/admin/orgs").then(setOrgs).catch((e) => setErr(e.message));
  useEffect(() => { if (user) loadUsers(); }, [q, user?.id]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (user) loadOrgs(); }, [user?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const act = async (fn: () => Promise<unknown>, ok: string) => {
    setErr(null);
    setMsg(null);
    try { await fn(); setMsg(ok); loadUsers(); loadOrgs(); } catch (e) { setErr(e instanceof ApiError ? e.message : "Failed."); }
  };

  if (!loaded) return <div className="empty">Loading…</div>;
  if (!user) return <div className="card empty" style={{ marginTop: 40 }}><a href="#/login?next=/admin">Sign in</a> to manage users.</div>;
  if (!(can("user.manage") || can("entitlement.manage") || can("institution.manage_own"))) return <div className="card empty" style={{ marginTop: 40 }}>You do not have permission to manage users.</div>;

  const memberForm = (kind: "institutions" | "groups", o: Org) => {
    const k = `${kind}:${o.id}`;
    const v = add[k] || { email: "", expires_at: "", admin: false };
    return (
      <div className="row" style={{ marginTop: 6 }}>
        <input className="field" style={{ flex: 1 }} placeholder="member@college.edu" value={v.email} onChange={(e) => setAdd({ ...add, [k]: { ...v, email: e.target.value } })} aria-label="Member email" />
        <input className="field" type="date" value={v.expires_at} onChange={(e) => setAdd({ ...add, [k]: { ...v, expires_at: e.target.value } })} aria-label="Membership ends (optional)" />
        {kind === "institutions" && can("institution.manage") && <label className="small row" style={{ gap: 4 }}><input type="checkbox" checked={v.admin} onChange={(e) => setAdd({ ...add, [k]: { ...v, admin: e.target.checked } })} /> admin</label>}
        <button className="btn" disabled={!v.email} onClick={() => act(() => api.post(`/admin/${kind}/${o.id}/members`, {
          email: v.email, expires_at: v.expires_at ? new Date(v.expires_at + "T23:59:59").toISOString() : null, member_role: v.admin ? "admin" : "member",
        }), "Member added.").then(() => setAdd({ ...add, [k]: { email: "", expires_at: "", admin: false } }))}>Add</button>
      </div>
    );
  };

  return (
    <div className="stack">
      <h1>Users and institutions</h1>
      {msg && <div className="card small" role="status" style={{ background: "var(--ok-soft)" }}>{msg}</div>}
      {err && <div className="err" role="alert">{err}</div>}
      <div className="card stack">
        <div className="spread"><h2 style={{ margin: 0 }}>Users</h2>
          <input className="field" style={{ maxWidth: 280 }} placeholder="Search email or name" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search users" /></div>
        {!users?.can_manage_roles && <div className="small muted">Only platform administrators can change roles.</div>}
        <div className="scroll"><table>
          <thead><tr><th>User</th><th>Roles</th><th>Status</th></tr></thead>
          <tbody>{users?.users.map((u) => (
            <tr key={u.id}>
              <td className="small">{u.display_name}<div className="tiny muted">{u.email} · #{u.id}</div></td>
              <td>
                <div className="row" style={{ gap: 4 }}>
                  {u.roles.map((r) => (
                    <span key={r} className="badge">{r.replace("_", " ")}
                      {users.can_manage_roles && <button className="btn ghost" style={{ padding: "0 4px" }} aria-label={`Remove ${r}`}
                        onClick={() => act(() => api.del(`/admin/users/${u.id}/roles/${r}`), `Removed ${r}.`)}>✕</button>}
                    </span>
                  ))}
                  {users.can_manage_roles && (
                    <select className="field" value="" onChange={(e) => e.target.value && act(() => api.post(`/admin/users/${u.id}/roles`, { role: e.target.value }), `Granted ${e.target.value}.`)} aria-label="Add role">
                      <option value="">+ role</option>{users.roles.filter((r) => !u.roles.includes(r)).map((r) => <option key={r} value={r}>{r}</option>)}
                    </select>
                  )}
                </div>
              </td>
              <td>
                <span className={`badge ${u.is_active ? "ok" : "bad"}`}>{u.is_active ? "active" : "deactivated"}</span>
                {users.can_manage_roles && u.id !== user.id && <button className="btn ghost small" onClick={() => act(() => api.post(`/admin/users/${u.id}/active`, { active: !u.is_active }), u.is_active ? "Deactivated; sessions revoked." : "Reactivated.")}>{u.is_active ? "Deactivate" : "Activate"}</button>}
              </td>
            </tr>))}
          </tbody></table></div>
      </div>

      <div className="grid2">
        <div className="card stack">
          <h2 style={{ margin: 0 }}>Institutions</h2>
          {orgs?.can_create && (
            <div className="row"><input className="field" style={{ flex: 1 }} placeholder="New institution name" value={newInst} onChange={(e) => setNewInst(e.target.value)} aria-label="New institution" />
              <button className="btn" disabled={newInst.trim().length < 2} onClick={() => act(() => api.post("/admin/institutions", { name: newInst }), "Institution created.").then(() => setNewInst(""))}>Create</button></div>
          )}
          {orgs?.institutions.map((i) => (
            <div key={i.id} style={{ borderTop: "1px solid var(--line)", paddingTop: 8 }}>
              <b>{i.name}</b> <span className="tiny muted">#{i.id} · {i.members.length} members</span>
              {i.members.map((m) => (
                <div key={m.id} className="spread small"><span>{m.email}{m.member_role === "admin" ? " (admin)" : ""}{m.expires_at ? <span className="tiny muted"> · until {m.expires_at.slice(0, 10)}</span> : null}</span>
                  {i.can_manage && <button className="btn ghost small" onClick={() => act(() => api.del(`/admin/institutions/${i.id}/members/${m.id}`), "Member removed.")}>Remove</button>}</div>
              ))}
              {i.can_manage && memberForm("institutions", i)}
            </div>
          ))}
        </div>
        <div className="card stack">
          <h2 style={{ margin: 0 }}>Groups</h2>
          {(orgs?.can_create || user) && (
            <div className="row">
              <input className="field" style={{ flex: 1 }} placeholder="New group name" value={newGroup.name} onChange={(e) => setNewGroup({ ...newGroup, name: e.target.value })} aria-label="New group" />
              <select className="field" value={newGroup.institution_id} onChange={(e) => setNewGroup({ ...newGroup, institution_id: e.target.value })} aria-label="Institution">
                <option value="">No institution</option>{orgs?.institutions.map((i) => <option key={i.id} value={i.id}>{i.name}</option>)}
              </select>
              <button className="btn" disabled={newGroup.name.trim().length < 2} onClick={() => act(() => api.post("/admin/groups", { name: newGroup.name, institution_id: Number(newGroup.institution_id) || null }), "Group created.").then(() => setNewGroup({ name: "", institution_id: "" }))}>Create</button>
            </div>
          )}
          {orgs?.groups.map((g) => (
            <div key={g.id} style={{ borderTop: "1px solid var(--line)", paddingTop: 8 }}>
              <b>{g.name}</b> <span className="tiny muted">#{g.id} · {g.members.length} members</span>
              {g.members.map((m) => (
                <div key={m.id} className="spread small"><span>{m.email}{m.expires_at ? <span className="tiny muted"> · until {m.expires_at.slice(0, 10)}</span> : null}</span>
                  {g.can_manage && <button className="btn ghost small" onClick={() => act(() => api.del(`/admin/groups/${g.id}/members/${m.id}`), "Member removed.")}>Remove</button>}</div>
              ))}
              {g.can_manage && memberForm("groups", g)}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
