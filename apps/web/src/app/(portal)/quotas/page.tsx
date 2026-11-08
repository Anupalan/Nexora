"use client";

import { useEffect, useState, type FormEvent } from "react";
import { EmptyState, Err, Loading, PageHeader } from "@/components/ui";
import { IconGauge } from "@/components/icons";
import { api, ApiError, type QuotaRow } from "@/lib/api";

const FIELDS = ["requests_per_minute", "requests_per_day", "requests_per_month", "tokens_per_minute",
  "tokens_per_day", "tokens_per_month", "max_concurrent", "max_output_tokens"] as const;

export default function Quotas() {
  const [rows, setRows] = useState<QuotaRow[] | null>(null);
  const [error, setError] = useState("");
  const load = () => api<QuotaRow[]>("/admin/quotas").then(setRows).catch(() => setRows([]));
  useEffect(() => { load(); }, []);

  async function create(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget, f = new FormData(form);
    setError("");
    const json: Record<string, unknown> = { scope_type: f.get("scope_type"), scope_id: f.get("scope_id") || null };
    for (const k of FIELDS) json[k] = f.get(k) ? Number(f.get(k)) : null;
    try { await api("/admin/quotas", { method: "POST", json }); form.reset(); load(); }
    catch (err) { setError(err instanceof ApiError ? err.message : "Failed"); }
  }
  async function edit(q: QuotaRow, field: (typeof FIELDS)[number]) {
    const raw = prompt(`${field} (empty = unlimited)`, String(q[field] ?? ""));
    if (raw === null) return;
    try { await api(`/admin/quotas/${q.id}`, { method: "PATCH", json: { [field]: raw === "" ? null : Number(raw) } }); load(); }
    catch (err) { setError(err instanceof ApiError ? err.message : "Failed"); }
  }
  return (
    <>
      <PageHeader title="Quota Policies" subtitle="A request must pass every applicable scope, so the most restrictive limit wins. Blank means unlimited; concurrency counts queued and running requests." />
      <form className="card form-card" onSubmit={create}><div className="row">
        <label>Scope<select name="scope_type" defaultValue="user">
          {["global", "team", "user", "key", "project"].map((s) => <option key={s}>{s}</option>)}</select></label>
        <label>Scope ID (UUID)<input name="scope_id" placeholder="not needed for global" /></label>
        {FIELDS.map((k) => <label key={k}>{k.replace(/_/g, " ")}<input name={k} type="number" min={1} style={{ width: 110 }} /></label>)}
        <button>Create</button>
      </div></form>
      <Err>{error}</Err>
      <div className="card flush">
        {rows === null ? <Loading /> : rows.length === 0 ? <EmptyState icon={<IconGauge />} title="No quotas set" hint="Without a quota, requests are limited only by the queue." /> : (
          <div className="table-wrap"><table>
            <thead><tr><th>Scope</th>{FIELDS.map((k) => <th key={k}>{k.replace(/_/g, " ")}</th>)}</tr></thead>
            <tbody>{rows.map((q) => (
              <tr key={q.id}><td>{q.scope_type}<br /><span className="muted">{q.scope_id.slice(0, 8)}</span></td>
                {FIELDS.map((k) => <td key={k}><a href="#" onClick={(e) => { e.preventDefault(); edit(q, k); }}>{q[k] ?? "∞"}</a></td>)}</tr>
            ))}</tbody>
          </table></div>
        )}
      </div>
    </>
  );
}
