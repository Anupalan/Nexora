# Nexora

**One Platform. Every Intelligence.** Unified access to AI models, coding agents and intelligent
workflows — Model Gateway · AI Workspace · Agent Orchestration · Usage Intelligence.

Full spec and phased plan: [roadmap.md](roadmap.md). Current status: **Phase 3 (quotas, FIFO scheduler, usage)** on top of Phases 1–2.

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
Tests: `cd apps/api && .venv/bin/pytest` (SQLite database; needs `redis-server` on PATH, or set
`TEST_REDIS_URL`, because the Lua-based reservation/queue logic is tested against a real Redis).

## Phase 3 status
Done:
- **Quotas** (global / team / user / key / project): requests and tokens per minute, day and month,
  max concurrent (queued + running) and max output tokens. A request must pass *every* applicable
  scope, so the most restrictive wins. Capacity is reserved atomically in one Redis Lua script, then
  reconciled to actual tokens; windows are UTC-aligned. Rejections return `429` with `Retry-After`.
- **FIFO scheduler**, one Redis queue per model deployment with a configurable concurrency cap and
  queue length. Only the head of the queue can be claimed, so order is strict *per deployment* (not
  across models; vLLM still batches internally). Waiting never holds an inference slot.
- **Waiting requests**: the connection is held open; streaming requests that must wait get SSE
  `: queued` comment keepalives. Queue-full returns `429`; a wait over `QUEUE_TIMEOUT_SECONDS` returns
  `504`. Status, position, cancel (queued or running), `/queue/me`, estimate and an admin overview.
- **Recovery**: lease heartbeats; a sweeper fails expired leases and orphaned waiters *without
  replaying* the model request; Redis counters are rebuilt from Postgres after a Redis wipe.
- **Usage**: every request is recorded (user, team, project, key, model, tokens, queue wait, time to
  first token, duration, status); reports by day/model/project/key, request history, CSV export,
  all scoped by role. Prompt and completion content is never stored.
- Portal pages: Usage, Request Queue, Quota Policies.

Limits and caveats: the default concurrency is deliberately low (`DEFAULT_MAX_ACTIVE_REQUESTS=2`);
raise it only after benchmarking on the real DGX Spark. Token reservations use a rough estimate
(characters / 4 + the output cap) until usage is reported. Concurrency quotas count queued + running
requests. A quota added mid-window starts counting from zero. Aggregations run in Python over up to
200k rows per report; add rollups before large scale. Not yet: Prometheus metrics for the scheduler,
admin alerts, and the multi-process (separate scheduler service) split from the roadmap.

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
