"use client";

import { useEffect, useState, type FormEvent } from "react";
import { api, ApiError, type User } from "@/lib/api";
import { useUser } from "@/lib/session";

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
    await api(`/users/${u.id}/${u.status === "active" ? "suspend" : "activate"}`, { method: "POST" })
      .then(load).catch((e) => setError(e instanceof ApiError ? e.message : "Failed"));
  }
  const admin = me.role === "super_admin";
  return (
    <>
      <h1>Developers</h1>
      {admin && (
        <form className="row" onSubmit={create}>
          <label>Name<input name="name" required /></label>
          <label>Email<input name="email" type="email" required /></label>
          <label>Initial password (12+)<input name="password" type="password" minLength={12} required /></label>
          <label>Role<select name="role" defaultValue="developer">
            <option value="developer">Developer</option><option value="team_admin">Team admin</option>
            <option value="super_admin">Super admin</option></select></label>
          <button>Create user</button>
        </form>
      )}
      {error && <p className="err">{error}</p>}
      <div className="card">
        {rows === null ? <p className="muted">Loading…</p> : (
          <table>
            <thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Status</th><th /></tr></thead>
            <tbody>{rows.map((u) => (
              <tr key={u.id}><td>{u.name}</td><td>{u.email}</td><td>{u.role}</td>
                <td><span className="badge">{u.status}</span></td>
                <td>{admin && u.id !== me.id && (
                  <button className="ghost" onClick={() => toggle(u)}>
                    {u.status === "active" ? "Suspend" : "Activate"}</button>)}</td></tr>
            ))}</tbody>
          </table>
        )}
      </div>
    </>
  );
}
