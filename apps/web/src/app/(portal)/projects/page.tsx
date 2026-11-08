"use client";

import { useEffect, useState } from "react";
import { api, type Project } from "@/lib/api";

export default function Projects() {
  const [rows, setRows] = useState<Project[] | null>(null);
  useEffect(() => { api<Project[]>("/projects").then(setRows).catch(() => setRows([])); }, []);
  return (
    <>
      <h1>Projects</h1>
      <div className="card">
        {rows === null ? <p className="muted">Loading…</p> : rows.length === 0 ? (
          <p className="muted">No projects you can access yet.</p>
        ) : (
          <table>
            <thead><tr><th>Name</th><th>Repository</th><th>Branch</th><th>Status</th></tr></thead>
            <tbody>{rows.map((p) => (
              <tr key={p.id}><td>{p.name}</td><td className="muted">{p.repository_url ?? "—"}</td>
                <td>{p.default_branch}</td><td><span className="badge">{p.status}</span></td></tr>
            ))}</tbody>
          </table>
        )}
      </div>
    </>
  );
}
