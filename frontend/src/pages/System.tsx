import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api";

type Sys = {
  instance: string; uptime_s: number; mode: string;
  database: { engine: string; read_write_split: boolean; replication: { enabled: boolean; replica_in_recovery?: boolean; lag_seconds?: number; replay_lag_bytes?: number; error?: string }; partitioned_by: string | null; last_refresh: string | null };
  bus: { engine: string; queue_depth: number; cache_hits: number; cache_misses: number; rate_limited: number };
  index: { version: number; passages: number; vectors: number; skipped_embeddings: number; build_ms: number; built_from: string; embedder: string };
  ai: { provider: string; model: string; vision_model: string; data_leaves_device: boolean; last_error: string | null };
  embedder: { name: string; open_weight: boolean; semantic: boolean };
  limits: { search_per_min: number; scan_per_min: number };
};

function KV({ k, v }: { k: string; v: ReactNode }) {
  return <div className="spread small" style={{ padding: "4px 0", borderBottom: "1px solid var(--line)" }}><span className="muted">{k}</span><span>{v}</span></div>;
}

export default function SystemPage() {
  const [s, setS] = useState<Sys | null>(null);
  const [lb, setLb] = useState<Record<string, number> | null>(null);
  const [probing, setProbing] = useState(false);

  useEffect(() => {
    const f = () => api.get<Sys>("/system").then(setS).catch(() => setS(null));
    f();
    const t = window.setInterval(f, 5000);
    return () => window.clearInterval(t);
  }, []);

  const probe = async () => {
    setProbing(true);
    const counts: Record<string, number> = {};
    for (let i = 0; i < 12; i++) {
      const r = await fetch("/api/health", { cache: "no-store" });
      const who = r.headers.get("X-Served-By") || "?";
      counts[who] = (counts[who] || 0) + 1;
    }
    setLb(counts);
    setProbing(false);
  };

  if (!s) return <div className="card empty" style={{ marginTop: 40 }}>Connecting to the API…</div>;
  const hitRate = s.bus.cache_hits + s.bus.cache_misses ? Math.round((100 * s.bus.cache_hits) / (s.bus.cache_hits + s.bus.cache_misses)) : 0;

  return (
    <div className="stack">
      <h1>System</h1>
      <p className="lede" style={{ marginBottom: 0 }}>
        Live view of the running architecture: gateway and load balancer, stateless API replicas, Postgres primary and read
        replica, Redis cache, queue and pub/sub, and the ingestion worker. Refreshes every 5 s; values are for the instance that answered.
      </p>
      <div className="grid3">
        <div className="card">
          <h2>Load balancing</h2>
          <KV k="Answered by" v={<span className="mono">{s.instance}</span>} />
          <KV k="Deployment mode" v={s.mode} />
          <KV k="Uptime" v={`${Math.floor(s.uptime_s / 60)} min`} />
          <button className="btn" style={{ marginTop: 10 }} onClick={probe} disabled={probing}>{probing ? "Probing…" : "Send 12 requests"}</button>
          {lb && <div style={{ marginTop: 8 }}>{Object.entries(lb).map(([k, n]) => <KV key={k} k={k} v={`${n} requests`} />)}</div>}
        </div>
        <div className="card">
          <h2>Database</h2>
          <KV k="Engine" v={s.database.engine} />
          <KV k="Read/write split" v={s.database.read_write_split ? "primary writes · replica reads" : "single node"} />
          {s.database.replication.enabled && <KV k="Replica lag" v={s.database.replication.error ? "error" : `${s.database.replication.lag_seconds}s · ${s.database.replication.replay_lag_bytes ?? 0} B`} />}
          <KV k="Partitioned (sharded) by" v={s.database.partitioned_by || "—"} />
          <KV k="Last full refresh" v={s.database.last_refresh || "—"} />
        </div>
        <div className="card">
          <h2>Cache, queue, limits</h2>
          <KV k="Engine" v={s.bus.engine} />
          <KV k="Cache hit rate (this instance)" v={`${hitRate}% (${s.bus.cache_hits}/${s.bus.cache_hits + s.bus.cache_misses})`} />
          <KV k="Jobs waiting" v={s.bus.queue_depth} />
          <KV k="Requests rate-limited" v={s.bus.rate_limited} />
          <KV k="Search limit" v={`${s.limits.search_per_min}/min per client`} />
        </div>
        <div className="card">
          <h2>Search index</h2>
          <KV k="Version" v={`v${s.index.version}`} />
          <KV k="Passages (BM25)" v={s.index.passages} />
          <KV k="Vectors (FAISS)" v={s.index.vectors} />
          <KV k="Built from" v={s.index.built_from} />
          <KV k="Build time" v={`${s.index.build_ms} ms`} />
        </div>
        <div className="card">
          <h2>Models</h2>
          <KV k="Reasoning" v={s.ai.provider === "none" ? "off" : `${s.ai.model} (${s.ai.provider})`} />
          <KV k="Vision" v={s.ai.provider === "none" ? "off" : s.ai.vision_model} />
          <KV k="Embeddings" v={`${s.embedder.name}${s.embedder.open_weight ? " · open weights" : ""}${s.embedder.semantic ? "" : " · lexical fallback"}`} />
          <KV k="Query data leaves server" v={s.ai.data_leaves_device ? "yes, to Gemini API" : "no"} />
          {s.ai.last_error && <div className="tiny" style={{ color: "var(--bad)", marginTop: 6 }}>{s.ai.last_error}</div>}
        </div>
      </div>
    </div>
  );
}
