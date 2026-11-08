"use client";

import { useEffect, useState } from "react";
import { Badge, EmptyState, PageHeader, Stat } from "@/components/ui";
import { IconChart, IconInbox, IconKey, IconQueue } from "@/components/icons";
import { api, type HistoryRow, type UsageReport } from "@/lib/api";

const n = (v: number | null | undefined) => (v == null ? "—" : v.toLocaleString());

export default function Usage() {
  const [days, setDays] = useState(30);
  const [report, setReport] = useState<UsageReport | null>(null);
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const range = () => {
    const to = new Date(), from = new Date(Date.now() - days * 86400_000);
    return `from=${encodeURIComponent(from.toISOString())}&to=${encodeURIComponent(to.toISOString())}`;
  };
  useEffect(() => {
    api<UsageReport>(`/usage/me?${range()}`).then(setReport).catch(() => setReport(null));
    api<HistoryRow[]>(`/usage/requests?${range()}&limit=50&mine=true`).then(setHistory).catch(() => setHistory([]));
  }, [days]); // eslint-disable-line react-hooks/exhaustive-deps

  const max = Math.max(1, ...(report?.by_day.map((d) => d.total_tokens) ?? [1]));
  return (
    <>
      <PageHeader title="Usage" subtitle="Your requests and token consumption. Prompt and completion text is never stored."
        actions={<><label>Range<select value={days} onChange={(e) => setDays(Number(e.target.value))}>
          <option value={1}>Last 24 hours</option><option value={7}>Last 7 days</option>
          <option value={30}>Last 30 days</option><option value={90}>Last 90 days</option></select></label>
        <a href={`/api/v1/usage/export?${range()}&mine=true`}><button className="ghost" type="button">Export CSV</button></a></>} />
      {report && (
        <>
          <div className="grid">
            <Stat icon={<IconQueue />} label="Requests" value={n(report.totals.requests)} />
            <Stat icon={<IconKey />} label="Input tokens" value={n(report.totals.input_tokens)} />
            <Stat icon={<IconChart />} label="Output tokens" value={n(report.totals.output_tokens)} />
            <Stat icon={<IconInbox />} label="Avg queue wait" value={report.totals.avg_queue_ms == null ? "—" : `${n(report.totals.avg_queue_ms)} ms`} />
          </div>
          <div className="card" style={{ marginBottom: 14 }}>
            <b>Tokens per day</b>
            <div style={{ display: "flex", alignItems: "end", gap: 3, height: 90, marginTop: 8 }}>
              {report.by_day.length === 0 && <span className="muted" style={{ alignSelf: "center" }}>No usage in this range.</span>}
              {report.by_day.map((d) => (
                <div key={d.date} title={`${d.date}: ${d.total_tokens.toLocaleString()} tokens, ${d.requests} requests`}
                  style={{ flex: 1, minWidth: 4, maxWidth: 56, background: "var(--grad)", borderRadius: 3,
                    height: `${Math.max(3, (d.total_tokens / max) * 100)}%` }} />
              ))}
            </div>
          </div>
        </>
      )}
      <h2 className="section">Request history</h2>
      <div className="card flush">
        {history.length === 0 ? <EmptyState icon={<IconInbox />} title="No requests in this range" /> : (
          <div className="table-wrap"><table>
            <thead><tr><th>Time</th><th>Model</th><th>Source</th><th>Status</th><th>In</th><th>Out</th>
              <th>Queue ms</th><th>Duration ms</th><th>Error</th></tr></thead>
            <tbody>{history.map((r) => (
              <tr key={r.id}><td className="muted">{new Date(r.created_at).toLocaleString()}</td>
                <td>{r.model}</td><td>{r.source}</td><td><Badge value={r.status} /></td>
                <td>{n(r.input_tokens)}</td><td>{n(r.output_tokens)}</td><td>{n(r.queue_time_ms)}</td>
                <td>{n(r.duration_ms)}</td><td className="muted">{r.error_code ?? ""}</td></tr>
            ))}</tbody>
          </table></div>
        )}
      </div>
    </>
  );
}
