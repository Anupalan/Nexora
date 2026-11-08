"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type AlertRow } from "@/lib/api";
import { useUser } from "@/lib/session";
import { Badge, EmptyState, Err, Loading, PageHeader } from "@/components/ui";
import { IconBell } from "@/components/icons";

export default function Alerts() {
  const user = useUser();
  const [tab, setTab] = useState<"open" | "resolved">("open");
  const [rows, setRows] = useState<AlertRow[] | null>(null);
  const [error, setError] = useState("");
  const load = useCallback(() => api<AlertRow[]>(`/admin/alerts?status=${tab}&limit=100`)
    .then(setRows).catch(() => setRows([])), [tab]);
  useEffect(() => { setRows(null); load(); const t = setInterval(load, 10000); return () => clearInterval(t); }, [load]);

  async function resolve(id: string) {
    setError("");
    try { await api(`/admin/alerts/${id}/resolve`, { method: "POST" }); load(); }
    catch (e) { setError(e instanceof ApiError ? e.message : "Failed"); }
  }
  if (user.role !== "super_admin") return <EmptyState icon={<IconBell />} title="Administrators only" />;
  return (
    <>
      <PageHeader title="Alerts" subtitle="Raised automatically and resolved when the condition clears. Dismissing one by hand is audited; it re-opens if the problem is still there."
        actions={<div className="row">
          <button className={tab === "open" ? "" : "ghost"} onClick={() => setTab("open")}>Open</button>
          <button className={tab === "resolved" ? "" : "ghost"} onClick={() => setTab("resolved")}>Resolved</button>
        </div>} />
      <Err>{error}</Err>
      <div className="card flush">
        {rows === null ? <Loading /> : rows.length === 0 ? (
          <EmptyState icon={<IconBell />} title={tab === "open" ? "All clear" : "Nothing resolved yet"}
            hint={tab === "open" ? "No open alerts." : undefined} />
        ) : rows.map((a) => (
          <div className="alert-row" key={a.id}>
            <Badge value={a.severity} />
            <div className="grow">
              <div>{a.message}</div>
              <div className="meta"><code>{a.type}</code> · {new Date(a.created_at).toLocaleString()}
                {a.resolved_at && ` · resolved ${new Date(a.resolved_at).toLocaleString()} (${a.auto_resolved ? "automatically" : "manually"})`}</div>
            </div>
            {a.status === "open" && <button className="ghost sm" onClick={() => resolve(a.id)}>Dismiss</button>}
          </div>
        ))}
      </div>
    </>
  );
}
