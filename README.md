# Nexora

**One Platform. Every Intelligence.** Unified access to AI models, coding agents and intelligent
workflows — Model Gateway · AI Workspace · Agent Orchestration · Usage Intelligence.

Full spec and phased plan: [roadmap.md](roadmap.md). Current status: **Phase 2 (LiteLLM gateway and API keys)** on top of Phase 1.

## Layout
- `apps/api` — FastAPI management API (auth, RBAC, users, teams, projects, audit), Alembic migrations
- `apps/web` — Next.js portal shell
- `infra/compose` — local Postgres + Redis
- `infra/litellm` — example LiteLLM proxy config

## Run locally
```bash
# 1. dependencies (or use your own Postgres/Redis)
docker compose -f infra/compose/docker-compose.dev.yml up -d

# 2. API
cd apps/api && python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
export DATABASE_URL=postgresql+psycopg://nexora:nexora@localhost:5432/nexora COOKIE_SECURE=false
.venv/bin/alembic upgrade head
NEXORA_ADMIN_PASSWORD='change-me-please-12' .venv/bin/python -m app.cli create-admin --email you@company.com
.venv/bin/uvicorn app.main:app --port 8001 --reload

# 3. Web (proxies /api/v1 to the API)
cd apps/web && pnpm install && pnpm dev
```
Tests: `cd apps/api && .venv/bin/pytest` (SQLite, no services needed).

## Phase 2 status
Done: model registry (aliases → LiteLLM models), API-key create/rotate/revoke/update backed by
LiteLLM virtual keys (plaintext shown once, only a SHA-256 stored), OpenAI-compatible `/v1/models`,
`/v1/chat/completions` and `/v1/completions` with streaming passthrough, per-key model access,
revoked/expired/suspended-user rejection, and portal pages for keys (with an OpenCode config
snippet) and models. Set `LITELLM_BASE_URL` / `LITELLM_MASTER_KEY` for the API.

Not yet: quotas, FIFO queue, usage metering and `GET /keys/{id}/usage` (Phase 3); per-project model
permissions. LiteLLM request shapes are tested against a fake — validate against the pinned LiteLLM
and a real vLLM endpoint in Phase 0 before relying on them.

## Phase 1 status
Done: users/roles, login/logout/refresh, server-side sessions with revocation, CSRF double-submit,
login lockout, teams, projects, project roles, backend authorization, append-only audit log, portal shell.

Deferred (needs email/OIDC infra, tracked for a later phase): password reset & account activation
emails, MFA for admins, OIDC/SSO, per-IP login rate limiting at the ingress.
