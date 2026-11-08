"use client";

import { useEffect, useState, type FormEvent } from "react";
import { Badge, EmptyState, Err, Loading, PageHeader } from "@/components/ui";
import { IconKey } from "@/components/icons";
import { api, ApiError, type ApiKeyCreated, type ApiKeyInfo, type ModelInfo, type Project } from "@/lib/api";

/** OpenCode custom-provider config for an OpenAI-compatible endpoint. */
function openCodeConfig(key: string, models: string[]) {
  const origin = typeof window === "undefined" ? "" : window.location.origin;
  return JSON.stringify({
    $schema: "https://opencode.ai/config.json",
    provider: {
      nexora: {
        npm: "@ai-sdk/openai-compatible",
        name: "Nexora",
        options: { baseURL: `${origin}/v1`, apiKey: key },
        models: Object.fromEntries(models.map((m) => [m, { name: m }])),
      },
    },
  }, null, 2);
}

export default function Keys() {
  const [keys, setKeys] = useState<ApiKeyInfo[] | null>(null);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [secret, setSecret] = useState<ApiKeyCreated | null>(null);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);

  const load = () => api<ApiKeyInfo[]>("/keys").then(setKeys).catch(() => setKeys([]));
  useEffect(() => {
    load();
    api<ModelInfo[]>("/models").then(setModels).catch(() => {});
    api<Project[]>("/projects").then(setProjects).catch(() => {});
  }, []);

  const fail = (e: unknown) => setError(e instanceof ApiError ? e.message : "Request failed");

  async function create(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget, f = new FormData(form);
    setError(""); setCopied(false);
    try {
      const created = await api<ApiKeyCreated>("/keys", { method: "POST", json: {
        name: f.get("name"),
        project_id: f.get("project_id") || null,
        models: f.getAll("models").length ? f.getAll("models") : null,
        expires_in_days: Number(f.get("days")),
      } });
      setSecret(created); form.reset(); load();
    } catch (err) { fail(err); }
  }
  async function act(k: ApiKeyInfo, verb: "rotate" | "revoke") {
    if (verb === "revoke" && !confirm(`Revoke "${k.name}"? Clients using it will stop working.`)) return;
    setError(""); setCopied(false);
    try {
      const r = await api<ApiKeyCreated | ApiKeyInfo>(`/keys/${k.id}/${verb}`, { method: "POST" });
      if (verb === "rotate") setSecret(r as ApiKeyCreated);
      load();
    } catch (err) { fail(err); }
  }
  async function copy(text: string) {
    await navigator.clipboard.writeText(text).then(() => setCopied(true)).catch(() => {});
  }

  return (
    <>
      <PageHeader title="API Keys" subtitle="Personal keys for OpenAI-compatible clients such as OpenCode, Cline, Goose and Aider." />
      {secret && (
        <div className="notice">
          <b>Copy your key now — it will not be shown again.</b>
          <p><code>{secret.key}</code>{" "}
            <button className="ghost" onClick={() => copy(secret.key)}>{copied ? "Copied" : "Copy"}</button></p>
          <details open>
            <summary className="muted">OpenCode configuration (opencode.json)</summary>
            <pre style={{ overflow: "auto" }}>{openCodeConfig(secret.key, secret.allowed_models)}</pre>
          </details>
          <button className="ghost" onClick={() => setSecret(null)}>I have saved it</button>
        </div>
      )}
      <form className="card form-card" onSubmit={create}><div className="row">
        <label>Name<input name="name" required placeholder="laptop" /></label>
        <label>Project<select name="project_id" defaultValue="">
          <option value="">— none —</option>
          {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
        <label>Models (none = all)<select name="models" multiple size={Math.min(3, Math.max(2, models.length))}>
          {models.map((m) => <option key={m.id} value={m.model_alias}>{m.model_alias}</option>)}</select></label>
        <label>Expires (days)<input name="days" type="number" min={1} max={365} defaultValue={90} /></label>
        <button>Create key</button>
      </div></form>
      <Err>{error}</Err>
      <div className="card flush">
        {keys === null ? <Loading /> : keys.length === 0 ? (
          <EmptyState icon={<IconKey />} title="No keys yet" hint="Create one above to connect a client." />
        ) : (
          <div className="table-wrap"><table>
            <thead><tr><th>Name</th><th>Key</th><th>Models</th><th>Expires</th><th>Last used</th><th>Status</th><th /></tr></thead>
            <tbody>{keys.map((k) => (
              <tr key={k.id}>
                <td>{k.name}</td><td><code>{k.key_prefix}</code></td>
                <td className="muted">{k.allowed_models.join(", ")}</td>
                <td className="muted">{k.expires_at ? new Date(k.expires_at).toLocaleDateString() : "never"}</td>
                <td className="muted">{k.last_used_at ? new Date(k.last_used_at).toLocaleString() : "never"}</td>
                <td><Badge value={k.status} /></td>
                <td className="actions">{k.status === "active" && <span className="actions-inline">
                  <button className="ghost sm" onClick={() => act(k, "rotate")}>Rotate</button>
                  <button className="danger sm" onClick={() => act(k, "revoke")}>Revoke</button></span>}</td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
      </div>
    </>
  );
}
