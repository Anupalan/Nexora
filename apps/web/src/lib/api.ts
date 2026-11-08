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
  id: string; team_id: string; name: string; description: string | null; status: string; created_at: string;
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

export interface UsageBucket {
  requests: number; failed: number; input_tokens: number; output_tokens: number; total_tokens: number;
}
export interface UsageReport {
  from: string; to: string;
  totals: UsageBucket & { avg_queue_ms: number | null; avg_ttft_ms: number | null };
  by_day: (UsageBucket & { date: string })[];
  by_model: (UsageBucket & { model: string })[];
}
export interface HistoryRow {
  id: string; model: string; source: string; status: string; input_tokens: number | null;
  output_tokens: number | null; queue_time_ms: number | null; duration_ms: number | null;
  error_code: string | null; created_at: string;
}
export interface QueueItem {
  id: string; status: string; created_at: string; error_code: string | null;
}
export interface QuotaRow {
  id: string; scope_type: string; scope_id: string;
  requests_per_minute: number | null; requests_per_day: number | null; requests_per_month: number | null;
  tokens_per_minute: number | null; tokens_per_day: number | null; tokens_per_month: number | null;
  max_concurrent: number | null; max_output_tokens: number | null;
}

export interface Dashboard {
  today: { active_developers: number; requests: number; failed_requests: number; rejected_requests: number;
    input_tokens: number; output_tokens: number; avg_ttft_ms: number | null;
    avg_generation_tokens_per_s: number | null };
  totals: { users: number; open_alerts: number; critical_alerts: number };
  queue: { depth: number; active: number; avg_wait_ms_5m: number | null };
  models: { model: string; deployment: string; node: string; health: string; queued: number; active: number; max_active: number }[];
  gpu: null;
}
export interface AlertRow {
  id: string; type: string; severity: string; message: string; status: string;
  created_at: string; resolved_at: string | null; auto_resolved: boolean;
}
export interface InferenceRow {
  deployment_id: string; model: string; deployment: string; node: string; status: string; queued: number; active: number;
  max_active: number; max_queue: number; last_check: { checked_at: string; latency_ms: number; detail: string } | null;
}

export interface NodeRow {
  id: string; name: string; runtime_url: string | null; enabled: boolean; max_concurrent: number | null;
  deployments: number; sampled_at: string | null; reachable: boolean | null; version: string | null;
  error: string | null; resident: { model: string; size: number | null; context_length: number | null }[];
  installed: Record<string, string>;
}
export interface DeploymentRow {
  id: string; model_id: string; model: string; name: string; node: string | null; node_id: string | null;
  litellm_model: string; runtime_model: string | null; weight: number; enabled: boolean; draining: boolean;
  health: string; resident: boolean | null; digest: string | null; context_limit: number | null;
  max_active: number; max_queue: number; queued: number; active: number; strategy: string; frozen: boolean;
}
export interface PolicyRow {
  id: string; model_id: string | null; version: number; strategy: string; status: string;
  configuration: { residency_aware?: boolean; exclude_mismatched?: boolean; reference_digest?: string | null };
}
export interface DecisionRow {
  request_id: string; attempt: number; strategy: string; policy_version: number | null;
  chosen_deployment_id: string | null; reason: string; failover_from_deployment_id: string | null; created_at: string;
  candidates: { deployment: string; node: string | null; eligible: boolean; excluded_because: string | null;
    queued: number; active: number; max_active: number; resident: boolean | null; expected_wait_s: number;
    cold_load_s: number; score: number }[];
}
