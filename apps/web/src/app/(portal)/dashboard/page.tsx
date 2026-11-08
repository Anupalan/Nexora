"use client";

import { useEffect, useState } from "react";
import { api, type Project, type Team } from "@/lib/api";
import { useUser } from "@/lib/session";

const PILLARS = [
  ["Model Gateway", "Keys, quotas and FIFO scheduling — Phase 2–3"],
  ["AI Workspace", "Browser coding workspace — Phase 5"],
  ["Agent Orchestration", "Agent tasks and approvals — Phase 6"],
  ["Usage Intelligence", "Metering and analytics — Phase 3–4"],
];

export default function Dashboard() {
  const user = useUser();
  const [teams, setTeams] = useState<Team[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  useEffect(() => {
    api<Team[]>("/teams").then(setTeams).catch(() => {});
    api<Project[]>("/projects").then(setProjects).catch(() => {});
  }, []);
  return (
    <>
      <h1>Welcome, {user.name}</h1>
      <div className="grid">
        <div className="card"><div className="muted">Teams</div><b style={{ fontSize: 28 }}>{teams.length}</b></div>
        <div className="card"><div className="muted">Projects</div><b style={{ fontSize: 28 }}>{projects.length}</b></div>
      </div>
      <div className="grid">
        {PILLARS.map(([t, d]) => (
          <div className="card pillar" key={t}><b>{t}</b><span>{d}</span></div>
        ))}
      </div>
    </>
  );
}
