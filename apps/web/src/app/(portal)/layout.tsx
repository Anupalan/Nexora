"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import type { ReactNode } from "react";
import { api } from "@/lib/api";
import { SessionGate, useUser } from "@/lib/session";

function Shell({ children }: { children: ReactNode }) {
  const user = useUser();
  const path = usePathname();
  const router = useRouter();
  const admin = user.role === "super_admin";
  const lead = admin || user.role === "team_admin";
  const links: [string, string][] = [
    ["/dashboard", "Dashboard"],
    ["/projects", "Projects"],
    ["/teams", "Teams"],
    ["/keys", "API Keys"],
    ["/models", "Models"],
    ["/usage", "Usage"],
    ["/queue", "Request Queue"],
    ...(lead ? ([["/users", "Developers"]] as [string, string][]) : []),
    ...(admin ? ([["/quotas", "Quota Policies"], ["/audit", "Audit Logs"]] as [string, string][]) : []),
  ];
  async function logout() {
    await api("/auth/logout", { method: "POST" }).catch(() => {});
    router.replace("/login");
  }
  return (
    <div className="shell">
      <aside className="side">
        <div className="brand">Nexora<small>One Platform. Every Intelligence.</small></div>
        <nav>
          {links.map(([href, label]) => (
            <Link key={href} href={href} className={path.startsWith(href) ? "active" : ""}>{label}</Link>
          ))}
        </nav>
        <div className="who">
          {user.name}<br />{user.email}<br /><span className="badge">{user.role}</span>
          <p><button className="ghost" onClick={logout}>Sign out</button></p>
        </div>
      </aside>
      <main className="main">{children}</main>
    </div>
  );
}

export default function PortalLayout({ children }: { children: ReactNode }) {
  return <SessionGate><Shell>{children}</Shell></SessionGate>;
}
