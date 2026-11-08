"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type InferenceRow } from "@/lib/api";
import { useUser } from "@/lib/session";
import { Badge, EmptyState, Err, Loading, PageHeader } from "@/components/ui";
import { IconServer } from "@/components/icons";

export default function Inference() {
  const user = useUser();
  const [rows, setRows] = useState<InferenceRow[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const load = useCallback(() => api<{ deployments: InferenceRow[] }>("/admin/inference")
    .then((r) => setRows(r.deployments)).catch(() => setRows([])), []);
  useEffect(() => { load(); const t = setInterval(load, 10000); return () => clearInterval(t); }, [load]);

  async function check() {
    setBusy(true); setError("");
    try { await api("/admin/inference/check", { method: "POST" }); await load(); }
    catch (e) { setError(e instanceof ApiError ? e.message : "Health check failed"); }
    setBusy(false);
  }
  if (user.role !== "super_admin") return <EmptyState icon={<IconServer />} title="Administrators only" />;
  return (
    <>
      <PageHeader title="Inference Servers"
        subtitle="Health is checked through LiteLLM every 30 seconds. An unhealthy model raises a critical alert and clears itself on recovery."
        actions={<button onClick={check} disabled={busy}>{busy ? "Checking…" : "Check now"}</button>} />
      <Err>{error}</Err>
      <div className="card flush">
        {rows === null ? <Loading /> : rows.length === 0 ? (
          <EmptyState icon={<IconServer />} title="No enabled models" hint="Register a model first." />
        ) : (
          <div className="table-wrap"><table>
            <thead><tr><th>Model</th><th>Status</th><th>Queued</th><th>Running</th><th>Limits</th><th>Last check</th></tr></thead>
            <tbody>{rows.map((r) => (
              <tr key={r.deployment_id}><td><b>{r.model}</b></td><td><Badge value={r.status} /></td>
                <td>{r.queued}</td><td>{r.active}</td>
                <td className="muted">{r.max_active} running · {r.max_queue} queued</td>
                <td className="muted">{r.last_check
                  ? `${new Date(r.last_check.checked_at).toLocaleTimeString()} · ${r.last_check.latency_ms} ms · ${r.last_check.detail}`
                  : "not checked yet"}</td></tr>
            ))}</tbody>
          </table></div>
        )}
      </div>
    </>
  );
}
