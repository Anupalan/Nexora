"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, type AlertRow, type Dashboard } from "@/lib/api";
import { useUser } from "@/lib/session";
import { Badge, EmptyState, Loading, PageHeader, Stat } from "@/components/ui";
import { IconActivity, IconBell, IconChart, IconKey, IconQueue, IconUsers } from "@/components/icons";

const n = (v: number | null | undefined, unit = "") => (v == null ? "—" : `${v.toLocaleString()}${unit}`);

export default function AdminOverview() {
  const user = useUser();
  const [d, setD] = useState<Dashboard | null>(null);
  const [alerts, setAlerts] = useState<AlertRow[]>([]);
  const load = useCallback(() => {
    api<Dashboard>("/admin/dashboard").then(setD).catch(() => {});
    api<AlertRow[]>("/admin/alerts?status=open&limit=5").then(setAlerts).catch(() => {});
  }, []);
  useEffect(() => { load(); const t = setInterval(load, 10000); return () => clearInterval(t); }, [load]);

  if (user.role !== "super_admin") return <EmptyState icon={<IconActivity />} title="Administrators only" />;
  if (!d) return <><PageHeader title="Admin Overview" /><div className="card"><Loading /></div></>;
  const t = d.today;
  return (
    <>
      <PageHeader title="Admin Overview" subtitle="Live view of the platform. Counts are for today (UTC); refreshes every 10 seconds." />
      <div className="grid">
        <Stat icon={<IconUsers />} label="Active developers today" value={n(t.active_developers)} />
        <Stat icon={<IconChart />} label="Requests today" value={n(t.requests)} />
        <Stat icon={<IconKey />} label="Tokens processed today" value={n(t.input_tokens + t.output_tokens)} />
        <Stat icon={<IconQueue />} label="Queue depth / active" value={`${n(d.queue.depth)} / ${n(d.queue.active)}`} />
        <Stat icon={<IconActivity />} label="Failed requests today" value={n(t.failed_requests)} />
        <Stat icon={<IconActivity />} label="Avg time to first token" value={n(t.avg_ttft_ms, " ms")} />
        <Stat icon={<IconActivity />} label="Avg generation speed" value={n(t.avg_generation_tokens_per_s, " tok/s")} />
        <Stat icon={<IconBell />} label="Open alerts" value={n(d.totals.open_alerts)} />
      </div>
      <div className="card flush" style={{ marginBottom: 14 }}>
        <div className="card-head">Models <Link href="/inference">Inference servers →</Link></div>
        {d.models.length === 0 ? <EmptyState icon={<IconActivity />} title="No models registered" /> : (
          <div className="table-wrap"><table>
            <thead><tr><th>Model</th><th>Deployment</th><th>Health</th><th>Queued</th><th>Running</th><th>Slots</th></tr></thead>
            <tbody>{d.models.map((m) => (
              <tr key={`${m.model}:${m.deployment}`}><td><b>{m.model}</b></td>
                <td className="muted">{m.deployment}{m.node ? ` · ${m.node}` : ""}</td><td><Badge value={m.health} /></td>
                <td>{m.queued}</td><td>{m.active}</td><td>{m.max_active}</td></tr>
            ))}</tbody>
          </table></div>
        )}
      </div>
      <div className="card flush">
        <div className="card-head">Open alerts <Link href="/alerts">All alerts →</Link></div>
        {alerts.length === 0 ? <EmptyState icon={<IconBell />} title="All clear" hint="No open alerts." /> : alerts.map((a) => (
          <div className="alert-row" key={a.id}><Badge value={a.severity} />
            <div className="grow"><div>{a.message}</div>
              <div className="meta">{new Date(a.created_at).toLocaleString()}</div></div></div>
        ))}
      </div>
      <p className="muted" style={{ marginTop: 14 }}>GPU and unified-memory utilisation come from the DGX Spark&apos;s exporter in Grafana (see <code>infra/monitoring</code>), not from Nexora.</p>
    </>
  );
}
