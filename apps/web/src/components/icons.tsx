import type { ReactNode } from "react";

const P = (d: ReactNode) =>
  function Icon({ size = 18 }: { size?: number }) {
    return (
      <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
        strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{d}</svg>
    );
  };

export const IconLogo = P(<path d="M6 19V5l12 14V5" strokeWidth="2.2" />);
export const IconDashboard = P(<><rect x="3" y="3" width="7" height="9" rx="1.5" /><rect x="14" y="3" width="7" height="5" rx="1.5" /><rect x="14" y="12" width="7" height="9" rx="1.5" /><rect x="3" y="16" width="7" height="5" rx="1.5" /></>);
export const IconFolder = P(<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />);
export const IconUsers = P(<><circle cx="9" cy="8" r="3.2" /><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6" /><circle cx="17" cy="9" r="2.4" /><path d="M16 14.2c2.9.3 5 2.6 5 5.8" /></>);
export const IconTeam = P(<><circle cx="12" cy="7" r="3" /><circle cx="5" cy="17" r="2.6" /><circle cx="19" cy="17" r="2.6" /><path d="M10 10l-3.6 5M14 10l3.6 5M7.6 17h8.8" /></>);
export const IconKey = P(<><circle cx="8" cy="15" r="4" /><path d="M11 12l9-9M16 7l3 3M14 9l2 2" /></>);
export const IconCpu = P(<><rect x="6" y="6" width="12" height="12" rx="2" /><rect x="9.5" y="9.5" width="5" height="5" rx="1" /><path d="M9 2v3M15 2v3M9 19v3M15 19v3M2 9h3M2 15h3M19 9h3M19 15h3" /></>);
export const IconChart = P(<><path d="M4 20V10M10 20V4M16 20v-7M22 20H2" /></>);
export const IconQueue = P(<><path d="M4 6h16M4 12h16M4 18h10" /><circle cx="19" cy="18" r="2" /></>);
export const IconGauge = P(<><path d="M4 16a8 8 0 1 1 16 0" /><path d="M12 16l4-5" /><circle cx="12" cy="16" r="1.2" /></>);
export const IconShield = P(<><path d="M12 3l8 3v6c0 4.4-3.2 8-8 9-4.8-1-8-4.6-8-9V6z" /><path d="M9 12l2 2 4-4" /></>);
export const IconLogout = P(<><path d="M10 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h4" /><path d="M16 8l4 4-4 4M20 12H9" /></>);
export const IconCode = P(<><path d="M8 7l-5 5 5 5M16 7l5 5-5 5M14 4l-4 16" /></>);
export const IconBot = P(<><rect x="4" y="8" width="16" height="11" rx="3" /><path d="M12 4v4M9 13h.01M15 13h.01M9 16h6" /></>);
export const IconGateway = P(<><circle cx="5" cy="12" r="2" /><circle cx="19" cy="6" r="2" /><circle cx="19" cy="18" r="2" /><path d="M7 12h4l6-5M11 12l6 5" /></>);
export const IconInbox = P(<><path d="M3 13l3-8h12l3 8" /><path d="M3 13v6h18v-6h-5a4 4 0 0 1-8 0z" /></>);
export const IconBell = P(<><path d="M6 9a6 6 0 1 1 12 0c0 6 2.5 7.5 2.5 7.5h-17S6 15 6 9z" /><path d="M10 20a2 2 0 0 0 4 0" /></>);
export const IconServer = P(<><rect x="3" y="4" width="18" height="6" rx="2" /><rect x="3" y="14" width="18" height="6" rx="2" /><path d="M7 7h.01M7 17h.01" /></>);
export const IconActivity = P(<path d="M3 12h4l3-8 4 16 3-8h4" />);
