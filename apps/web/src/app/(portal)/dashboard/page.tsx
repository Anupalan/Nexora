"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, type Project, type Team, type UsageReport } from "@/lib/api";
import { useUser } from "@/lib/session";
import { Badge, PageHeader, Stat } from "@/components/ui";
import { IconBot, IconChart, IconCode, IconFolder, IconGateway, IconKey, IconQueue, IconTeam } from "@/components/icons";

const PILLARS = [
  { t: "Model Gateway", d: "OpenAI-compatible API with virtual keys, quotas and a fair FIFO queue.", s: "live", I: IconGateway, href: "/keys" },
  { t: "AI Workspace", d: "Browser coding workspace with isolated environments.", s: "planned", I: IconCode },
  { t: "Agent Orchestration", d: "Coding agents with review, approval and Git workflows.", s: "planned", I: IconBot },
  { t: "Usage Intelligence", d: "Per-request metering by user, team, project and key.", s: "live", I: IconChart, href: "/usage" },
];

export default function Dashboard() {
  const user = useUser();
  const [teams, setTeams] = useState<Team[] | null>(null);
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [usage, setUsage] = useState<UsageReport | null>(null);
  useEffect(() => {
    api<Team[]>("/teams").then(setTeams).catch(() => setTeams([]));
    api<Project[]>("/projects").then(setProjects).catch(() => setProjects([]));
    api<UsageReport>("/usage/me").then(setUsage).catch(() => {});
  }, []);
  const n = (v: number | undefined) => (v ?? 0).toLocaleString();
  return (
    <>
      <PageHeader title={`Welcome back, ${user.name.split(" ")[0]}`}
        subtitle="Here is where things stand across your teams and models."
        actions={<Link href="/keys"><button type="button">Create API key</button></Link>} />
      <div className="grid">
        <Stat icon={<IconTeam />} label="Teams" value={teams ? n(teams.length) : "—"} />
        <Stat icon={<IconFolder />} label="Projects" value={projects ? n(projects.length) : "—"} />
        <Stat icon={<IconQueue />} label="Requests (30 days)" value={usage ? n(usage.totals.requests) : "—"} />
        <Stat icon={<IconKey />} label="Tokens (30 days)" value={usage ? n(usage.totals.total_tokens) : "—"} />
      </div>
      <h2 className="section">Platform</h2>
      <div className="grid">
        {PILLARS.map(({ t, d, s, I, href }) => {
          const body = (
            <div className="card pillar" style={{ height: "100%" }}>
              <div className="stat-icon"><I /></div>
              <b>{t}</b><span className="d">{d}</span><Badge value={s} />
            </div>
          );
          return href ? <Link key={t} href={href} style={{ color: "inherit", textDecoration: "none" }}>{body}</Link>
            : <div key={t}>{body}</div>;
        })}
      </div>
    </>
  );
}
