"use client";

import { useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError, type User } from "@/lib/api";
import { IconChart, IconGateway, IconLogo, IconRoute } from "@/components/icons";
import { Err } from "@/components/ui";

export default function Login() {
  const router = useRouter();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const f = new FormData(e.currentTarget);
    setBusy(true); setError("");
    try {
      await api<User>("/auth/login", { method: "POST", json: { email: f.get("email"), password: f.get("password") } });
      router.replace("/dashboard");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not sign in.");
      setBusy(false);
    }
  }

  return (
    <main className="auth">
      <section className="auth-hero">
        <div className="brand" style={{ padding: 0 }}>
          <div className="logo"><IconLogo size={20} /></div><b style={{ fontSize: 20 }}>Nexora</b>
        </div>
        <h1>One Platform.<br /><span>Every Intelligence.</span></h1>
        <p>Controlled access to your AI models, a complete record of how they are used, and routing that keeps them fast — on infrastructure you control.</p>
        <div className="feature-list">
          <div className="feature"><IconGateway size={20} />Model Gateway</div>
          <div className="feature"><IconChart size={20} />Activity Intelligence</div>
          <div className="feature"><IconRoute size={20} />Routing Optimization</div>
        </div>
      </section>
      <section className="auth-form-wrap">
        <form className="card auth-card stack" onSubmit={submit}>
          <div><h2>Welcome back</h2><span className="muted">Sign in to Nexora</span></div>
          <label>Email<input name="email" type="email" autoComplete="username" required autoFocus placeholder="you@company.com" /></label>
          <label>Password<input name="password" type="password" autoComplete="current-password" required /></label>
          <Err>{error}</Err>
          <button disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
        </form>
      </section>
    </main>
  );
}
