"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, ApiError, type AuditLog, type ChainStatus, type RetentionInfo } from "@/lib/api";
import { Badge, EmptyState, Err, Loading, PageHeader } from "@/components/ui";
import { IconShield } from "@/components/icons";

export default function Audit() {
  const [rows, setRows] = useState<AuditLog[] | null>(null);
  const [query, setQuery] = useState("");
  const load = useCallback((qs: string) => {
    api<AuditLog[]>(`/admin/audit-logs?limit=100${qs}`).then(setRows).catch(() => setRows([]));
  }, []);
  useEffect(() => { load(""); }, [load]);

  const [chain, setChain] = useState<ChainStatus | null>(null);
  const [ret, setRet] = useState<RetentionInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const loadRet = useCallback(() => { api<RetentionInfo>("/admin/retention").then(setRet).catch(() => {}); }, []);
  useEffect(() => { loadRet(); }, [loadRet]);
  async function act(fn: () => Promise<unknown>) {
    setBusy(true); setError("");
    try { await fn(); } catch (e) { setError(e instanceof ApiError ? e.message : "Request failed"); }
    setBusy(false);
  }
  const verify = (qs = "") => act(async () => { setChain(await api<ChainStatus>(`/admin/audit-logs/verify${qs}`)); });

  function search(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    const p = new URLSearchParams();
    for (const k of ["q", "entity_type", "from", "to"]) {
      const v = String(f.get(k) ?? "").trim();
      if (!v) continue;
      p.set(k, k === "from" || k === "to" ? new Date(k === "to" ? `${v}T23:59:59` : v).toISOString() : v);
    }
    setRows(null); setQuery(p.toString()); load(p.toString() ? `&${p}` : "");
  }
  return (
    <>
      <PageHeader title="Audit Logs" subtitle="Append-only record of privileged actions and sign-ins. Secrets are never logged." />
      <Err>{error}</Err>
      <h2 className="section">Integrity</h2>
      <div className="card form-card">
        <p className="muted" style={{ marginTop: 0 }}>
          Each entry is chained to the one before it, so changing or removing an entry is detectable. Deleting the newest
          entries is only caught if you keep the chain head somewhere else and check against it.
        </p>
        <div className="row">
          <button disabled={busy} onClick={() => verify()}>Verify chain now</button>
          {chain && (chain.ok ? <Badge value="verified" tone="ok" /> : <Badge value="broken" tone="bad" />)}
          {chain && <span className="muted">{chain.checked} entries checked{chain.unsealed ? `, ${chain.unsealed} waiting to be chained` : ""}</span>}
        </div>
        {chain && !chain.ok && chain.problem && <p className="err" role="alert">Entry {chain.problem.seq}: {chain.problem.reason}.</p>}
        {chain?.head && (
          <p className="muted">Chain head — keep this outside the database: entry <b>{chain.head.seq}</b>, hash <code style={{ wordBreak: "break-all" }}>{chain.head.hash}</code></p>
        )}
        <form className="row" onSubmit={(e: FormEvent<HTMLFormElement>) => {
          e.preventDefault();
          const f = new FormData(e.currentTarget);
          const seq = Number(f.get("aseq")), hash = String(f.get("ahash") ?? "").trim();
          if (seq && hash) verify(`?anchor_seq=${seq}&anchor_hash=${encodeURIComponent(hash)}`);
        }}>
          <label>Check against a saved head: entry<input name="aseq" type="number" min={1} style={{ width: 110 }} /></label>
          <label>hash<input name="ahash" style={{ width: 360 }} /></label>
          <button className="ghost" disabled={busy}>Check</button>
        </form>
      </div>

      <h2 className="section">Retention</h2>
      <div className="card form-card">
        <p className="muted" style={{ marginTop: 0 }}>
          How long detailed per-request logs are kept. Request summaries, usage records and this audit log are never deleted.
          0 keeps everything. A daily job applies it.
        </p>
        {ret && (
          <form className="row" onSubmit={(e: FormEvent<HTMLFormElement>) => {
            e.preventDefault();
            const f = new FormData(e.currentTarget);
            act(async () => {
              await api("/admin/retention", { method: "PUT", json: {
                request_events_days: Number(f.get("ev")), routing_decisions_days: Number(f.get("rd")) } });
              loadRet();
            });
          }}>
            <label>Request events (days)<input name="ev" type="number" min={0} max={3650} defaultValue={ret.settings.request_events_days} style={{ width: 110 }} /></label>
            <label>Routing decisions (days)<input name="rd" type="number" min={0} max={3650} defaultValue={ret.settings.routing_decisions_days} style={{ width: 110 }} /></label>
            <button disabled={busy}>Save</button>
            <button type="button" className="ghost" disabled={busy}
              onClick={() => confirm("Delete detail rows older than the retention settings now?") &&
                act(async () => { await api("/admin/maintenance/run", { method: "POST" }); loadRet(); })}>Run maintenance now</button>
          </form>
        )}
        {ret?.last_maintenance && (
          <p className="muted">Last run {new Date(ret.last_maintenance.at).toLocaleString()}: removed{" "}
            {Object.entries(ret.last_maintenance.purged).map(([k, v]) => `${v} ${k.replace("_", " ")}`).join(", ")};
            audit chain {ret.last_maintenance.audit_chain.ok ? "verified" : "BROKEN"}.</p>
        )}
      </div>

      <h2 className="section">Events</h2>
      <form className="card form-card" onSubmit={search}>
        <div className="row">
          <label>Search action or entity ID<input name="q" placeholder="key.revoke, user.suspend…" /></label>
          <label>Entity<select name="entity_type" defaultValue="">
            <option value="">Any</option>
            {["user", "team", "project", "api_key", "model", "quota", "alert", "deployment"].map((t) => <option key={t}>{t}</option>)}
          </select></label>
          <label>From<input name="from" type="date" /></label>
          <label>To<input name="to" type="date" /></label>
          <button>Search</button>
          {query && <button type="button" className="ghost" onClick={() => { setRows(null); setQuery(""); load(""); }}>Clear</button>}
        </div>
      </form>
      <div className="card flush">
        {rows === null ? <Loading /> : rows.length === 0 ? (
          <EmptyState icon={<IconShield />} title="No matching events" />
        ) : (
          <div className="table-wrap"><table>
            <thead><tr><th>#</th><th>Time</th><th>Action</th><th>Entity</th><th>Details</th></tr></thead>
            <tbody>{rows.map((a) => (
              <tr key={a.id}><td className="muted">{a.seq ?? "…"}</td><td className="muted">{new Date(a.created_at).toLocaleString()}</td>
                <td><code>{a.action}</code></td><td>{a.entity_type}</td>
                <td className="muted" style={{ maxWidth: 420, overflow: "hidden", textOverflow: "ellipsis" }}>
                  {JSON.stringify(a.metadata)}</td></tr>
            ))}</tbody>
          </table></div>
        )}
      </div>
    </>
  );
}
