"use client";

import { useEffect, useState } from "react";
import { api, type AuditLog } from "@/lib/api";

export default function Audit() {
  const [rows, setRows] = useState<AuditLog[] | null>(null);
  useEffect(() => { api<AuditLog[]>("/admin/audit-logs?limit=100").then(setRows).catch(() => setRows([])); }, []);
  return (
    <>
      <h1>Audit Logs</h1>
      <div className="card">
        {rows === null ? <p className="muted">Loading…</p> : (
          <table>
            <thead><tr><th>Time</th><th>Action</th><th>Entity</th><th>Details</th></tr></thead>
            <tbody>{rows.map((a) => (
              <tr key={a.id}><td className="muted">{new Date(a.created_at).toLocaleString()}</td>
                <td>{a.action}</td><td>{a.entity_type}</td>
                <td className="muted">{JSON.stringify(a.metadata)}</td></tr>
            ))}</tbody>
          </table>
        )}
      </div>
    </>
  );
}
