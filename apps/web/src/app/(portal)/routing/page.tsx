"use client";

import { Fragment, useCallback, useEffect, useState, type FormEvent } from "react";
import { api, ApiError, type DecisionRow, type DeploymentRow, type NodeRow, type PolicyRow, type WorkloadConfig } from "@/lib/api";
import { useUser } from "@/lib/session";
import { Badge, EmptyState, Err, Loading, PageHeader } from "@/components/ui";
import { IconRoute } from "@/components/icons";

const STRATEGIES = [
  ["least_loaded", "Least loaded (recommended)"],
  ["weighted", "Weighted split"],
  ["fixed", "Fixed (first by weight)"],
] as const;

export default function Routing() {
  const user = useUser();
  const [nodes, setNodes] = useState<NodeRow[] | null>(null);
  const [deps, setDeps] = useState<DeploymentRow[] | null>(null);
  const [policies, setPolicies] = useState<PolicyRow[]>([]);
  const [frozen, setFrozen] = useState(false);
  const [decisions, setDecisions] = useState<DecisionRow[]>([]);
  const [wl, setWl] = useState<WorkloadConfig | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api<NodeRow[]>("/admin/nodes").then(setNodes).catch(() => setNodes([]));
    api<DeploymentRow[]>("/admin/deployments").then(setDeps).catch(() => setDeps([]));
    api<{ frozen: boolean; policies: PolicyRow[] }>("/admin/routing/status")
      .then((s) => { setFrozen(s.frozen); setPolicies(s.policies); }).catch(() => {});
    api<DecisionRow[]>("/admin/routing/decisions?limit=25").then(setDecisions).catch(() => {});
    api<WorkloadConfig>("/admin/workload-classes").then((c) => setWl((cur) => cur ?? c)).catch(() => {});
  }, []);
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, [load]);

  const run = async (fn: () => Promise<unknown>) => {
    setError("");
    try { await fn(); load(); } catch (e) { setError(e instanceof ApiError ? e.message : "Request failed"); }
  };

  if (user.role !== "super_admin") return <EmptyState icon={<IconRoute />} title="Administrators only" />;
  const models = [...new Map((deps ?? []).map((d) => [d.model_id, d.model])).entries()];
  const nameOf = (id: string | null) => deps?.find((d) => d.id === id)?.name ?? (id ? id.slice(0, 8) : "—");

  return (
    <>
      <PageHeader title="Routing"
        subtitle="Each request goes to the deployment that will serve it soonest: healthy, not saturated, with the model already loaded. Every decision is recorded and explainable."
        actions={frozen
          ? <button onClick={() => run(() => api("/admin/routing/unfreeze", { method: "POST" }))}>Resume smart routing</button>
          : <button className="danger" onClick={() => confirm("Freeze routing? Every model will use the fixed strategy until you resume.") &&
              run(() => api("/admin/routing/freeze", { method: "POST" }))}>Freeze routing</button>} />
      {frozen && <p className="err" role="alert">Routing is frozen: all models use the fixed strategy and ignore load and residency.</p>}
      <Err>{error}</Err>

      <h2 className="section">Nodes</h2>
      <div className="card flush">
        {nodes === null ? <Loading /> : nodes.length === 0 ? <EmptyState icon={<IconRoute />} title="No nodes registered" hint="Register an inference node (POST /admin/nodes) to enable residency-aware routing." /> : (
          <div className="table-wrap"><table>
            <thead><tr><th>Node</th><th>Status</th><th>Ollama</th><th>Loaded models</th><th>Concurrency cap</th><th>Memory budget</th><th /></tr></thead>
            <tbody>{nodes.map((n) => (
              <tr key={n.id}>
                <td><b>{n.name}</b><div className="muted">{n.runtime_url ?? "no runtime URL"}</div></td>
                <td>{!n.enabled ? <Badge value="disabled" /> : n.reachable === null ? <Badge value="unknown" /> : n.reachable ? <Badge value="healthy" /> : <Badge value="unhealthy" />}</td>
                <td>{n.version ?? "—"}</td>
                <td className="muted">{n.resident.length ? n.resident.map((r) => `${r.model}${r.context_length ? ` (${Math.round(r.context_length / 1024)}k ctx)` : ""}`).join(", ") : "nothing loaded"}</td>
                <td>{n.max_concurrent ?? "none"}{" "}
                  <button className="ghost sm" onClick={() => { const v = prompt("Max concurrent requests across all models on this node (empty = no cap)", String(n.max_concurrent ?? "")); if (v !== null) run(() => api(`/admin/nodes/${n.id}`, { method: "PATCH", json: { max_concurrent: v === "" ? null : Number(v) } })); }}>Edit</button></td>
                <td>{n.memory_budget_gb ? `${n.memory_budget_gb} GB` : "not set"}{" "}
                  <button className="ghost sm" onClick={() => { const v = prompt("Memory (GB) Nexora may keep models loaded in on this node. Required before it will preload a model that is not already loaded (empty = none)", String(n.memory_budget_gb ?? "")); if (v !== null) run(() => api(`/admin/nodes/${n.id}`, { method: "PATCH", json: { memory_budget_gb: v === "" ? null : Number(v) } })); }}>Edit</button></td>
                <td className="actions"><span className="actions-inline">
                  <button className="ghost sm" onClick={() => run(() => api(`/admin/nodes/${n.id}/sample`, { method: "POST" }))}>Sample now</button>
                  <button className="ghost sm" onClick={() => run(() => api(`/admin/nodes/${n.id}`, { method: "PATCH", json: { enabled: !n.enabled } }))}>{n.enabled ? "Disable" : "Enable"}</button></span></td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
      </div>

      <h2 className="section">Deployments</h2>
      <div className="card flush">
        {deps === null ? <Loading /> : deps.length === 0 ? <EmptyState icon={<IconRoute />} title="No deployments" /> : (
          <div className="table-wrap"><table>
            <thead><tr><th>Deployment</th><th>Health</th><th>Model loaded</th><th>Keep warm</th><th>Queued</th><th>Running</th><th>Lanes (running/reserved)</th><th>Weight</th><th>Build</th><th /></tr></thead>
            <tbody>{deps.map((d) => (
              <tr key={d.id}>
                <td><b>{d.name}</b><div className="muted">{d.model}{d.node ? ` · ${d.node}` : ""}</div></td>
                <td>{!d.enabled ? <Badge value="disabled" /> : d.draining ? <Badge value="draining" tone="warn" /> : <Badge value={d.health} />}</td>
                <td>{d.resident === null ? <span className="muted">unknown</span> : d.resident ? <Badge value="loaded" tone="ok" /> : <Badge value="cold" tone="warn" />}</td>
                <td>{d.keep_warm ? <Badge value="on" tone="ok" /> : <span className="muted">off</span>}{d.warm?.ok === false && <div className="err">{d.warm.detail}</div>}{" "}
                  <button className="ghost sm" onClick={() => run(() => api(`/admin/deployments/${d.id}`, { method: "PATCH", json: { keep_warm: !d.keep_warm } }))}>{d.keep_warm ? "Stop" : "Start"}</button>
                  {d.keep_warm && <button className="ghost sm" onClick={() => run(() => api(`/admin/deployments/${d.id}/warm`, { method: "POST" }))}>Warm now</button>}</td>
                <td>{d.queued}</td><td>{d.active} / {d.max_active}</td>
                <td className="muted" style={{ whiteSpace: "normal" }}>{Object.entries(d.classes ?? {}).filter(([, v]) => v.reserved || v.active || v.queued).map(([k, v]) => <span key={k} style={{ marginRight: 10 }}>{k} <b>{v.active}/{v.reserved}</b>{v.queued ? ` (+${v.queued} waiting)` : ""}</span>)}</td>
                <td>{d.weight}{" "}<button className="ghost sm" onClick={() => { const v = prompt("Weight (0 stops routing here)", String(d.weight)); if (v !== null && v !== "") run(() => api(`/admin/deployments/${d.id}`, { method: "PATCH", json: { weight: Number(v) } })); }}>Edit</button></td>
                <td className="muted"><code>{d.digest ?? "—"}</code></td>
                <td className="actions"><span className="actions-inline">
                  <button className="ghost sm" onClick={() => run(() => api(`/admin/deployments/${d.id}`, { method: "PATCH", json: { draining: !d.draining } }))}>{d.draining ? "Undrain" : "Drain"}</button>
                  <button className="ghost sm" onClick={() => run(() => api(`/admin/deployments/${d.id}`, { method: "PATCH", json: { enabled: !d.enabled } }))}>{d.enabled ? "Disable" : "Enable"}</button></span></td>
              </tr>
            ))}</tbody>
          </table></div>
        )}
      </div>

      <h2 className="section">Workload classes</h2>
      <div className="card form-card">
        <p className="muted" style={{ marginTop: 0 }}>
          Each class reserves a share of every deployment&apos;s slots. A class under its reservation gets the next free slot;
          otherwise lower priority numbers go first (extraction = 1). A critical class (extraction) is served before every other class, bounded so it cannot starve them. Idle reserved slots can be borrowed unless the
          class was active within the window below. Assign a class to a project or key on those pages.
        </p>
        {wl && (
          <form onSubmit={(e: FormEvent<HTMLFormElement>) => {
            e.preventDefault();
            const f = new FormData(e.currentTarget);
            const classes = Object.fromEntries(Object.keys(wl.classes).map((k) => [k,
              { reserved_pct: Number(f.get(`pct-${k}`)), priority: Number(f.get(`pri-${k}`)), critical: f.get(`crit-${k}`) === "on" }]));
            run(async () => { setWl(await api<WorkloadConfig>("/admin/workload-classes", { method: "PUT", json: {
              classes, borrow_idle_after_s: Number(f.get("idle")), critical_burst: Number(f.get("burst")) } })); });
          }}>
            <div className="table-wrap"><table>
              <thead><tr><th>Class</th><th>Reserved %</th><th>Priority (1 = first)</th><th>Critical</th></tr></thead>
              <tbody>{Object.entries(wl.classes).map(([k, v]) => (
                <tr key={k}><td><b>{k}</b></td>
                  <td><input name={`pct-${k}`} type="number" min={0} max={100} defaultValue={v.reserved_pct} style={{ width: 90 }} /></td>
                  <td><input name={`pri-${k}`} type="number" min={1} max={99} defaultValue={v.priority} style={{ width: 90 }} /></td>
                  <td><input name={`crit-${k}`} type="checkbox" defaultChecked={!!v.critical} /></td></tr>
              ))}</tbody>
            </table></div>
            <div className="row" style={{ marginTop: 12 }}>
              <label>Hold a class&apos;s reserved slots for (seconds after it was last active; 0 = never hold)
                <input name="idle" type="number" min={0} max={86400} defaultValue={wl.borrow_idle_after_s} style={{ width: 120 }} /></label>
              <label>Critical classes may go first at most this many times in a row while others wait
                <input name="burst" type="number" min={1} max={1000} defaultValue={wl.critical_burst ?? 4} style={{ width: 90 }} /></label>
              <button>Save classes</button>
            </div>
          </form>
        )}
      </div>

      <h2 className="section">Policy</h2>
      <div className="card form-card">
        {models.length === 0 ? <span className="muted">No models yet.</span> : models.map(([mid, alias]) => {
          const pol = policies.find((p) => p.model_id === mid);
          const mine = (deps ?? []).filter((d) => d.model_id === mid && d.digest);
          return (
            <form key={mid} className="row" style={{ marginBottom: 12 }} onSubmit={(e: FormEvent<HTMLFormElement>) => {
              e.preventDefault();
              const f = new FormData(e.currentTarget);
              run(() => api(`/admin/routing/policies/${mid}`, { method: "PUT", json: {
                strategy: f.get("strategy"), residency_aware: f.get("residency") === "on",
                exclude_mismatched: f.get("exclude") === "on",
                reference_deployment_id: f.get("ref") || null } }));
            }}>
              <label>Model<input value={alias} readOnly style={{ width: 150 }} /></label>
              <label>Strategy<select name="strategy" defaultValue={pol?.strategy ?? "least_loaded"}>
                {STRATEGIES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select></label>
              <label>Reference model build<select name="ref" defaultValue="">
                <option value="">{pol?.configuration.reference_digest ? `keep ${pol.configuration.reference_digest.slice(0, 8)}` : "none (all builds allowed)"}</option>
                {mine.map((d) => <option key={d.id} value={d.id}>{d.name} · {d.digest?.slice(0, 8)}</option>)}</select></label>
              <label style={{ display: "flex", alignItems: "center", gap: 6, flexDirection: "row" }}>
                <input type="checkbox" name="residency" defaultChecked={pol?.configuration.residency_aware ?? true} /> Prefer loaded models</label>
              <label style={{ display: "flex", alignItems: "center", gap: 6, flexDirection: "row" }}>
                <input type="checkbox" name="exclude" defaultChecked={pol?.configuration.exclude_mismatched ?? true} /> Skip other builds</label>
              <button>Save v{(pol?.version ?? 0) + 1}</button>
            </form>
          );
        })}
      </div>

      <h2 className="section">Recent decisions</h2>
      <div className="card flush">
        {decisions.length === 0 ? <EmptyState icon={<IconRoute />} title="No routed requests yet" /> : (
          <div className="table-wrap"><table>
            <thead><tr><th>When</th><th>Request</th><th>Went to</th><th>Why</th></tr></thead>
            <tbody>{decisions.map((d) => {
              const key = `${d.request_id}:${d.attempt}`;
              return (
                <Fragment key={key}>
                  <tr style={{ cursor: "pointer" }} onClick={() => setOpen(open === key ? null : key)}>
                    <td className="muted">{new Date(d.created_at).toLocaleTimeString()}</td>
                    <td><code>{d.request_id.slice(0, 8)}</code>{d.attempt > 1 && <> <Badge value="failover" tone="warn" /></>}</td>
                    <td><b>{nameOf(d.chosen_deployment_id)}</b></td>
                    <td className="muted" style={{ whiteSpace: "normal", maxWidth: 520 }}>{d.reason}</td>
                  </tr>
                  {open === key && (
                    <tr><td colSpan={4} style={{ background: "var(--panel-2)" }}>
                      <table><thead><tr><th>Candidate</th><th>Eligible</th><th>Queued</th><th>Running</th><th>Loaded</th><th>Queue wait</th><th>Cold load</th><th>Score</th></tr></thead>
                        <tbody>{d.candidates.map((c) => (
                          <tr key={c.deployment}><td>{c.deployment}</td>
                            <td>{c.eligible ? <Badge value="yes" tone="ok" /> : <span className="muted">{c.excluded_because}</span>}</td>
                            <td>{c.queued}</td><td>{c.active}/{c.max_active}</td>
                            <td>{c.resident === null ? "?" : c.resident ? "yes" : "no"}</td>
                            <td>{c.expected_wait_s}s</td><td>{c.cold_load_s}s</td><td>{c.score}</td></tr>
                        ))}</tbody></table></td></tr>
                  )}
                </Fragment>
              );
            })}</tbody>
          </table></div>
        )}
      </div>
    </>
  );
}
