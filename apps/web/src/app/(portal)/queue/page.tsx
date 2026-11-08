"use client";

import { useCallback, useEffect, useState } from "react";
import { Badge, EmptyState, Err, PageHeader } from "@/components/ui";
import { IconQueue } from "@/components/icons";
import { api, ApiError, type QueueItem } from "@/lib/api";
import { useUser } from "@/lib/session";

interface Overview {
  deployments: { deployment_id: string; model: string; deployment: string; queued: number; active: number;
    config: { max_active_requests?: number; max_queue_length?: number } }[];
}

export default function Queue() {
  const user = useUser();
  const [mine, setMine] = useState<QueueItem[]>([]);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(() => {
    api<QueueItem[]>("/queue/me").then(setMine).catch(() => {});
    if (user.role === "super_admin") api<Overview>("/admin/queue").then(setOverview).catch(() => {});
  }, [user.role]);
  useEffect(() => { load(); const t = setInterval(load, 3000); return () => clearInterval(t); }, [load]);

  async function cancel(id: string) {
    setError("");
    try { await api(`/queue/${id}/cancel`, { method: "POST" }); load(); }
    catch (e) { setError(e instanceof ApiError ? e.message : "Failed"); }
  }
  async function setLimit(id: string, field: "max_active_requests" | "max_queue_length", cur?: number) {
    const v = Number(prompt(`New ${field.replace(/_/g, " ")}`, String(cur ?? "")));
    if (!v) return;
    try { await api(`/admin/deployments/${id}`, { method: "PATCH", json: { [field]: v } }); load(); }
    catch (e) { setError(e instanceof ApiError ? e.message : "Failed"); }
  }
  return (
    <>
      <PageHeader title="Request Queue" subtitle="Requests are admitted first-in, first-out per model. Waiting never holds a GPU slot." />
      <Err>{error}</Err>
      <div className="card flush" style={{ marginBottom: 14 }}>
        <div className="card-head">My pending requests</div>
        {mine.length === 0 ? <EmptyState icon={<IconQueue />} title="Nothing queued or running" /> : (
          <div className="table-wrap"><table>
            <thead><tr><th>Request</th><th>Status</th><th>Submitted</th><th /></tr></thead>
            <tbody>{mine.map((r) => (
              <tr key={r.id}><td><code>{r.id.slice(0, 8)}</code></td><td><Badge value={r.status} /></td>
                <td className="muted">{new Date(r.created_at).toLocaleTimeString()}</td>
                <td className="actions"><button className="danger sm" onClick={() => cancel(r.id)}>Cancel</button></td></tr>
            ))}</tbody>
          </table></div>
        )}
      </div>
      {overview && (
        <div className="card flush">
          <div className="card-head">Deployments <span className="muted" style={{ fontWeight: 400 }}>FIFO within each deployment&apos;s queue, not across models</span></div>
          <div className="table-wrap"><table>
            <thead><tr><th>Model</th><th>Queued</th><th>Active</th><th>Max active</th><th>Max queue</th></tr></thead>
            <tbody>{overview.deployments.map((d) => (
              <tr key={d.deployment_id}><td>{d.model}</td><td>{d.queued}</td><td>{d.active}</td>
                <td>{d.config.max_active_requests ?? "default"}{" "}
                  <button className="ghost sm" onClick={() => setLimit(d.deployment_id, "max_active_requests", d.config.max_active_requests)}>Edit</button></td>
                <td>{d.config.max_queue_length ?? "default"}{" "}
                  <button className="ghost sm" onClick={() => setLimit(d.deployment_id, "max_queue_length", d.config.max_queue_length)}>Edit</button></td></tr>
            ))}</tbody>
          </table></div>
        </div>
      )}
    </>
  );
}
