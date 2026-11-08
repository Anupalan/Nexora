"use client";

import { useEffect, useState, type FormEvent } from "react";
import { api, ApiError, type Team } from "@/lib/api";
import { useUser } from "@/lib/session";
import { Badge, EmptyState, Err, Loading, PageHeader } from "@/components/ui";
import { IconTeam } from "@/components/icons";

export default function Teams() {
  const user = useUser();
  const [rows, setRows] = useState<Team[] | null>(null);
  const [error, setError] = useState("");
  const load = () => api<Team[]>("/teams").then(setRows).catch(() => setRows([]));
  useEffect(() => { load(); }, []);

  async function create(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    setError("");
    try {
      await api("/teams", { method: "POST", json: { name: new FormData(form).get("name") } });
      form.reset(); load();
    } catch (err) { setError(err instanceof ApiError ? err.message : "Failed"); }
  }
  return (
    <>
      <PageHeader title="Teams" subtitle="Teams own projects and can be given their own quotas." />
      {user.role === "super_admin" && (
        <form className="card form-card" onSubmit={create}>
          <div className="row"><label>New team<input name="name" required placeholder="Platform" /></label><button>Create team</button></div>
        </form>
      )}
      <Err>{error}</Err>
      <div className="card flush">
        {rows === null ? <Loading /> : rows.length === 0 ? (
          <EmptyState icon={<IconTeam />} title="No teams yet" hint="You are not in any team." />
        ) : (
          <div className="table-wrap"><table>
            <thead><tr><th>Name</th><th>Status</th><th>Created</th></tr></thead>
            <tbody>{rows.map((t) => (
              <tr key={t.id}><td><b>{t.name}</b></td><td><Badge value={t.status} /></td>
                <td className="muted">{new Date(t.created_at).toLocaleDateString()}</td></tr>
            ))}</tbody>
          </table></div>
        )}
      </div>
    </>
  );
}
