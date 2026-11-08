"use client";

import { useEffect, useState } from "react";
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
    api<HistoryRow[]>(`/usage/requests?${range()}&limit=50`).then(setHistory).catch(() => setHistory([]));
  }, [days]); // eslint-disable-line react-hooks/exhaustive-deps

  const max = Math.max(1, ...(report?.by_day.map((d) => d.total_tokens) ?? [1]));
  return (
    <>
      <h1>Usage</h1>
      <div className="row">
        <label>Range<select value={days} onChange={(e) => setDays(Number(e.target.value))}>
          <option value={1}>Last 24 hours</option><option value={7}>Last 7 days</option>
          <option value={30}>Last 30 days</option><option value={90}>Last 90 days</option></select></label>
        <a href={`/api/v1/usage/export?${range()}`}><button className="ghost" type="button">Export CSV</button></a>
      </div>
      {report && (
        <>
          <div className="grid">
            {([["Requests", report.totals.requests], ["Input tokens", report.totals.input_tokens],
               ["Output tokens", report.totals.output_tokens], ["Failed", report.totals.failed],
               ["Avg queue wait (ms)", report.totals.avg_queue_ms]] as [string, number | null][]).map(([l, v]) => (
              <div className="card" key={l}><div className="muted">{l}</div>
                <b style={{ fontSize: 24 }}>{n(v)}</b></div>
            ))}
          </div>
          <div className="card" style={{ marginBottom: "1rem" }}>
            <div className="muted">Tokens per day</div>
            <div style={{ display: "flex", alignItems: "end", gap: 3, height: 90, marginTop: 8 }}>
              {report.by_day.length === 0 && <span className="muted">No usage in this range.</span>}
              {report.by_day.map((d) => (
                <div key={d.date} title={`${d.date}: ${d.total_tokens.toLocaleString()} tokens, ${d.requests} requests`}
                  style={{ flex: 1, minWidth: 4, background: "var(--accent)", borderRadius: 2,
                    height: `${Math.max(3, (d.total_tokens / max) * 100)}%` }} />
              ))}
            </div>
          </div>
        </>
      )}
      <h1 style={{ fontSize: 16 }}>Request history</h1>
      <div className="card">
        {history.length === 0 ? <p className="muted">No requests in this range.</p> : (
          <table>
            <thead><tr><th>Time</th><th>Model</th><th>Source</th><th>Status</th><th>In</th><th>Out</th>
              <th>Queue ms</th><th>Duration ms</th><th>Error</th></tr></thead>
            <tbody>{history.map((r) => (
              <tr key={r.id}><td className="muted">{new Date(r.created_at).toLocaleString()}</td>
                <td>{r.model}</td><td>{r.source}</td><td><span className="badge">{r.status}</span></td>
                <td>{n(r.input_tokens)}</td><td>{n(r.output_tokens)}</td><td>{n(r.queue_time_ms)}</td>
                <td>{n(r.duration_ms)}</td><td className="muted">{r.error_code ?? ""}</td></tr>
            ))}</tbody>
          </table>
        )}
      </div>
    </>
  );
}
