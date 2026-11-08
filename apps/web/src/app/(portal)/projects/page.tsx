"use client";

import { useEffect, useState } from "react";
import { api, ApiError, type Project, type WorkloadConfig } from "@/lib/api";
import { useUser } from "@/lib/session";
import { Badge, EmptyState, Loading, PageHeader } from "@/components/ui";
import { IconFolder } from "@/components/icons";

export default function Projects() {
  const user = useUser();
  const admin = user.role === "super_admin";
  const [rows, setRows] = useState<Project[] | null>(null);
  const [classes, setClasses] = useState<string[]>([]);
  const [error, setError] = useState("");
  const load = () => api<Project[]>("/projects").then(setRows).catch(() => setRows([]));
  useEffect(() => {
    load();
    if (admin) api<WorkloadConfig>("/admin/workload-classes").then((c) => setClasses(Object.keys(c.classes))).catch(() => {});
  }, [admin]); // eslint-disable-line react-hooks/exhaustive-deps
  async function setClass(id: string, v: string) {
    setError("");
    try { await api(`/admin/projects/${id}/workload-class`, { method: "PATCH", json: { workload_class: v || null } }); load(); }
    catch (e) { setError(e instanceof ApiError ? e.message : "Failed"); }
  }
  return (
    <>
      <PageHeader title="Projects" subtitle="Projects you own or have been given access to." />
      {error && <p className="err">{error}</p>}
      <div className="card flush">
        {rows === null ? <Loading /> : rows.length === 0 ? (
          <EmptyState icon={<IconFolder />} title="No projects yet" hint="Ask a team admin to add you to a project." />
        ) : (
          <div className="table-wrap"><table>
            <thead><tr><th>Name</th><th>Description</th>{admin && <th>Workload class</th>}<th>Status</th></tr></thead>
            <tbody>{rows.map((p) => (
              <tr key={p.id}><td><b>{p.name}</b></td><td className="muted">{p.description ?? "—"}</td>
                {admin && <td><select value={p.workload_class ?? ""} onChange={(e) => setClass(p.id, e.target.value)}><option value="">standard (default)</option>{classes.filter((c) => c !== "standard").map((c) => <option key={c} value={c}>{c}</option>)}</select></td>}
                <td><Badge value={p.status} /></td></tr>
            ))}</tbody>
          </table></div>
        )}
      </div>
    </>
  );
}
