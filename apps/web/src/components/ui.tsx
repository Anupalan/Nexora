import type { ReactNode } from "react";

type Tone = "ok" | "warn" | "info" | "bad" | "";

const TONES: Record<string, Tone> = {
  active: "ok", completed: "ok", enabled: "ok", healthy: "ok", live: "ok",
  running: "info", queued: "warn", cancelling: "warn", pending: "warn", planned: "",
  failed: "bad", revoked: "bad", suspended: "bad", rejected: "bad", disabled: "",
  cancelled: "", archived: "", inactive: "", expired: "bad",
  critical: "bad", warning: "warn", info: "info", unhealthy: "bad", unknown: "",
};

/** Status pill. Colour is derived from the value, so every page renders states consistently. */
export function Badge({ value, tone }: { value: string; tone?: Tone }) {
  const t = tone ?? TONES[value.toLowerCase()] ?? "";
  return <span className={`badge ${t}`}>{value.replace(/_/g, " ")}</span>;
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="page-head">
      <div><h1>{title}</h1>{subtitle && <p>{subtitle}</p>}</div>
      {actions && <div className="row">{actions}</div>}
    </header>
  );
}

export function EmptyState({ icon, title, hint }: { icon: ReactNode; title: string; hint?: string }) {
  return (
    <div className="empty"><div className="ic">{icon}</div><b>{title}</b>{hint && <span>{hint}</span>}</div>
  );
}

export function Loading() {
  return <div aria-busy="true" aria-label="Loading"><div className="skeleton" style={{ width: "60%" }} />
    <div className="skeleton" style={{ width: "85%" }} /><div className="skeleton" style={{ width: "45%" }} /></div>;
}

export function Stat({ icon, label, value }: { icon: ReactNode; label: string; value: ReactNode }) {
  return (
    <div className="card stat"><div className="stat-icon">{icon}</div>
      <div><div className="stat-label">{label}</div><div className="stat-value">{value}</div></div></div>
  );
}

export function Err({ children }: { children: ReactNode }) {
  return children ? <p className="err" role="alert">{children}</p> : null;
}
