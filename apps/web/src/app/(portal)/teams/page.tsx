"use client";

import { useEffect, useState, type FormEvent } from "react";
import { api, ApiError, type Team } from "@/lib/api";
import { useUser } from "@/lib/session";

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
      <h1>Teams</h1>
      {user.role === "super_admin" && (
        <form className="row" onSubmit={create}>
          <label>New team<input name="name" required /></label>
          <button>Create</button>
          {error && <span className="err">{error}</span>}
        </form>
      )}
      <div className="card">
        {rows === null ? <p className="muted">Loading…</p> : rows.length === 0 ? (
          <p className="muted">You are not in any team yet.</p>
        ) : (
          <table>
            <thead><tr><th>Name</th><th>Status</th><th>Created</th></tr></thead>
            <tbody>{rows.map((t) => (
              <tr key={t.id}><td>{t.name}</td><td><span className="badge">{t.status}</span></td>
                <td className="muted">{new Date(t.created_at).toLocaleDateString()}</td></tr>
            ))}</tbody>
          </table>
        )}
      </div>
    </>
  );
}
