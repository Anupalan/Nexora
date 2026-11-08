"use client";

import { useEffect, useState, type FormEvent } from "react";
import { api, ApiError, type User } from "@/lib/api";
import { useUser } from "@/lib/session";
import { Badge, EmptyState, Err, Loading, PageHeader } from "@/components/ui";
import { IconUsers } from "@/components/icons";

export default function Users() {
  const me = useUser();
  const [rows, setRows] = useState<User[] | null>(null);
  const [error, setError] = useState("");
  const load = () => api<User[]>("/users").then(setRows).catch((e) => {
    setError(e instanceof ApiError ? e.message : "Failed"); setRows([]);
  });
  useEffect(() => { load(); }, []);

  async function create(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget, f = new FormData(form);
    setError("");
    try {
      await api("/users", { method: "POST", json: {
        name: f.get("name"), email: f.get("email"), password: f.get("password"), role: f.get("role"),
      } });
      form.reset(); load();
    } catch (err) { setError(err instanceof ApiError ? err.message : "Failed"); }
  }
  async function toggle(u: User) {
    setError("");
    await api(`/users/${u.id}/${u.status === "active" ? "suspend" : "activate"}`, { method: "POST" })
      .then(load).catch((e) => setError(e instanceof ApiError ? e.message : "Failed"));
  }
  const admin = me.role === "super_admin";
  return (
    <>
      <PageHeader title="Developers" subtitle="People with access to Nexora. Suspending someone ends their sessions and revokes their API keys." />
      {admin && (
        <form className="card form-card" onSubmit={create}>
          <div className="row">
            <label>Name<input name="name" required /></label>
            <label>Email<input name="email" type="email" required /></label>
            <label>Initial password (12+ characters)<input name="password" type="password" minLength={12} required /></label>
            <label>Role<select name="role" defaultValue="developer">
              <option value="developer">Developer</option><option value="team_admin">Team admin</option>
              <option value="super_admin">Super admin</option></select></label>
            <button>Create user</button>
          </div>
        </form>
      )}
      <Err>{error}</Err>
      <div className="card flush">
        {rows === null ? <Loading /> : rows.length === 0 ? (
          <EmptyState icon={<IconUsers />} title="No developers to show" />
        ) : (
          <div className="table-wrap"><table>
            <thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Status</th><th /></tr></thead>
            <tbody>{rows.map((u) => (
              <tr key={u.id}><td><b>{u.name}</b></td><td className="muted">{u.email}</td>
                <td><Badge value={u.role} tone="" /></td><td><Badge value={u.status} /></td>
                <td className="actions">{admin && u.id !== me.id && (
                  <button className={u.status === "active" ? "danger sm" : "ghost sm"} onClick={() => toggle(u)}>
                    {u.status === "active" ? "Suspend" : "Activate"}</button>)}</td></tr>
            ))}</tbody>
          </table></div>
        )}
      </div>
    </>
  );
}
