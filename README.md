# Nexora

**One Platform. Every Intelligence.** Unified access to AI models, coding agents and intelligent
workflows — Model Gateway · AI Workspace · Agent Orchestration · Usage Intelligence.

Full spec and phased plan: [roadmap.md](roadmap.md). Current status: **Phase 4 (admin analytics and operations)** on top of Phases 1–3. See `docs/operations.md`.

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

## Phase 4 status (admin analytics and operations)
Done: admin overview (active developers, requests, tokens, queue, failures, time to first token,
generation speed); per-deployment **health checks through LiteLLM** every 30s; **in-app alerts** that
raise and auto-resolve (inference unavailable, queue depth/wait, quota approaching/exceeded, repeated
sign-in failures) with a count badge in the nav; audit-log search (text, entity, date range);
**Prometheus `/metrics`** (token-protected, 404 until `METRICS_TOKEN` is set); Prometheus config, alert
rules and a Grafana dashboard under `infra/monitoring`; **backup and restore-test scripts** under
`infra/backup` (tested, including tampered/truncated backups and failure metrics); runbooks in
`docs/operations.md`.

Not verified / not done: GPU and unified-memory panels need the DGX Spark's DCGM exporter (not available
here); the Prometheus rules and Grafana dashboard are syntax-checked but were not loaded into running
Prometheus/Grafana (no Docker here); no email/webhook notification channels; no WAL/PITR; no
off-host upload in the backup script; workspace-worker health arrives with the workspace (Phase 5).

## Local end-to-end demo (roadmap §51)
`infra/mock-llm` is a mock LiteLLM + vLLM (virtual keys with model allow-lists, streaming, usage) for
local work: `cd infra/mock-llm && MOCK_MASTER_KEY=dev uvicorn server:app --port 4000` (run the API with
`LITELLM_MASTER_KEY=dev`). With web, API, Postgres, Redis and the mock running,
`apps/api/.venv/bin/python tests/e2e/demo.py` walks the 18-step workflow: admin creates team, developer,
project and quotas; the developer gets a key and uses it through `/v1` (chat and streaming); usage shows
up for the developer and the admin; the next request over quota is rejected with a clear 429. The mock
is not a faithful LiteLLM: the same flow must still be validated against the real proxy and vLLM.
Register models with LiteLLM's `model_name` (not the `openai/...` provider path) as the model's
"LiteLLM model": Nexora creates keys in LiteLLM with those names.

## Running against the real LiteLLM proxy
Validated against LiteLLM 1.108.3 (virtual-key create/update/delete by hashed id, per-key model
allow-lists, streaming with usage). Setup in a separate venv (outside this repo):
```bash
python3 -m venv ~/litellm-venv && ~/litellm-venv/bin/pip install 'litellm[proxy,extra_proxy]'
createdb litellm_dev
SCHEMA=$(~/litellm-venv/bin/python -c "import litellm.proxy,os;print(os.path.join(os.path.dirname(litellm.proxy.__file__),'schema.prisma'))")
PATH=~/litellm-venv/bin:$PATH DATABASE_URL=postgresql://$USER@localhost/litellm_dev ~/litellm-venv/bin/prisma generate --schema "$SCHEMA"
# backend: the mock acting as vLLM on :4000, then LiteLLM on :4100 (first start applies ~220 DB migrations)
(cd infra/mock-llm && MOCK_OPEN=1 uvicorn server:app --port 4000) &
LITELLM_MASTER_KEY=sk-dev-master LITELLM_DATABASE_URL=postgresql://$USER@localhost/litellm_dev \
  ~/litellm-venv/bin/litellm --config infra/litellm/config.local.yaml --port 4100
```
Run the API with `LITELLM_BASE_URL=http://localhost:4100 LITELLM_MASTER_KEY=sk-dev-master`. Things the
real proxy taught us, now covered by tests: LiteLLM requires **unique key aliases** (Nexora suffixes
them), and checks a key's model list against the **model_name** in each request (Nexora creates keys
with the model's LiteLLM name, not its alias).

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
