"use client";

import { useEffect, useState } from "react";
import { api, type AuditLog } from "@/lib/api";
import { EmptyState, Loading, PageHeader } from "@/components/ui";
import { IconShield } from "@/components/icons";

export default function Audit() {
  const [rows, setRows] = useState<AuditLog[] | null>(null);
  useEffect(() => { api<AuditLog[]>("/admin/audit-logs?limit=100").then(setRows).catch(() => setRows([])); }, []);
  return (
    <>
      <PageHeader title="Audit Logs" subtitle="Append-only record of privileged actions and sign-ins. Secrets are never logged." />
      <div className="card flush">
        {rows === null ? <Loading /> : rows.length === 0 ? (
          <EmptyState icon={<IconShield />} title="No events yet" />
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
