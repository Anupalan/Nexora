"use client";

import { useEffect, useState, type FormEvent } from "react";
import { Badge, EmptyState, Err, Loading, PageHeader } from "@/components/ui";
import { IconCpu } from "@/components/icons";
import { api, ApiError, type ModelInfo } from "@/lib/api";
import { useUser } from "@/lib/session";

export default function Models() {
  const user = useUser();
  const admin = user.role === "super_admin";
  const [rows, setRows] = useState<ModelInfo[] | null>(null);
  const [error, setError] = useState("");
  const load = () => api<ModelInfo[]>(admin ? "/admin/models" : "/models").then(setRows).catch(() => setRows([]));
  useEffect(() => { load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function create(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget, f = new FormData(form);
    setError("");
    const num = (k: string) => (f.get(k) ? Number(f.get(k)) : null);
    try {
      await api("/admin/models", { method: "POST", json: {
        name: f.get("name"), model_alias: f.get("alias"), litellm_model: f.get("litellm"),
        context_limit: num("ctx"), max_output_tokens: num("out"),
      } });
      form.reset(); load();
    } catch (err) { setError(err instanceof ApiError ? err.message : "Failed"); }
  }
  async function toggle(m: ModelInfo) {
    await api(`/admin/models/${m.id}/${m.enabled ? "disable" : "enable"}`, { method: "POST" }).then(load)
      .catch((e) => setError(e instanceof ApiError ? e.message : "Failed"));
  }
  return (
    <>
      <PageHeader title="Models" subtitle="Aliases that clients send, mapped to models served through LiteLLM." />
      {admin && (
        <form className="card form-card" onSubmit={create}><div className="row">
          <label>Display name<input name="name" required /></label>
          <label>Alias (clients send this)<input name="alias" required pattern="[A-Za-z0-9._:/-]+" /></label>
          <label>LiteLLM model<input name="litellm" required placeholder="qwen3-coder (model_name in LiteLLM)" /></label>
          <label>Context<input name="ctx" type="number" min={1} /></label>
          <label>Max output<input name="out" type="number" min={1} /></label>
          <button>Register</button>
        </div></form>
      )}
      <Err>{error}</Err>
      <div className="card flush">
        {rows === null ? <Loading /> : rows.length === 0 ? (
          <EmptyState icon={<IconCpu />} title="No models registered" hint="Register a model so developers can use it." />
        ) : (
          <div className="table-wrap"><table>
            <thead><tr><th>Alias</th><th>Name</th><th>Provider</th><th>Context</th><th>Max out</th><th>Status</th><th /></tr></thead>
            <tbody>{rows.map((m) => (
              <tr key={m.id}><td><code>{m.model_alias}</code></td><td>{m.name}</td><td>{m.provider}</td>
                <td>{m.context_limit ?? "—"}</td><td>{m.max_output_tokens ?? "—"}</td>
                <td><Badge value={m.enabled ? "enabled" : "disabled"} /></td>
                <td className="actions">{admin && <button className="ghost sm" onClick={() => toggle(m)}>{m.enabled ? "Disable" : "Enable"}</button>}</td></tr>
            ))}</tbody>
          </table></div>
        )}
      </div>
    </>
  );
}
