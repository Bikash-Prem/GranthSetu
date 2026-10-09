import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import "./styles.css";
import { api, onConnection, subscribe } from "./api";
import SearchPage from "./pages/Search";
import ScanPage from "./pages/Scan";
import LibraryPage from "./pages/Library";
import GapsPage from "./pages/Gaps";
import EvalPage from "./pages/Eval";
import SystemPage from "./pages/System";
import LessonPage, { type LessonRequest } from "./pages/Lesson";
import { AccountPage, LoginPage } from "./pages/Account";
import BookPage, { CataloguePage } from "./pages/Book";
import { ManageAudit, ManageDashboard, ManageJobs, ManageList, ManageNew, ManageResource } from "./pages/Manage";
import AdminPage from "./pages/Admin";
import { useSession } from "./session";
import { go } from "./nav";

type Route = { path: string; params: URLSearchParams };

function parse(): Route {
  const h = window.location.hash.replace(/^#/, "") || "/";
  const [path, qs] = h.split("?");
  return { path: path || "/", params: new URLSearchParams(qs || "") };
}


const NAV = [
  ["/", "Search"],
  ["/catalogue", "Digital library"],
  ["/scan", "Scan to learn"],
  ["/library", "Live feed"],
  ["/gaps", "Gap board"],
  ["/eval", "Evaluation"],
  ["/system", "System"],
] as const;

type Sys = { instance: string; ai: { provider: string; model: string }; mode: string; embedder: { name: string } };

function App() {
  const [route, setRoute] = useState<Route>(parse());
  const [sys, setSys] = useState<Sys | null>(null);
  const [live, setLive] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [lesson, setLesson] = useState<LessonRequest | null>(null);

  useEffect(() => {
    const f = () => setRoute(parse());
    window.addEventListener("hashchange", f);
    return () => window.removeEventListener("hashchange", f);
  }, []);

  useEffect(() => {
    api.get<Sys>("/system").then(setSys).catch(() => setSys(null));
    const off1 = onConnection(setLive);
    const off2 = subscribe((e) => {
      if (e.type === "index_updated" && Number(e.added) > 0) {
        setToast(`Live update: ${e.added} new resource${Number(e.added) > 1 ? "s" : ""} added to the library`);
        window.setTimeout(() => setToast(null), 4500);
      }
    });
    return () => {
      off1();
      off2();
    };
  }, []);

  const { user, can } = useSession();
  const p = route.path;
  const seg = p.split("/").filter(Boolean);
  const aiLabel = !sys ? "connecting…" : sys.ai.provider === "none" ? "AI off · retrieval only" : `${sys.ai.provider === "gemini" ? "Gemma via Gemini API" : "Local Gemma"} · ${sys.ai.model}`;

  return (
    <>
      <header className="topbar">
        <div className="topbar-in">
          <a className="brand" href="#/">
            <img src="/favicon.svg" width={26} height={26} alt="" />
            <span>
              Granth<b>Setu</b>
            </span>
          </a>
          <nav className="nav" aria-label="Main">
            {NAV.map(([href, label]) => (
              <a key={href} href={`#${href}`} className={p === href ? "on" : ""}>
                {label}
              </a>
            ))}
            {can("resource.create") && <a href="#/manage" className={p.startsWith("/manage") ? "on" : ""}>Manage</a>}
            {(can("user.manage") || can("institution.manage_own")) && <a href="#/admin" className={p === "/admin" ? "on" : ""}>Admin</a>}
          </nav>
          {user ? <a className="pill" href="#/account" title={user.email}>{user.display_name}</a>
            : <a className="pill" href={`#/login?next=${encodeURIComponent(p)}`}>Sign in</a>}
          <span className="pill" title={sys ? `Embeddings: ${sys.embedder.name} · Served by ${sys.instance}` : ""}>
            <span className={`dot ${live ? "live" : ""}`} />
            {aiLabel}
          </span>
        </div>
      </header>
      <main className="shell">
        {p === "/" && <SearchPage initial={route.params.get("q") || ""} onLesson={(l) => { setLesson(l); go("/lesson"); }} />}
        {p === "/scan" && <ScanPage aiOn={!!sys && sys.ai.provider !== "none"} />}
        {p === "/library" && <LibraryPage />}
        {p === "/gaps" && <GapsPage />}
        {p === "/eval" && <EvalPage />}
        {p === "/system" && <SystemPage />}
        {p === "/lesson" && <LessonPage req={lesson} />}
        {p === "/catalogue" && <CataloguePage />}
        {seg[0] === "book" && seg[1] && <BookPage id={Number(seg[1])} startPage={Number(route.params.get("page")) || null} />}
        {p === "/login" && <LoginPage next={route.params.get("next") || "/"} />}
        {p === "/account" && <AccountPage />}
        {p === "/manage" && <ManageDashboard />}
        {p === "/manage/new" && <ManageNew />}
        {p === "/manage/list" && <ManageList status={route.params.get("status") || ""} />}
        {seg[0] === "manage" && seg[1] === "res" && seg[2] && <ManageResource id={Number(seg[2])} />}
        {p === "/manage/jobs" && <ManageJobs />}
        {p === "/manage/audit" && <ManageAudit />}
        {p === "/admin" && <AdminPage />}
        <footer>
          GranthSetu is open source (MIT). Every result links to its source with its licence or access terms. Restricted books are served only to readers their policy allows; authorised and paid items open on the provider's own site.
          <br />
          Built by Team DevDynasty at Hacktoberfest Hack Day Bengaluru '26.
        </footer>
      </main>
      {toast && <div className="toast" role="status">{toast}</div>}
    </>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
