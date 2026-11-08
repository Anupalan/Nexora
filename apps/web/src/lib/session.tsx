"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError, type User } from "./api";

const Ctx = createContext<User | null>(null);
export const useUser = () => {
  const u = useContext(Ctx);
  if (!u) throw new Error("useUser outside SessionGate");
  return u;
};

/** Loads /auth/me; redirects to /login on 401. Authorization itself is enforced by the API. */
export function SessionGate({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const router = useRouter();
  useEffect(() => {
    api<User>("/auth/me")
      .then(setUser)
      .catch((e) => { if (e instanceof ApiError && e.status === 401) router.replace("/login"); });
  }, [router]);
  if (!user) return <p className="muted center">Loading…</p>;
  return <Ctx.Provider value={user}>{children}</Ctx.Provider>;
}
