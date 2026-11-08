"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api, type AuditLog } from "@/lib/api";
import { EmptyState, Loading, PageHeader } from "@/components/ui";
import { IconShield } from "@/components/icons";

export default function Audit() {
  const [rows, setRows] = useState<AuditLog[] | null>(null);
  const [query, setQuery] = useState("");
  const load = useCallback((qs: string) => {
    api<AuditLog[]>(`/admin/audit-logs?limit=100${qs}`).then(setRows).catch(() => setRows([]));
  }, []);
  useEffect(() => { load(""); }, [load]);

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
            <thead><tr><th>Time</th><th>Action</th><th>Entity</th><th>Details</th></tr></thead>
            <tbody>{rows.map((a) => (
              <tr key={a.id}><td className="muted">{new Date(a.created_at).toLocaleString()}</td>
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
