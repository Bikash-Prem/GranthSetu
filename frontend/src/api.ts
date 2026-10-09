export type Scores = { fused: number; bm25: number; semantic: number | null; rerank: number | null };

export type AccessInfo = {
  policy: string;
  policy_text: string;
  can_read: boolean;
  can_download: boolean;
  reason: string;
  needs_login: boolean;
  external_url: string | null;
};

export type Result = {
  access: "open" | "authorised" | "paid";
  policy: AccessInfo;
  managed: boolean;
  page_no: number | null;
  snippet_kind: "content" | "metadata";
  rank: number;
  id: number;
  title: string;
  source: string;
  kind: string;
  url: string;
  lang: string;
  lang_name: string;
  cross_lingual: boolean;
  licence: string;
  licence_url: string | null;
  attribution: string | null;
  author: string | null;
  topic_key: string | null;
  passage: string;
  passage_id: number;
  scores: Scores;
  reason: string | null;
};

export type Sentence = {
  pid: number;
  quote: string;
  source_text: string;
  display_text: string;
  verified: boolean;
  checks: { quote_found: boolean; lexical: number; semantic: number | null; missing_numbers: string[] };
  rejection_reason: string | null;
  translated?: boolean;
};

export type TraceStep = { step: string; status: string; ms: number; [k: string]: unknown };

export type SearchResponse = {
  query: string;
  mode: string;
  language: string;
  intent: string | null;
  subject: string | null;
  topic: string | null;
  expansions: string[];
  confidence: "high" | "medium" | "low" | "none";
  decision: string;
  results: Result[];
  restricted_results: Result[];
  explanation: { status: string; sentences: Sentence[]; pass_rate: number | null; language?: string; translation?: string; scope?: string };
  gap: { id: number; topic: string; status: string; hits: number } | null;
  live_fetch_job: string | null;
  trace: TraceStep[];
  ai: { provider: string; model: string | null; data_sent_to_google: boolean };
  index_version: number;
  served_by: string;
  cached: boolean;
  timings: { total_ms: number };
};

export type ScanResponse = {
  language: string;
  subject: string;
  topic: string;
  key_terms: string[];
  questions: string[];
  extracted_text: string;
  search_query: string;
  readability: "good" | "partial" | "unreadable";
  model: string;
  provider: string;
  ms: number;
  privacy: string;
};

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

const authListeners = new Set<() => void>();
/** Pages subscribe to learn that the session ended (expired or revoked) so they can show the sign-in prompt. */
export function onAuthChange(fn: () => void): () => void {
  authListeners.add(fn);
  return () => authListeners.delete(fn);
}
export function authChanged() {
  authListeners.forEach((f) => f());
}

async function handle<T>(r: Response): Promise<T> {
  if (r.status === 401) authChanged();
  if (!r.ok) {
    let msg = `Request failed (${r.status})`;
    try {
      const j = await r.json();
      if (typeof j.detail === "string") msg = j.detail;
      else if (Array.isArray(j.detail)) msg = j.detail.map((d: { msg: string }) => d.msg).join("; ");
    } catch {
      /* not json */
    }
    throw new ApiError(r.status, msg);
  }
  return r.json() as Promise<T>;
}

// Every state-changing request carries this header; the server rejects cookie-authenticated writes without it (CSRF guard).
const CSRF = { "X-GranthSetu-CSRF": "1" };
const send = <T,>(method: string, path: string, body?: unknown) =>
  fetch(`/api${path}`, {
    method,
    credentials: "same-origin",
    headers: body === undefined ? CSRF : { "Content-Type": "application/json", ...CSRF },
    body: body === undefined ? undefined : JSON.stringify(body),
  }).then((r) => handle<T>(r));

export const api = {
  get: <T,>(path: string) => fetch(`/api${path}`, { credentials: "same-origin" }).then((r) => handle<T>(r)),
  post: <T,>(path: string, body: unknown) => send<T>("POST", path, body),
  put: <T,>(path: string, body: unknown) => send<T>("PUT", path, body),
  patch: <T,>(path: string, body: unknown) => send<T>("PATCH", path, body),
  del: <T,>(path: string) => send<T>("DELETE", path),
  upload: <T,>(path: string, file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return fetch(`/api${path}`, { method: "POST", body: fd, headers: CSRF, credentials: "same-origin" }).then((r) => handle<T>(r));
  },
};

export type User = { id: number; email: string; display_name: string; roles: string[]; permissions: string[]; institutions: number[]; groups: number[] };

export const POLICY_LABEL: Record<string, [string, string]> = {
  OPEN_ACCESS: ["ok", "Open access"],
  PUBLIC_METADATA_ONLY: ["info", "Catalogue record only"],
  REGISTERED_USERS: ["info", "Signed-in readers"],
  INSTITUTION_ONLY: ["warn", "Institution members"],
  GROUP_RESTRICTED: ["warn", "Group members"],
  INDIVIDUAL_ENTITLEMENT: ["warn", "Named readers only"],
  EXTERNAL_PROVIDER_ACCESS: ["info", "At the provider"],
  PRIVATE: ["bad", "Private"],
  UNPUBLISHED: ["bad", "Unpublished"],
};

export type LiveEvent = { type: string; [k: string]: unknown };

/** One shared SSE connection with auto-reconnect (EventSource does it natively). */
const listeners = new Set<(e: LiveEvent) => void>();
const statusListeners = new Set<(ok: boolean) => void>();
let source: EventSource | null = null;

export function subscribe(fn: (e: LiveEvent) => void): () => void {
  listeners.add(fn);
  if (!source) {
    source = new EventSource("/api/events");
    source.onmessage = (m) => {
      try {
        const ev = JSON.parse(m.data) as LiveEvent;
        listeners.forEach((l) => l(ev));
      } catch {
        /* ignore */
      }
    };
    source.onopen = () => statusListeners.forEach((l) => l(true));
    source.onerror = () => statusListeners.forEach((l) => l(false));
  }
  return () => listeners.delete(fn);
}

export function onConnection(fn: (ok: boolean) => void): () => void {
  statusListeners.add(fn);
  return () => statusListeners.delete(fn);
}

export const LANGS: Record<string, string> = { en: "English", hi: "हिन्दी", kn: "ಕನ್ನಡ" };
