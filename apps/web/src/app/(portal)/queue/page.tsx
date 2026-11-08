"use client";

import { useCallback, useEffect, useState } from "react";
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
      <h1>Request Queue</h1>
      {error && <p className="err">{error}</p>}
      <div className="card" style={{ marginBottom: "1rem" }}>
        <b>My pending requests</b>
        {mine.length === 0 ? <p className="muted">Nothing queued or running.</p> : (
          <table>
            <thead><tr><th>Request</th><th>Status</th><th>Submitted</th><th /></tr></thead>
            <tbody>{mine.map((r) => (
              <tr key={r.id}><td><code>{r.id.slice(0, 8)}</code></td><td><span className="badge">{r.status}</span></td>
                <td className="muted">{new Date(r.created_at).toLocaleTimeString()}</td>
                <td><button className="ghost" onClick={() => cancel(r.id)}>Cancel</button></td></tr>
            ))}</tbody>
          </table>
        )}
      </div>
      {overview && (
        <div className="card">
          <b>Deployments</b>
          <p className="muted">FIFO is guaranteed within each deployment&apos;s queue (not across models).</p>
          <table>
            <thead><tr><th>Model</th><th>Queued</th><th>Active</th><th>Max active</th><th>Max queue</th></tr></thead>
            <tbody>{overview.deployments.map((d) => (
              <tr key={d.deployment_id}><td>{d.model}</td><td>{d.queued}</td><td>{d.active}</td>
                <td>{d.config.max_active_requests ?? "default"}{" "}
                  <button className="ghost" onClick={() => setLimit(d.deployment_id, "max_active_requests", d.config.max_active_requests)}>Edit</button></td>
                <td>{d.config.max_queue_length ?? "default"}{" "}
                  <button className="ghost" onClick={() => setLimit(d.deployment_id, "max_queue_length", d.config.max_queue_length)}>Edit</button></td></tr>
            ))}</tbody>
          </table>
        </div>
      )}
    </>
  );
}
