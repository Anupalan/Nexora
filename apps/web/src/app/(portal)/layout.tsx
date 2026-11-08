"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, type ComponentType, type ReactNode } from "react";
import { api, type AlertRow } from "@/lib/api";
import { SessionGate, useUser } from "@/lib/session";
import {
  IconActivity, IconBell, IconChart, IconCpu, IconDashboard, IconFolder, IconGauge, IconKey, IconLogo, IconLogout,
  IconQueue, IconServer, IconShield, IconTeam, IconUsers,
} from "@/components/icons";

type Item = [href: string, label: string, Icon: ComponentType<{ size?: number }>];

function Shell({ children }: { children: ReactNode }) {
  const user = useUser();
  const path = usePathname();
  const router = useRouter();
  const admin = user.role === "super_admin";
  const [openAlerts, setOpenAlerts] = useState(0);
  useEffect(() => {
    if (!admin) return;
    const load = () => api<AlertRow[]>("/admin/alerts?status=open&limit=200")
      .then((a) => setOpenAlerts(a.length)).catch(() => {});
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [admin]);
  const lead = admin || user.role === "team_admin";
  const groups: [string, Item[]][] = [
    ["Overview", [["/dashboard", "Dashboard", IconDashboard]]],
    ["Build", [
      ["/keys", "API Keys", IconKey],
      ["/models", "Models", IconCpu],
      ["/usage", "Usage", IconChart],
      ["/queue", "Request Queue", IconQueue],
    ]],
    ["Organisation", [
      ["/projects", "Projects", IconFolder],
      ["/teams", "Teams", IconTeam],
      ...(lead ? ([["/users", "Developers", IconUsers]] as Item[]) : []),
    ]],
    ...(admin ? ([["Administration", [
      ["/admin", "Admin Overview", IconActivity],
      ["/inference", "Inference Servers", IconServer],
      ["/alerts", "Alerts", IconBell],
      ["/quotas", "Quota Policies", IconGauge],
      ["/audit", "Audit Logs", IconShield],
    ]]] as [string, Item[]][]) : []),
  ];
  const initials = user.name.split(/\s+/).map((p) => p[0]).slice(0, 2).join("").toUpperCase();
  async function logout() {
    await api("/auth/logout", { method: "POST" }).catch(() => {});
    router.replace("/login");
  }
  return (
    <div className="shell">
      <aside className="side">
        <div className="brand">
          <div className="logo"><IconLogo size={20} /></div>
          <div><b>Nexora</b><small>One Platform. Every Intelligence.</small></div>
        </div>
        <nav aria-label="Main">
          {groups.map(([group, items]) => (
            <div key={group}>
              <div className="nav-group">{group}</div>
              {items.map(([href, label, Icon]) => (
                <Link key={href} href={href} className={path.startsWith(href) ? "active" : ""}
                  aria-current={path.startsWith(href) ? "page" : undefined}>
                  <Icon /><span>{label}</span>
                  {href === "/alerts" && openAlerts > 0 && <em className="count">{openAlerts}</em>}
                </Link>
              ))}
            </div>
          ))}
        </nav>
        <div className="who">
          <div className="who-row">
            <div className="avatar" aria-hidden="true">{initials}</div>
            <div style={{ minWidth: 0 }}>
              <div className="who-name">{user.name}</div>
              <div className="who-mail">{user.email}</div>
            </div>
          </div>
          <span className="badge plain" style={{ justifySelf: "start" }}>{user.role.replace("_", " ")}</span>
          <button className="ghost sm" onClick={logout} style={{ display: "inline-flex", gap: 6, justifyContent: "center", alignItems: "center" }}>
            <IconLogout size={15} /> Sign out
          </button>
        </div>
      </aside>
      <main className="main">{children}</main>
    </div>
  );
}

export default function PortalLayout({ children }: { children: ReactNode }) {
  return <SessionGate><Shell>{children}</Shell></SessionGate>;
}
