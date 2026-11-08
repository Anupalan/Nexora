export type Role = "super_admin" | "team_admin" | "developer";

export interface User {
  id: string;
  name: string;
  email: string;
  status: string;
  role: Role;
  last_login_at: string | null;
  created_at: string;
}
export interface Team { id: string; name: string; status: string; created_at: string }
export interface Project {
  id: string; team_id: string; name: string; description: string | null;
  repository_url: string | null; default_branch: string; status: string; created_at: string;
}
export interface AuditLog {
  id: string; actor_id: string | null; action: string; entity_type: string;
  entity_id: string | null; metadata: Record<string, unknown>; created_at: string;
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

function csrfToken(): string {
  const m = document.cookie.match(/(?:^|; )nexora_csrf=([^;]+)/);
  return m?.[1] ? decodeURIComponent(m[1]) : "";
}

export async function api<T>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (init.json !== undefined) headers.set("content-type", "application/json");
  if (method !== "GET") headers.set("x-csrf-token", csrfToken());
  const res = await fetch(`/api/v1${path}`, {
    ...init,
    method,
    headers,
    body: init.json !== undefined ? JSON.stringify(init.json) : init.body,
    credentials: "same-origin",
  });
  if (res.status === 204) return undefined as T;
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const e = body?.error;
    throw new ApiError(res.status, e?.code ?? "ERROR", e?.message ?? res.statusText);
  }
  return body as T;
}

export interface ModelInfo {
  id: string; name: string; model_alias: string; provider: string; description: string | null;
  context_limit: number | null; max_output_tokens: number | null; enabled: boolean;
}
export interface ApiKeyInfo {
  id: string; name: string; key_prefix: string; allowed_models: string[]; project_id: string | null;
  rpm_limit: number | null; tpm_limit: number | null; expires_at: string | null; status: string;
  last_used_at: string | null; created_at: string;
}
export interface ApiKeyCreated extends ApiKeyInfo { key: string }
