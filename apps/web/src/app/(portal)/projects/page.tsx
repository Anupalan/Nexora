"use client";

import { useEffect, useState } from "react";
import { api, type Project } from "@/lib/api";
import { Badge, EmptyState, Loading, PageHeader } from "@/components/ui";
import { IconFolder } from "@/components/icons";

export default function Projects() {
  const [rows, setRows] = useState<Project[] | null>(null);
  useEffect(() => { api<Project[]>("/projects").then(setRows).catch(() => setRows([])); }, []);
  return (
    <>
      <PageHeader title="Projects" subtitle="Projects you own or have been given access to." />
      <div className="card flush">
        {rows === null ? <Loading /> : rows.length === 0 ? (
          <EmptyState icon={<IconFolder />} title="No projects yet" hint="Ask a team admin to add you to a project." />
        ) : (
          <div className="table-wrap"><table>
            <thead><tr><th>Name</th><th>Description</th><th>Status</th></tr></thead>
            <tbody>{rows.map((p) => (
              <tr key={p.id}><td><b>{p.name}</b></td><td className="muted">{p.description ?? "—"}</td>
                <td><Badge value={p.status} /></td></tr>
            ))}</tbody>
          </table></div>
        )}
      </div>
    </>
  );
}
