# Nexora --- Product Roadmap & Technical Specification

**Version:** 2.0 (scope revision of 8 November 2026)\
**Date:** 8 November 2026\
**Status:** Phases 0--4 implemented; Phases 5--7 planned\
**Product type:** Self-hosted, multi-user AI model gateway with activity logging and routing
optimization\
**Tagline:** One Platform. Every Intelligence.\
**Primary inference hardware:** two NVIDIA DGX Spark systems, 128 GB unified memory each\
**Inference runtime:** Ollama on each node (the runtime is an adapter; others can be added)\
**Primary stack:** Next.js, React, TypeScript, FastAPI, PostgreSQL, Redis, LiteLLM, Ollama\
**Deployment model:** Public HTTPS application with private application and inference networks

---

## 1. Executive Summary

Nexora is a centralized internal AI platform that gives an organization controlled, observable
and efficient access to its own language models. It has three pillars:

1.  **Model Gateway --- access to models.** An OpenAI-compatible API (for OpenCode, Cline, Goose,
    Aider and any compatible client) with individually attributable API keys, team and project
    access, quotas and a fair FIFO queue in front of the inference servers.
2.  **Activity Intelligence --- log everything that matters.** Every request, key use, sign-in and
    administrative change is recorded with attribution, timing, token usage and outcome, searchable,
    exportable and retained under an explicit policy. Prompt and completion content is **not**
    retained unless an organizational policy deliberately enables it.
3.  **Routing Optimization --- use the hardware well.** As deployments multiply (more DGX Spark nodes,
    more models), Nexora decides where each request should run, from measured load, latency and
    health, explains every decision, and recommends configuration changes backed by the logged data.

Developers must never connect directly to Ollama. All inference passes through authenticated gateway,
quota and scheduling controls, so every request is attributable to a user, team, project and key.

The product stays modular: more DGX Spark systems, model deployments and inference workers can be
added without redesigning the portal.

### 1.1 Implementation status (8 November 2026)

| Phase | Scope | Status |
|---|---|---|
| 0 | Technical validation | Partly done: LiteLLM 1.108.3 validated locally against a mock backend; **validation on the real Ollama/DGX Spark nodes still open** |
| 1 | Foundation and identity | Implemented and tested |
| 2 | LiteLLM gateway and API keys | Implemented; validated against the real LiteLLM proxy |
| 3 | Quotas, usage and FIFO scheduler | Implemented; tested against real Redis (concurrency, recovery, cancellation) |
| 4 | Admin analytics and operations | Implemented: dashboard, health checks, alerts, metrics, audit search, backups. Prometheus/Grafana files are syntax-checked only |
| 5 | Activity logging depth | **Planned** |
| 6 | Routing optimization | **Core implemented** (routing, health-aware and warm-model-aware selection, failover, queue re-routing, freeze, decision records, admin UI), verified on the real nodes. **Workload lanes with reserved capacity** are implemented and measured on a real node. Latency-aware scoring, canary, rollups, recommendations and the simulator are still planned |
| 7 | Production hardening and launch | **Planned** |

The end-to-end workflow of §51 (admin creates team, developer, project and quotas; developer gets a
key and uses it; usage is metered; over-quota requests are rejected) is demonstrated by
`tests/e2e/demo.py` against the real LiteLLM proxy, with a mock standing in for Ollama.

### 1.2 Deployment context and workloads

Nexora will run on the host that already runs one of the two DGX Spark nodes (Ollama on
`localhost`); the second node is reached over the private network. Until now both nodes have served a
single workload, document extraction. The aim is to serve several workloads from the same hardware
through this one gateway, routing each request to whichever node will serve it best.

| Workload | Character | What it needs from the platform |
|---|---|---|
| Document extraction (existing) | Batch, long inputs, throughput-oriented, tolerant of waiting | Must not be starved, and must not starve others; ideally moved behind the gateway |
| PMS chatbot | Interactive, multi-turn, latency-sensitive (time to first token) | Priority, warm models, predictable queue wait |
| PMS tool (programmatic calls) | Short and structured, often JSON output, bursts | Reliable, quota-controlled, cheap routing |
| Vibe coding (OpenCode, Cline, Goose, Aider) | Long context, streaming, sustained sessions, large outputs | Coding-capable model, long-context routing, fair share |

Consequences that shape the design:

-   **Workload classes.** Each key or project belongs to a class (interactive, batch, coding) that
    drives priority lane, quotas and routing policy; the class is assigned by an administrator, never
    self-declared by the client.
-   **Warm models matter.** Ollama loads and unloads models on demand; a cold load can take seconds to
    minutes and evicts something else. Routing prefers a node where the requested model is already
    resident and manages keep-alive deliberately (§6.3).
-   **Shared hardware.** Doc extraction, chat and coding compete for the same memory and compute on each
    node. Reserved capacity and per-class limits keep one workload from degrading the rest.
-   **Anything that bypasses the gateway is invisible load.** Callers that talk to Ollama directly
    consume capacity the router cannot see, so the existing extraction service should move behind the
    gateway (own key, own class) as part of rollout.
-   **Ollama has no authentication.** It must listen on `localhost` (DGX 1, the gateway host) or a private interface
    reachable only from the gateway host (DGX 2), never on a public address.
-   **Co-location.** Running the gateway on DGX 1 shares that node's memory and CPU with inference and
    makes the gateway a single point of failure for both nodes; size memory accordingly and plan
    process supervision and, later, a standby.

#### Proposed initial placement

Keep today's document extraction where its models are already warm (DGX 2) and start the new
interactive workloads (PMS chatbot, PMS tool, vibe coding) on the idle, local DGX 1. This isolates the
workloads by placement from the first day, so lane reservations do not have to be perfect before launch,
and it produces the measurements routing needs (time to first token, load times, contention). Cross-node
routing is then enabled in stages: interactive traffic may overflow to DGX 2 only when DGX 1 is saturated
and the model is resident there; batch traffic may use DGX 1's spare capacity outside the interactive
reserve. The extraction service moves behind the gateway first (own key, `batch` class, pinned to
DGX 2) before any overflow is switched on.

#### Observed environment (read-only checks, 8 November 2026)

Measured from the development machine over the private (Tailscale) network with metadata calls only
and no generation requests, so production traffic was not disturbed. Addresses are kept out of this
document; the two nodes are on different private addresses. **DGX 1** (port `10100`) is local to the
gateway host, which will reach it on `localhost`; **DGX 2** (port `10101`) is remote.

| Item | DGX 1 (local, port 10100) | DGX 2 (remote, port 10101) |
|---|---|---|
| Reachable | Yes (about 31 ms from the development machine; loopback in production) | Yes, about 25 ms round trip |
| Ollama version | **0.40.2** | **0.40.5** |
| OpenAI-compatible `/v1` | Enabled | Enabled |
| Installed models | `qwen3-vl:8b-instruct` (8.8B Q4_K_M), `qwen3-vl:30b-a3b-instruct` (31B MoE Q4_K_M) | The same two tags |
| Currently loaded | **Nothing** (idle, cold) | **Both** (26.1 GB and 15.6 GB resident, about 42 GB of 128 GB); keep-alive differs per model |
| Capabilities / context | Completion, vision, tools; 262,144-token maximum, **65,536 in effect** when loaded | Same: **65,536 in effect** |
| Model digests | `30b`: `4695d1593d59`, `8b`: `6ec59f0dd38f` (pulled 9 Oct) | `30b`: `c871fc73fabc`, `8b`: `0533d74300e4` (pulled 17 Oct) |

**Measured with tiny requests (8 November 2026, with the owner's permission).** The 8B model, a few
tokens each, once directly and once through the real LiteLLM 1.108.3 proxy; DGX 1's 8B model, loaded by
these tests, was unloaded afterwards to restore its idle state.

| Measurement | Result |
|---|---|
| Cold load of the 8B model on DGX 1 | **5.4 s** to first token (16 GB resident afterwards because of the 64k context) |
| Warm time to first token, direct | about 0.06 s on both nodes (including the network round trip) |
| Token usage in a streamed reply (`stream_options.include_usage`) | **Reported** by Ollama's `/v1` endpoint and passed through LiteLLM: prompt, completion and cached-prompt token counts. Nexora does not need to estimate usage |
| Through LiteLLM (`openai/` adapter, names such as `qwen3-vl-8b@dgx1`) | Text, streaming with usage, and **vision** (an image input was described correctly) all work on both nodes; per-key model allow-lists are enforced (403 for a model not on the key) |
| Context in effect | 65,536 tokens on both nodes (from `/api/ps`) |
| Keep-alive | Requests that set none were given an **8-hour** expiry, so the servers' default is about 8 h. The 30B model on DGX 2 expires about 24 h out, which suggests the extraction service requests a longer keep-alive per call (inferred, to confirm) |

**Parallelism and lane isolation, measured on DGX 1 (9 November 2026, with the owner's agreement; DGX 2 was not
touched).** DGX 1 runs the 8B model **one request at a time**: 4 concurrent identical requests took 6.05 s against
about 6.3 s run serially (1.04x), and 8 concurrent took 12.04 s (1.04x). Further requests queue invisibly inside
Ollama, so a deployment's `max_active_requests` must be 1 on this node until `OLLAMA_NUM_PARALLEL` is raised (which
costs memory per extra slot). With one slot, lanes act as **priority at request boundaries** rather than split
capacity: a burst of 8 extraction-sized requests with chat requests arriving during it gave

| | First chat request during the burst | Other chat requests | Extraction burst finished in |
|---|---|---|---|
| Lanes off (plain FIFO) | **17.2 s** | 0.4-0.5 s (arrived after the burst drained) | 18.7 s |
| Lanes on | **0.6 s** (all four 0.6-1.0 s) | 0.8-1.0 s | 17.8 s |

Chat was no longer stuck behind the extraction queue, and extraction throughput did not drop.

**DGX 2 measured the same way (8 November 2026, with the owner's agreement, 8B model already resident, 64 output tokens per request, nothing loaded or unloaded):** 1 request took 1.5 s, 2 concurrent took 2.97 s, and 4 concurrent took 5.84 s, a steady 43 tokens/s in aggregate. That is also **effectively serial (parallelism 1)**, so both nodes need `max_active_requests` = 1 for now.

Not yet measured: the 30B model's cold-load time, long-context behavior near the 64k limit,
tool calling, and the `ollama_chat/` adapter.

What this means for the plan:

-   **Same tag, different model build.** The two nodes carry different digests for both models (sizes
    differ by about 48 MB), so a request for one alias could be served by subtly different weights,
    templates or parameters depending on the node. Document extraction outputs may differ between nodes
    today. Before routing treats the nodes as interchangeable they must run identical digests; the node
    sampler verifies this continuously and a mismatch raises an alert and, by policy, excludes the stale
    deployment (§6.1). The Ollama versions should be aligned too.
-   **DGX 2 is warm and busy; DGX 1 is cold, idle and older.** A request sent to DGX 1 right now pays a
    model load (seconds to minutes), though nothing needs evicting since it is empty. DGX 2 is the one
    doing the extraction work today, so it is the likely reference build. This is exactly the case
    residency-aware routing, warm-up (§6.3) and the proposed initial placement exist for, and it means
    routing defaults must be measured, not assumed.
-   **The models are vision-language models**, which suits document extraction. Chat, tool calls and
    coding may run well on them (they support tools and long context) or may want a dedicated text or
    coding model; that is a Phase 0 measurement and decision.
-   **Context in effect is 65,536 tokens**, well below the 262,144 the models support. Requests longer than this are a risk: Ollama may truncate over-long prompts rather than fail them, which would silently corrupt an extraction or a coding session. The gateway therefore enforces each deployment's context limit itself and rejects over-length requests with a clear error (to be verified against the real behavior). Raising the context costs memory on each node (the 8B model already occupies about 16 GB at 64k).
-   **Ollama has no authentication**, and anything on the private network can call both nodes. Network
    policy (for example Tailscale ACLs) should restrict them to the gateway host and the existing
    extraction service until that service moves behind the gateway.

### 1.3 Open questions (to settle before Phase 6 design is frozen)

1.  Models wanted per workload (chat, tool calls, coding, extraction). Today only the two Qwen3-VL
    models are installed on each node; which workloads should use them, which need additional models,
    and whether those fit alongside the resident ones. Which build of each model is the correct one
    (DGX 1's or DGX 2's digest; DGX 2 is probably the reference since extraction runs there), given extraction results may depend on it?
2.  Remaining Ollama settings: `OLLAMA_MAX_LOADED_MODELS`, `OLLAMA_MAX_QUEUE`, and whether to raise
    `OLLAMA_NUM_PARALLEL` (both nodes measured at an effective parallelism of 1). Context 65,536
    and an 8-hour default keep-alive are known.
    Versions `0.40.2` (DGX 1) and `0.40.5` (DGX 2) are confirmed.
3.  Whether the document-extraction service can be repointed at the gateway, and its current volume,
    input sizes and latency tolerance.
4.  Whether any workload needs embeddings (vision input works on both nodes).
5.  Whether DGX 1 (gateway host) can spare memory for Postgres, Redis and the app next to inference.

### 1.4 Scope revision log

-   **8 Nov 2026 (v2.0):** The browser coding workspace and AI agent runtime are **removed from scope**.
    Nexora is a model-access, activity-logging and routing-optimization platform. Removed: the
    coding workspace, repository connections and credentials, agent tasks and tools, isolated
    execution hosts, the Monaco editor, and the associated schema, APIs, UI, security tests and
    phases (previous Phases 5 and 6). Coding agents remain first-class *clients* of the gateway
    (OpenCode, Cline, Goose, Aider), but Nexora does not host them. A Phase 7 hardening phase remains.
    Cleanup owed in code: the implemented `projects.repository_url` / `default_branch` columns and the
    uncommitted workspace, repository, secret and workspace-event code, tables and UI are to be removed.

## 2. Product Goals

### 2.1 Primary goals

-   Provide a secure shared local-LLM service for multiple developers.
-   Give every developer individually attributable API keys.
-   Support OpenAI-compatible clients with minimal configuration changes.
-   Enforce request-count and token-count limits at global, team, user, key and project levels.
-   Provide a durable FIFO request queue with queue visibility and cancellation.
-   Record every request with attribution, token usage, queue wait, time to first token, generation
    throughput, status and error category, and keep that record trustworthy and queryable.
-   Make activity auditable: tamper-evident audit trail, searchable history, exports and retention
    policies, with privacy-preserving defaults (no prompt/completion retention by default).
-   Route requests across deployments to minimize waiting and failures and to use each inference
    server efficiently, with every routing decision explainable and reversible.
-   Turn the logged activity into concrete optimization advice (concurrency, replica placement,
    model choice) that an administrator can accept, test and roll back.
-   Give administrators a single control plane for users, teams, projects, models, keys, quotas,
    routing and infrastructure health.
-   Keep inference data within the organization's controlled infrastructure.

### 2.2 Non-goals

-   Training or fine-tuning models.
-   Public multi-tenant SaaS billing.
-   Hosting coding workspaces, a browser IDE or agent runtimes (removed in v2.0; coding agents are
    clients of the gateway, not part of it).
-   Replacing GitHub or any source-control platform.
-   Supporting arbitrary model runtimes beyond the defined provider abstraction.
-   Silently substituting a cheaper or weaker model for the one a client asked for.
-   Guaranteeing a fixed number of concurrent developers before load testing.

## 3. Success Metrics

| Area | Metric |
|---|---|
| Availability | Portal, gateway and inference availability |
| Access | Successful authentication and key provisioning |
| Quotas | Correct rejection and reservation under concurrency |
| Scheduling | FIFO ordering correctness and queue wait time (p50/p95) |
| Inference | Requests/sec, tokens/sec, time to first token |
| Reliability | Request success rate, retry and failover rate |
| Attribution | Percentage of requests with user, team, project and key attribution (target 100%) |
| Logging | Completeness of request records, audit-trail integrity checks, export success |
| Routing | Queue wait and time-to-first-token improvement versus fixed routing; failover success; share of decisions with a recorded explanation (target 100%) |
| Optimization | Recommendations accepted, and the measured effect after they were applied |
| Operations | Backup success, restore test success and alert response |

Do not use tokens/sec alone as a measure of model service quality. Track queue wait, time to first
token, error rate and completion outcome together, per model and per deployment.

---

# PART I --- PRODUCT AND USER EXPERIENCE

## 4. User Roles and Permissions

### 4.1 Super Admin

-   Full platform access.
-   Create, activate, suspend and deactivate users.
-   Create and manage teams.
-   Manage projects and memberships.
-   Register and configure models.
-   Manage inference deployments and endpoint health.
-   Configure global concurrency, queue and quota policies.
-   Generate, revoke and rotate keys where permitted.
-   Inspect organization-wide usage and request history.
-   View audit logs and security alerts.
-   Manage platform settings and integrations.

### 4.2 Team Admin

-   Manage assigned team members.
-   Create and manage team projects.
-   Assign project membership and permissions.
-   View team usage and request history.
-   Allocate quotas within administrator-defined limits.
-   Revoke keys belonging to team members when authorized.
-   View team activity and usage.

### 4.3 Developer

-   Access assigned projects.
-   Create and manage personal API keys.
-   Select permitted models.
-   Use the API gateway from compatible coding clients.
-   View personal usage, quota consumption and request history.
-   View personal activity, including routing decisions that affected their requests.

### 4.4 Authorization principles

-   Enforce permissions on the backend, not only in the frontend.
-   Deny access by default.
-   Apply team and project membership checks to every resource.
-   Prevent users from reading another user's keys, request history or activity.
-   Log privileged operations.
-   Require stronger authentication for administrative actions.

------------------------------------------------------------------------

## 5. Functional Modules

### 5.1 Authentication and User Management

Features:

-   Email/password login.
-   Secure password hashing using Argon2id or an equivalent approved
    password-hashing algorithm.
-   Password reset and account activation.
-   Session creation, expiry and revocation.
-   Secure, HTTP-only, SameSite cookies for browser sessions.
-   MFA for administrators.
-   Optional OIDC/SSO integration.
-   Account suspension and deactivation.
-   Login history and security events.
-   Role and team assignment.

Acceptance criteria:

-   Suspended users cannot authenticate or use existing API keys.
-   Session revocation takes effect promptly.
-   Authentication and authorization failures are logged without storing
    passwords or secrets.

### 5.2 Teams and Projects

Features:

-   Create teams and assign team administrators.
-   Add and remove team members.
-   Create projects under a team.
-   Assign project-level roles.
-   Set project-specific model permissions and quota policies.
-   View project usage and activity.

Suggested project access levels:

-   Owner
-   Maintainer
-   Developer
-   Read-only

### 5.3 API Key Management

Use LiteLLM virtual keys as the inference credential mechanism.

Features:

-   Create a named key for a user and optional project.
-   Assign permitted model aliases.
-   Configure key expiry.
-   Configure request and token limits.
-   Configure rate limits.
-   Rotate and revoke keys.
-   Track last-used time.
-   View key-specific usage.
-   Display the plaintext key only once at creation/rotation.

Security requirements:

-   Never store plaintext API keys in PostgreSQL.
-   Store LiteLLM key identifiers and non-reversible hashes/fingerprints
    where appropriate.
-   Do not expose the LiteLLM master key to developers or browser
    clients.
-   Revoke keys at the gateway and invalidate associated metadata.
-   Ensure a suspended user cannot continue using an otherwise active
    key.

### 5.4 Model Management

Features:

-   Register model aliases.
-   Map aliases to LiteLLM model names and Ollama deployments (a deployment is one model on one node).
-   Enable/disable models.
-   Configure model display name and description.
-   Configure context and output limits.
-   Configure allowed user/team/project scopes.
-   Track endpoint health and deployment status.
-   Support multiple deployments behind a stable alias.
-   Configure fallback behavior only where explicitly approved.

Initial model strategy:

-   Start with one validated local coding model.
-   Benchmark the existing Qwen coding model against candidate
    Qwen3-Coder-Next configurations on the actual DGX Spark.
-   Record exact model repository, quantization, runtime version and
    launch configuration.
-   Do not hard-code unverified model identifiers or assume benchmark
    figures are guaranteed.
-   Keep model configuration in the database and deployment
    configuration, not scattered through application code.

### 5.5 Quota and Rate-Limit Management

Enforce quotas at these scopes:

1.  Global platform.
2.  Team.
3.  User.
4.  API key.
5.  Optional project.

Supported limits:

-   Requests per minute.
-   Requests per day.
-   Requests per month.
-   Tokens per minute.
-   Tokens per day.
-   Tokens per month.
-   Maximum concurrent requests.
-   Maximum output tokens per request.
-   Optional model-specific limits.

Quota policy rules:

-   Effective limits must be the most restrictive applicable limits
    across scopes.
-   A request must pass all applicable policies.
-   Reserve request and estimated token capacity atomically before
    admission.
-   Reconcile reservations against actual input and output tokens.
-   Release unused reservation after completion or cancellation.
-   Record actual usage even when a request fails after consuming
    tokens.
-   Define and test treatment of provider errors, client disconnects and
    retries.
-   Use UTC-aligned quota windows with documented reset times.
-   Never rely solely on a frontend quota display.

### 5.6 FIFO Request Scheduling

The user-selected scheduling policy is FIFO.

Requirements:

-   FIFO ordering within each model deployment queue.
-   A monotonically increasing sequence or equivalent atomic ordering
    mechanism.
-   Configurable maximum active inference requests.
-   Configurable maximum queue length.
-   Queue position and request status visibility.
-   Cancellation for queued requests.
-   Defined handling for cancellation of running streaming requests.
-   Request timeout and stale-job cleanup.
-   Durable request records in PostgreSQL.
-   Redis for fast queue state and coordination.
-   No silent request dropping.
-   Queue-full response with a clear retry policy.
-   Scheduler recovery after process restart.

Important distinction:

-   Queue admission and GPU execution are separate.
-   A request waiting in the queue must not reserve an inference slot.
-   Once admitted, stream tokens to the client without buffering the
    full completion.
-   Do not claim that the inference server itself guarantees FIFO scheduling. The
    application scheduler controls admission order; Ollama handles
    loading, batching and execution.
-   Begin with one queue per model deployment. Document that FIFO is
    guaranteed within each queue, not necessarily globally across
    unrelated models.

### 5.7 Usage Tracking and Analytics

Track every inference request with:

-   Request ID.
-   User ID.
-   Team ID.
-   Project ID.
-   API key ID.
-   Model/deployment ID.
-   Source: API gateway, with the client tool (OpenCode, Cline, ...) when detected.
-   Input tokens.
-   Output tokens.
-   Total tokens.
-   Queue wait time.
-   Time to first token.
-   Generation duration.
-   Status and error category.
-   Created, started and completed timestamps.
-   Idempotency key where applicable.

A single client session (for example a coding agent working for an hour) produces many inference
requests. Nexora records and attributes every request individually; grouping requests into sessions
is a reporting feature (a client-supplied session header, Phase 5), never a reason to skip per-request
accounting.

Analytics:

-   Usage by user, team, project, API key and model.
-   Input/output token trends.
-   Requests per minute/day/month.
-   Queue depth and wait time.
-   Time to first token.
-   Generation throughput.
-   Success and failure rates.
-   Active users and keys.
-   Exportable CSV reports.

### 5.8 Request History

Developers can see their own requests. Administrators can inspect
requests within their permission scope.

Display:

-   Request ID.
-   Model.
-   Project.
-   Source.
-   Status.
-   Token usage.
-   Queue time.
-   Duration.
-   Error category.
-   Timestamp.

Prompt and completion content should not be retained by default unless a
specific, documented organizational policy requires it. If content
retention is enabled, disclose it to users, define retention periods and
restrict access.

### 5.9 Alerts and Notifications

Alert types:

-   Inference server unavailable.
-   Model unhealthy or loading failed.
-   Queue length above threshold.
-   Queue wait time above threshold.
-   Token or request quota approaching limit.
-   Quota exceeded.
-   Deployment draining, failing over or flapping between healthy and unhealthy.
-   Disk or memory pressure.
-   Repeated authentication failures.
-   Suspicious API-key usage.

Support in-app notifications initially. Add email or other channels
later.

------------------------------------------------------------------------

### 5.10 Activity Logging and Audit

Nexora keeps three complementary records, each append-only:

-   **Request log** (`inference_requests`, `request_events`, `usage_records`): one row per model
    request with attribution, model/deployment, tokens, queue wait, time to first token, duration,
    status, error category and every state transition. *Implemented (Phases 3--4).*
-   **Audit log** (`audit_logs`): privileged actions and authentication events. Database-enforced
    append-only. *Implemented; tamper evidence below is planned.*
-   **Activity feed**: a unified, filterable view over the above for administrators, team admins and
    developers, scoped by role.

Planned (Phase 5):

-   **Richer request context:** client IP, user agent / detected client tool (OpenCode, Cline, ...),
    API key id, and the correlation id propagated to LiteLLM and Ollama so one request can be traced
    end to end without exposing its content.
-   **Tamper evidence:** each audit row stores a hash of its content and of the previous row
    (hash chain); a verifier job and `GET /admin/audit-logs/verify` detect gaps or edits. The
    existing trigger already blocks UPDATE and DELETE; the chain also covers superuser tampering.
-   **Retention and archival:** configurable retention per record class (for example request log
    13 months, audit log 7 years), monthly partitioning of high-volume tables, and a job that
    archives expired partitions to compressed, encrypted files before dropping them.
-   **Content logging is OFF by default.** Prompts and completions contain source code and
    secrets. A super admin may enable it only per team or project, with: a recorded justification,
    visible disclosure to affected users, field-level encryption, redaction of obvious secrets
    (API keys, private-key blocks, tokens), a short retention window, restricted read access, and
    an audit entry for every read. Without it, Nexora stores sizes and hashes only.
-   **Export and integration:** CSV and JSON exports; streaming to a webhook or syslog/SIEM sink with
    at-least-once delivery and a dead-letter table; no secrets in any export.
-   **Anomaly and abuse signals:** unusual key use (new network, burst far above its baseline,
    use outside normal hours), repeated authentication failures, quota-probing patterns. These raise
    in-app alerts (Phase 4 mechanism) and are written to the audit log.
-   **Cost-equivalent reporting:** token usage per team and project priced against a configurable
    reference rate card, for internal showback (not billing).

Acceptance criteria:

-   100% of gateway requests, including rejected and cancelled ones, leave a request-log record.
-   Editing or deleting any audit row is detected by the verifier.
-   Enabling content logging requires the documented policy fields and is itself audited.
-   Exports never contain keys, tokens, passwords or (unless content logging is on) message content.

---

# PART II --- ROUTING OPTIMIZATION

## 6. Routing Optimization Requirements

Today each model alias maps to one deployment with one FIFO queue. Routing optimization adds
**multiple deployments per alias** and a policy that picks where each request runs, then uses the
activity log to improve that policy over time. Principles:

-   **Never change what the client asked for without a policy that allows it.** Routing chooses
    *where* to run a model, not silently *which* model, unless an alias is explicitly defined as an
    "auto" alias with documented rules.
-   **Every decision is explainable.** Store the candidates considered, the scores, the policy
    version and the reason for the choice, and expose them for each request.
-   **Never replay a request of unknown outcome.** Failover happens only before dispatch, or after a
    failure that is known not to have started generation (connection refused, deployment marked
    unhealthy). After the first generated token, or when the outcome is unknown, the request fails
    rather than being re-sent (see §47).
-   **Preserve FIFO per deployment queue.** Routing decides the queue at admission; within a queue
    order stays strict. Re-routing queued (not running) requests when a deployment turns unhealthy
    preserves their original sequence relative to each other.
-   **Reversible and safe to experiment with.** Policies are versioned, can run as a canary for a
    percentage of traffic, and a single "freeze" switch returns to fixed routing instantly.

### 6.1 Deployments and capacity

Each deployment records: host or node, endpoint reference (no credentials), enabled flag, weight,
maximum active requests, context limit, capability tags (for example `long-context`, `fast`),
health status from the Phase 4 checks, and optional drain mode (finish running work, accept none).
Capacity has two levels, because Ollama shares a node's memory and compute across loaded models:
a per-deployment limit (`max_active_requests`, at or below the node's `OLLAMA_NUM_PARALLEL`) and a
per-node concurrency budget across all of that node's deployments. A request is dispatched only when
both have room.

**Design decision: routing lives in Nexora, not in LiteLLM.** Each deployment is registered in LiteLLM
under its own model name (for example `qwen3-coder@spark-a` and `qwen3-coder@spark-b`), and Nexora
chooses which one to call. LiteLLM provides keys, provider adaptation and usage reporting but is
not given two entries with the same name to balance, so its router and Nexora's never double-balance
and every decision stays in Nexora's decision record and per-deployment FIFO queues. A key's model
list in LiteLLM therefore contains every deployment name of the aliases it may use.

**Equivalence rule.** Deployments of one alias must serve the same model build. The node sampler records
each deployment's model digest and each node's Ollama version; deployments of an alias whose digests
differ are flagged, raise an alert, and (configurable, default on) are excluded from routing until an
administrator resolves it. This is also what makes failover and performance comparisons between nodes
meaningful.

Adding a DGX Spark node is: run Ollama on it, pull the models, register its deployments, set capacity
from benchmarking, enable.

### 6.2 Routing strategies

Configured per model alias, selectable and combinable:

1.  **Fixed (default):** the first healthy deployment; today's behavior.
2.  **Health-aware:** exclude unhealthy and draining deployments (always applied).
3.  **Residency-aware (Ollama):** prefer a deployment whose model is already loaded on its node
    (from the node's `/api/ps`), because a cold start can cost seconds to minutes and evicts another
    model; fall back to a cold node only when the warm one is saturated, and account for the load time
    in the decision.
4.  **Least loaded:** score = (queued + active) / capacity per deployment; pick the lowest. Uses live
    Redis queue state.
5.  **Latency-aware:** exponentially weighted moving averages of time to first token and generation
    tokens/sec per deployment, penalizing recent errors; avoids routing to a deployment that is
    slow even if its queue looks short.
6.  **Weighted round-robin:** operator-set weights, for gradual rollouts and uneven hardware.
7.  **Context-length aware:** estimate input plus requested output tokens; route requests that exceed
    a deployment's context or memory comfort zone to deployments tagged `long-context`.
8.  **Workload lanes (implemented):** classes `pms`, `coding`, `extraction` and a catch-all `standard`, assigned by a
    super admin to a project or key (a key's class wins) and never chosen by the client. Each class reserves a share
    of every deployment's slots (default 50/25/25). A free slot goes first to a waiting class still under its
    reservation, then by priority number (extraction first), then by age; order inside a class stays strictly FIFO.
    Idle reserved slots can be borrowed, except the slots of a class active within `borrow_idle_after_s` (default
    60 s), which are held, but never so many that every slot is held. Running requests are never preempted. The
    router's score is class-aware.
9.  **Explicit fallback chain:** alias A may fall back to alias B (or deployment 2) only if an
    administrator approved that exact fallback; the response carries a header naming the model that
    actually served the request.
10. **Auto alias (later, opt-in):** an alias such as `auto` that chooses between named models using
    simple, inspectable rules (prompt length, tool-call presence, key tier). Off unless an admin
    defines it; always labeled in the response and the log.

Tie-breaking is deterministic (stable ordering by deployment id) so decisions are reproducible in
tests and in the simulator.

### 6.3 Ollama-specific behavior to design for

Ollama differs from a batching inference server in ways the router must respect (to be confirmed on the
actual nodes in Phase 0, since behavior varies by version):

-   **Residency and load time.** `/api/ps` reports which models are loaded on a node and their memory
    use; `/api/tags` lists what is installed. Nexora samples both per node and records load durations.
-   **Keep-alive and warm-up.** Models unload after `OLLAMA_KEEP_ALIVE` of inactivity. Nexora can keep
    a priority model warm with periodic lightweight requests, and warm a model before shifting traffic
    to it, instead of letting the first user pay the cold start. Warm-ups are logged and never counted
    against a user's quota.
-   **Context length.** The default context window is small and, on the OpenAI-compatible endpoint, is
    set by the model's configuration or an environment setting rather than per request. Long-document
    extraction and coding sessions must run on models configured with enough context, and the router
    treats a deployment's configured context as a hard limit (§6.2 context-length awareness).
-   **Parallelism.** Requests beyond `OLLAMA_NUM_PARALLEL` queue inside Ollama; keep Nexora's limits at
    or below it (§21.4). More parallel slots enlarge memory use per loaded model.
-   **Usage reporting.** Verified on both nodes: streamed responses carry exact token usage through the
    OpenAI-compatible endpoint and through LiteLLM. Nexora keeps its estimate-based fallback (and flags
    such counts as estimated) for other backends or if a stream is cut short.
-   **Native metrics.** Ollama's native API reports load, prompt-evaluation and generation durations and
    token counts, which give time to first token and generation tokens per second per deployment.
-   **No authentication or per-request identity.** The gateway is the only client that may reach it.

### 6.4 Decision record and explainability

For every routed request store: policy id and version, strategy, the candidate deployments with their
inputs (queue depth, active, health, EWMA latency, weight), the chosen deployment, the reason, and
whether it was a failover (and from where). Expose `GET /admin/routing/explain/{request_id}` and show
the decision in the request detail view. Decisions are retained like the request log.

### 6.5 Measurement and rollups

A rollup job turns the request log into per-deployment windows (1 minute, 1 hour, 1 day): request
count, error rate, queue wait p50/p95/p99, time to first token p50/p95, generation tokens/sec,
maximum queue depth, utilization (active/capacity). These feed routing scores, dashboards, alerts and
recommendations, and keep reporting fast as the request log grows.

### 6.6 Recommendations and what-if simulation

The system proposes changes and shows the evidence; an administrator applies or dismisses them.
Examples: raise or lower a deployment's maximum active requests (queue wait p95 high while memory and
time-to-first-token headroom exist); shift weight toward a faster node; add a replica for a model whose
peak queue wait exceeds a threshold; mark a flaky deployment for investigation; recommend a context
limit for long-prompt traffic.

A **replay simulator** takes a window of the request log (arrival times, token counts, durations) and
re-runs it against a candidate configuration (different concurrency, strategy or deployment set) to
estimate queue wait and utilization *before* anything changes in production. Simulation assumes the
logged service times; it is an estimate, and the UI says so.

### 6.7 Safe rollout

Policy changes are versioned and audited. A new policy can run on a canary percentage (chosen
deterministically from the request id, so a request's arm is reproducible), is compared against the
control on queue wait, time to first token and error rate, and can be promoted or rolled back with one
action. The freeze switch overrides everything and routes with the fixed strategy.

Acceptance criteria:

-   A digest or version mismatch between deployments of one alias is detected within one sampling
    interval and raises an alert; with the default policy the stale deployment is not routed to.
-   With both nodes healthy and the target model resident on only one, residency-aware routing sends
    requests there and avoids a cold load while it has capacity.
-   A burst of document-extraction requests does not raise the chatbot's p95 time to first token beyond
    its target (lane isolation), and the chatbot's reserved capacity is observable.
-   With two healthy deployments and one slowed artificially, latency-aware and least-loaded routing
    measurably reduce p95 queue wait compared with fixed routing in a load test.
-   Killing a deployment mid-load causes no duplicate executions and no lost requests: queued requests
    move, running ones finish or fail cleanly, and every move is recorded.
-   Per-deployment FIFO order is preserved under concurrent admission and re-routing.
-   Every routed request has a decision record; the explanation reproduces from stored inputs.
-   The simulator's estimate for the *current* configuration is within an agreed tolerance of the
    measured queue wait on the same window.
-   Freeze returns to fixed routing within one routing cycle.

---

# PART III --- TECHNICAL ARCHITECTURE

## 7. Technology Stack

| Layer | Technology | Responsibility |
|---|---|---|
| Frontend | Next.js, React, TypeScript | Portal and admin console |
| Backend | Python FastAPI | Management, gateway and orchestration APIs |
| Database | PostgreSQL | Persistent business data, request log, audit trail |
| Cache/Queue | Redis | FIFO queues, counters, leases, routing state |
| LLM gateway | LiteLLM Proxy | Virtual keys, model routing to backends, usage integration |
| Inference | Ollama (two DGX Spark nodes) | Local model serving |
| Reverse proxy | Nginx | TLS and routing |
| Monitoring | Prometheus/Grafana | Metrics and dashboards |
| Logging | Structured application logs | Troubleshooting and audit correlation |

### 7.1 Service boundaries

1.  **Frontend:** presentation and user interaction.
2.  **FastAPI management service:** identity, teams, projects, keys, quotas, reports, administration.
3.  **Gateway and scheduler:** OpenAI-compatible endpoints, admission control, quota reservations,
    FIFO queues, cancellation and request lifecycle.
4.  **Router:** deployment selection, health- and load-aware scoring, failover and decision records
    (a module of the scheduler at first; separable if it needs to scale independently).
5.  **Ops and analytics:** health checks, alert evaluation, rollups, recommendations, simulation.
6.  **LiteLLM:** virtual-key verification and model routing to inference backends.
7.  **Ollama:** model inference on DGX Spark nodes.
8.  **PostgreSQL:** durable source of truth.
9.  **Redis:** fast coordination and transient state.

Avoid circular dependencies. Components call well-defined internal interfaces rather than directly
modifying each other's tables.

## 8. High-Level Architecture

``` text
Developers and tools
  |-- Browser (portal, admin console)
  |-- OpenCode / Cline / Goose / Aider (OpenAI-compatible clients)
  |
Public HTTPS
  |
Nginx / WAF / Rate Limiting
  |
  +-- Next.js Portal
  |
  +-- FastAPI Management API
  |
  +-- OpenAI-Compatible Inference API
          |
          v
    Authentication / Policy
          |
    Quota Reservation
          |
    Router  <------ live queue/health state, latency stats
          |   (chooses a deployment; records the decision)
          v
    Redis FIFO queue (one per deployment)
          |
    LiteLLM Proxy
          |
    Private VPN / Application Network
          |
    DGX Spark node(s) -- Ollama -- Local models

Every request is written to the request log (PostgreSQL) with its routing decision;
rollups, alerts and recommendations are computed from that log.
```

All model requests must be attributable to a user, team, project and key.

## 9. Public and Private Network Design

Publicly expose only the HTTPS ingress.

Private services:

-   FastAPI internal endpoints.
-   PostgreSQL.
-   Redis.
-   LiteLLM administration.
-   Ollama.
-   Prometheus and Grafana.

Recommended deployment topology:

  Component          Placement
  ------------------ ---------------------------------------
  Nginx ingress      Public-facing VPS or DMZ
  Next.js            Application server
  FastAPI            Application server
  PostgreSQL         Private database network
  Redis              Private application network
  LiteLLM            Private application/inference network
  Scheduler          Private application network
  DGX Spark/Ollama     Private network (never public)

Use WireGuard or another managed private VPN to connect the application
environment to DGX Spark.

Security controls:

-   HTTPS-only.
-   Secure session cookies.
-   MFA for administrators.
-   Strong API key handling.
-   Per-user and per-project authorization.
-   Network-level service isolation.
-   WAF and ingress rate limits.
-   Secrets manager or protected environment injection.
-   Audit logs.
-   Encrypted backups.
-   Regular dependency and container image updates.

------------------------------------------------------------------------

# PART IV --- DATABASE DESIGN

## 10. PostgreSQL Schema

Use UUID primary keys, UTC timestamps, foreign keys, indexes, migrations
and explicit lifecycle states.

### 10.1 Identity and organization

#### `roles`

-   id
-   name
-   permissions JSONB
-   created_at

#### `users`

-   id
-   role_id
-   name
-   email
-   password_hash
-   status
-   last_login_at
-   created_at
-   updated_at

#### `teams`

-   id
-   name
-   status
-   created_at

#### `team_members`

-   id
-   team_id
-   user_id
-   membership_role
-   joined_at

#### `projects`

-   id
-   team_id
-   name
-   description
-   status
-   created_at

#### `project_members`

-   project_id
-   user_id
-   access_level
-   created_at

### 10.2 Model and API key management

#### `models`

-   id
-   name
-   model_alias
-   provider
-   context_limit
-   max_output_tokens
-   enabled
-   configuration JSONB

#### `model_deployments`

-   id
-   model_id
-   deployment_name
-   endpoint_reference
-   health_status
-   configuration JSONB
-   created_at

Do not store sensitive endpoint credentials in ordinary configuration
JSON. Use secret references.

Routing extends this table (Phase 6): `weight`, `enabled`, `draining`, `max_active_requests`,
`context_limit`, `tags`, `node` (host label) and a unique `(model_id, deployment_name)` constraint.

#### `api_keys`

-   id
-   user_id
-   team_id
-   project_id
-   litellm_key_id
-   name
-   expires_at
-   status
-   last_used_at
-   created_at
-   revoked_at

### 10.3 Quota management

#### `quotas`

-   id
-   scope_type
-   scope_id
-   period
-   max_requests
-   max_tokens
-   max_parallel_requests
-   requests_per_minute
-   tokens_per_minute
-   created_at
-   updated_at

#### `quota_counters`

-   id
-   quota_id
-   window_start
-   requests_reserved
-   requests_used
-   tokens_reserved
-   tokens_used
-   updated_at

Use atomic Redis reservations for admission. PostgreSQL remains the
durable accounting source. Define reconciliation and recovery behavior
for Redis restarts.

### 10.4 Inference and usage

#### `inference_requests`

-   id
-   idempotency_key
-   user_id
-   team_id
-   project_id
-   api_key_id
-   model_id
-   deployment_id
-   source
-   status
-   queue_sequence
-   input_tokens
-   output_tokens
-   queue_time_ms
-   time_to_first_token_ms
-   duration_ms
-   error_code
-   error_message
-   created_at
-   started_at
-   completed_at

#### `request_events`

-   id
-   request_id
-   event_type
-   event_data JSONB
-   created_at

#### `usage_records`

-   id
-   request_id
-   user_id
-   team_id
-   project_id
-   api_key_id
-   model_id
-   input_tokens
-   output_tokens
-   total_tokens
-   recorded_at

### 10.5 Routing and activity logging

Planned (Phases 5--6). Tables are append-only unless stated.

#### `routing_policies`

-   id
-   model_id
-   version
-   strategy (fixed, least_loaded, latency_aware, weighted, composite)
-   configuration JSONB (weights, thresholds, fallback chain, lanes, canary percentage)
-   status (draft, canary, active, retired)
-   created_by, created_at, activated_at

#### `routing_decisions`

-   request_id
-   policy_id, policy_version
-   strategy
-   chosen_deployment_id
-   candidates JSONB (per deployment: queue depth, active, health, EWMA latency, weight, score)
-   reason
-   failover_from_deployment_id (nullable)
-   arm (control or canary)
-   created_at

#### `deployment_metrics`

Rollup of the request log per deployment and window (minute, hour, day): window_start, requests,
errors, queue_wait_ms p50/p95/p99, time_to_first_token_ms p50/p95, generation_tokens_per_s,
max_queue_depth, utilization. Rebuilt from `inference_requests` if lost.

#### `routing_recommendations`

-   id, kind (concurrency, weight, replica, context_limit, investigate)
-   model_id, deployment_id
-   summary, evidence JSONB (metric windows and the numbers behind the advice)
-   status (open, applied, dismissed), decided_by, decided_at, created_at

#### Activity logging additions

-   `inference_requests`: add `client_ip`, `user_agent`, `client_tool`, `session_id`, `correlation_id`.
-   `audit_logs`: add `prev_hash` and `row_hash` (hash chain).
-   `content_logs` (only when content logging is enabled for a scope): request_id, encrypted
    prompt/completion, redaction report, `retain_until`, `access_log` references.
-   `log_sinks`: type (webhook, syslog), destination reference (secret), filters, status.
-   `retention_policies`: record class, retention period, archive flag.

### 10.6 Administration and operations

#### `audit_logs`

-   id
-   actor_id
-   action
-   entity_type
-   entity_id
-   metadata JSONB
-   created_at

#### `alerts`

-   id
-   alert_type
-   severity
-   message
-   status
-   created_at
-   resolved_at

#### `idempotency_records`

-   id
-   user_id
-   idempotency_key
-   request_hash
-   response_reference
-   expires_at
-   created_at

### 10.7 Database requirements

-   Unique email and model aliases.
-   Unique LiteLLM key IDs.
-   Unique project membership.
-   Foreign keys and appropriate delete restrictions.
-   Index request and usage tables by user and timestamp.
-   Index queue status and creation time.
-   Partition high-volume request/usage tables when needed.
-   Use database migrations for every schema change.
-   Keep audit and usage records append-only.
-   Define retention and archival policies.
-   Use transactional updates for related business state.
-   Do not use PostgreSQL as the real-time FIFO queue.

------------------------------------------------------------------------

# PART V --- API SPECIFICATION

## 11. API Conventions

Management API prefix: `/api/v1`\
Inference API prefix: `/v1`

Conventions:

-   JSON request and response bodies.
-   UUID resource identifiers.
-   UTC ISO-8601 timestamps.
-   Consistent error envelope.
-   Pagination using cursor or limit/offset.
-   Request correlation ID.
-   Idempotency support for create/submit operations.
-   OpenAPI documentation.
-   Explicit authorization checks.
-   Versioned APIs.

Suggested error envelope:

``` json
{
  "error": {
    "code": "QUOTA_EXCEEDED",
    "message": "The applicable token quota has been exceeded.",
    "request_id": "uuid",
    "details": {}
  }
}
```

## 12. Authentication APIs

  Method   Endpoint                  Purpose
  -------- ------------------------- ------------------------
  POST     `/auth/login`             Authenticate
  POST     `/auth/logout`            End session
  POST     `/auth/refresh`           Refresh session
  GET      `/auth/me`                Current user
  POST     `/auth/password/forgot`   Password reset request
  POST     `/auth/password/reset`    Reset password
  POST     `/auth/mfa/verify`        Verify MFA
  POST     `/auth/mfa/enroll`        Enroll MFA

## 13. User and Team APIs

  Method   Endpoint                          Purpose
  -------- --------------------------------- -----------------
  GET      `/users`                          List users
  POST     `/users`                          Create user
  GET      `/users/{id}`                     User details
  PATCH    `/users/{id}`                     Update user
  POST     `/users/{id}/suspend`             Suspend user
  POST     `/users/{id}/activate`            Activate user
  GET      `/teams`                          List teams
  POST     `/teams`                          Create team
  GET      `/teams/{id}`                     Team details
  PATCH    `/teams/{id}`                     Update team
  POST     `/teams/{id}/members`             Add team member
  DELETE   `/teams/{id}/members/{user_id}`   Remove member

## 14. Project APIs

  Method   Endpoint                             Purpose
  -------- ------------------------------------ --------------------------
  GET      `/projects`                          List accessible projects
  POST     `/projects`                          Create project
  GET      `/projects/{id}`                     Project details
  PATCH    `/projects/{id}`                     Update project
  POST     `/projects/{id}/members`             Add project member
  DELETE   `/projects/{id}/members/{user_id}`   Remove project member

## 15. API Key APIs

  Method   Endpoint              Purpose
  -------- --------------------- ----------------------
  GET      `/keys`               List accessible keys
  POST     `/keys`               Create key
  GET      `/keys/{id}`          Key metadata
  PATCH    `/keys/{id}`          Update policy
  POST     `/keys/{id}/rotate`   Rotate key
  POST     `/keys/{id}/revoke`   Revoke key
  GET      `/keys/{id}/usage`    Key usage

## 16. Model and Quota APIs

  Method   Endpoint                       Purpose
  -------- ------------------------------ -----------------------
  GET      `/models`                      List permitted models
  GET      `/models/{id}`                 Model details
  POST     `/admin/models`                Register model
  PATCH    `/admin/models/{id}`           Update model
  POST     `/admin/models/{id}/enable`    Enable model
  POST     `/admin/models/{id}/disable`   Disable model
  GET      `/quotas/me`                   Personal quota
  GET      `/quotas/team/{id}`            Team quota
  POST     `/admin/quotas`                Create quota
  PATCH    `/admin/quotas/{id}`           Update quota

## 17. Inference and Queue APIs

OpenAI-compatible endpoints:

-   `GET /v1/models`
-   `POST /v1/chat/completions`
-   `POST /v1/completions` where supported

Management endpoints:

  Method   Endpoint               Purpose
  -------- ---------------------- -------------------------------
  POST     `/queue/estimate`      Estimate queue conditions
  GET      `/queue/status/{id}`   Request state
  POST     `/queue/{id}/cancel`   Cancel queued/running request
  GET      `/queue/me`            User's pending requests
  GET      `/admin/queue`         Admin queue overview

The gateway must support streaming and preserve the response semantics
expected by compatible coding clients.

For queued requests, define an explicit admission protocol. Do not
return a fake OpenAI streaming response while the request is still
waiting unless the client integration explicitly supports that behavior.
Use a compatible request-pending mechanism or an API gateway adapter
that preserves the expected client contract.

## 18. Usage APIs

  Method   Endpoint                Purpose
  -------- ----------------------- -----------------
  GET      `/usage/me`             Personal usage
  GET      `/usage/team/{id}`      Team usage
  GET      `/usage/project/{id}`   Project usage
  GET      `/usage/report`         Filtered report
  GET      `/usage/requests`       Request history
  GET      `/usage/export`         CSV export

## 19. Routing and Activity APIs

Planned (Phases 5--6). All routing endpoints are super-admin only; activity endpoints are scoped by
role (developers see their own, team admins their teams, super admins everything).

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/admin/deployments` | List deployments with capacity, weight, health, load |
| POST | `/admin/deployments` | Register a deployment |
| PATCH | `/admin/deployments/{id}` | Weight, capacity, tags, enable, drain |
| GET | `/admin/routing/policies` | List policies and versions |
| POST | `/admin/routing/policies` | Create a draft policy |
| POST | `/admin/routing/policies/{id}/canary` | Run on a percentage of traffic |
| POST | `/admin/routing/policies/{id}/activate` | Promote |
| POST | `/admin/routing/policies/{id}/rollback` | Return to the previous version |
| POST | `/admin/routing/freeze` | Kill switch: fixed routing everywhere |
| GET | `/admin/routing/decisions` | Search decisions |
| GET | `/admin/routing/explain/{request_id}` | Why this deployment was chosen |
| GET | `/admin/routing/metrics` | Per-deployment rollups |
| GET | `/admin/routing/recommendations` | Open recommendations with evidence |
| POST | `/admin/routing/recommendations/{id}/apply` | Apply a recommendation (audited) |
| POST | `/admin/routing/recommendations/{id}/dismiss` | Dismiss |
| POST | `/admin/routing/simulate` | Replay a log window against a candidate configuration |
| GET | `/activity` | Role-scoped activity feed with filters |
| GET | `/activity/export` | CSV or JSON export |
| GET | `/admin/audit-logs/verify` | Verify the audit hash chain |
| GET/PUT | `/admin/settings/content-logging` | Content-logging policy (default off) |
| GET/POST/DELETE | `/admin/log-sinks` | Webhook and syslog destinations |
| GET/PUT | `/admin/retention` | Retention policy per record class |

Responses from `/v1/*` carry `x-nexora-request-id` (implemented) and, once routing exists,
`x-nexora-deployment` and `x-nexora-model` so a client can see what actually served it.

## 20. Admin and Health APIs

  Method   Endpoint              Purpose
  -------- --------------------- -------------------
  GET      `/admin/dashboard`    Platform KPIs
  GET      `/admin/inference`    Inference health
  GET      `/admin/queue`        Queue health
  GET      `/admin/audit-logs`   Audit history
  GET      `/admin/alerts`       Alerts
  PATCH    `/admin/settings`     Platform settings
  GET      `/health/live`        Liveness
  GET      `/health/ready`       Readiness

------------------------------------------------------------------------

# PART VI --- FIFO SCHEDULER DESIGN

## 21. Queue Architecture

Use Redis for fast FIFO operations and PostgreSQL for durable request
state.

Suggested Redis keys:

``` text
nexora:queue:{deployment_id}
nexora:request:{request_id}
nexora:active:{deployment_id}
nexora:quota:{scope}:{period}
nexora:events:{user_id}
nexora:sequence:{deployment_id}
```

Use a Redis List or a sequence-backed sorted structure with atomic
insertion and removal. The selected implementation must guarantee stable
ordering under concurrent producers.

### 21.1 Admission flow

1.  Receive request.
2.  Authenticate API key or browser session.
3.  Resolve user, team, project and model.
4.  Validate model permissions.
5.  Check global, team, user, project and key quotas.
6.  Validate request size and output-token limit.
7.  Atomically reserve request and estimated token capacity.
8.  Create durable request record.
9.  Route: the Router selects the target deployment (§6) and records the decision. With one
    deployment per model this is trivial; with several it uses health, load and latency.
10. Insert request into the chosen deployment's FIFO queue.
11. Return request identifier and queue state.

### 21.2 Execution flow

1.  Scheduler checks available inference capacity.
2.  Atomically claims the oldest queued request.
3.  Marks request as processing.
4.  Forwards it to LiteLLM.
5.  LiteLLM routes to Ollama.
6.  Stream response to the client.
7.  Record actual token consumption and timing.
8.  Reconcile quota reservations.
9.  Release inference slot.
10. Continue with the next queued request.

### 21.3 Failure and recovery

-   Use idempotency keys to prevent duplicate submissions.
-   Use leases/heartbeats for active scheduler jobs.
-   Recover expired processing leases after worker failure.
-   Avoid automatic duplicate execution if the outcome of a model
    request is unknown.
-   Reconcile PostgreSQL and Redis after restart.
-   Release reservations safely.
-   Record all state transitions.
-   Define cancellation semantics for queued and active requests.

### 21.4 FIFO and Ollama concurrency

FIFO applies to scheduler admission order. Ollama runs up to `OLLAMA_NUM_PARALLEL` requests per
loaded model at once and queues the rest internally (up to `OLLAMA_MAX_QUEUE`, after which it answers
503). Nexora must keep that internal queue empty: a deployment's `max_active_requests` is set **at or
below** the node's parallelism for that model, so waiting happens in Nexora's queue, where it is fair,
cancellable, visible and measured, instead of invisibly inside Ollama. Do not promise strict
token-by-token completion ordering across concurrent requests.

Several loaded models share one node's unified memory and compute, so the per-model limit is not the
whole story: a node also has a total concurrency budget (§6.1). Initial configuration must be
benchmarked on the actual models and Ollama settings. Start conservatively with small limits, then
raise them after measuring memory, latency and throughput.

---

# PART VII --- FRONTEND DESIGN

## 22. Developer Portal

Navigation:

-   Dashboard
-   Projects
-   API Keys
-   Model Playground
-   Usage & Quotas
-   Request History
-   Profile & Settings

Dashboard widgets:

-   Requests today.
-   Input/output tokens.
-   Active keys.
-   Pending requests.
-   Quota consumption.
-   Recent requests.
-   Model availability.

### 22.1 API Key screen

-   Create key modal.
-   Name and project selection.
-   Model permissions.
-   Expiration.
-   Optional quota override within policy.
-   One-time secret display.
-   Copy configuration for OpenCode and other clients.
-   Rotate and revoke actions.
-   Per-key usage.

### 22.2 Usage screen

Filters:

-   Date range.
-   Project.
-   API key.
-   Model.
-   Status.
-   Source.

Charts:

-   Requests over time.
-   Input/output token trends.
-   Usage by model.
-   Usage by project.
-   Latency distribution.
-   Queue wait time.

### 22.3 Queue screen

-   Request ID.
-   Current state.
-   Queue position.
-   Model.
-   Submission time.
-   Estimated wait where reliable.
-   Cancellation action.
-   Clear explanations for full queue and quota rejection.

## 23. Admin Portal

Navigation:

-   Dashboard
-   Developers
-   Teams
-   Projects
-   API Keys
-   Models
-   Inference Servers
-   Request Queue
-   Usage Analytics
-   Quota Policies
-   Alerts
-   Audit Logs
-   System Settings

Dashboard:

-   Active developers.
-   Requests today.
-   Tokens processed.
-   Queue depth.
-   Active inference requests.
-   GPU utilization and memory.
-   Model health.
-   Failed requests.
-   Average time to first token.
-   Average generation throughput.

## 24. Routing and Activity UI

Planned (Phases 5--6).

**Routing (admin):**

-   Deployments: health, capacity, weight, load, drain toggle, live queue depth per deployment.
-   Policy editor: strategy and parameters with validation, canary controls, version history,
    one-click rollback and a prominent freeze switch.
-   Decision explorer: search by request, model, deployment or time; open any request to see its
    candidates, scores and reason.
-   Recommendations: each with the evidence chart, an *Apply* action, and the measured effect after
    it was applied.
-   Simulator: choose a log window and a candidate configuration, see estimated queue wait and
    utilization next to the measured values, clearly labeled as an estimate.

**Activity:**

-   Unified feed with filters (actor, key, project, model, status, time), request detail with its
    timeline and routing decision, and exports.
-   Admin: audit-chain verification result, log sinks, retention policies, and the content-logging
    policy page that states plainly what is stored and who can read it.
-   Developers see their own activity and why a request waited or was routed elsewhere.

Use Server-Sent Events or polling for live views, with authentication and authorization on every
subscription.

# PART VIII --- DEPLOYMENT AND INFRASTRUCTURE

## 25. Deployment Topology

### Public ingress

-   Nginx.
-   TLS certificates.
-   WAF or equivalent edge protection.
-   Public DNS.
-   Ingress request-size and rate limits (streaming responses need long read timeouts and
    buffering disabled, or waiting streams are cut).

### Application server

-   Next.js.
-   FastAPI (management API, gateway, scheduler, router, ops).
-   LiteLLM.
-   PostgreSQL.
-   Redis.
-   Monitoring agents.

### DGX Spark node(s)

-   Ollama (one deployment per model per node).
-   Local model files.
-   Private VPN interface.
-   GPU and model metrics exporter (DCGM / node_exporter).

Each additional node is registered as a deployment of the models it serves (§6.1). Keep inference
nodes on the private network only; the application tier is the sole client.

## 26. Repository Structure

``` text
nexora/
├── apps/
│   ├── api/                 # FastAPI: management, gateway, scheduler, router, ops
│   └── web/                 # Next.js portal and admin console
│
├── infra/
│   ├── litellm/             # LiteLLM configuration
│   ├── mock-llm/            # mock LiteLLM/Ollama for local development
│   ├── monitoring/          # Prometheus, alert rules, Grafana dashboards
│   ├── backup/              # backup and restore-test scripts
│   └── compose/             # local dependencies
│
├── tests/
│   └── e2e/                 # end-to-end demo of the first-release workflow
│
├── docs/
│   └── operations.md        # runbooks
│
├── .env.example
├── README.md
└── roadmap.md
```

Database migrations live in `apps/api/migrations`; API tests in `apps/api/tests`.

## 27. Environment Variables

Example only; use secret injection in production.

``` dotenv
APP_ENV=production
APP_NAME=Nexora
PUBLIC_APP_URL=https://ai.company.com

DATABASE_URL=postgresql://...
REDIS_URL=redis://...

LITELLM_BASE_URL=http://litellm:4000
LITELLM_MASTER_KEY=<secret>

# One entry per Ollama node (Ollama has no authentication: never expose it publicly)
OLLAMA_DGX1_URL=http://127.0.0.1:10100
OLLAMA_DGX2_URL=http://<dgx2-private-address>:10101

JWT_SECRET=<secret>
ENCRYPTION_KEY=<secret>
```

Do not commit actual secrets. Use separate credentials for database,
LiteLLM administration, inference and the metrics scrape token.

## 28. Port and Exposure Matrix

  Service               Port Exposure
  ------------------- ------ ------------------
  Nginx                  443 Public
  Next.js               3000 Internal
  FastAPI               8001 Internal
  LiteLLM               4000 Internal
  PostgreSQL            5432 Internal
  Redis                 6379 Internal
  Ollama                  10100/10101 VPN/private only
  Prometheus            9090 Admin only
  Grafana               3001 Admin only

No internal service should be exposed directly through public firewall
rules.

## 29. Deployment Sequence

1.  Prepare application and inference hosts.
2.  Configure private networking and VPN.
3.  Deploy PostgreSQL.
4.  Apply database migrations.
5.  Deploy Redis.
6.  Deploy LiteLLM (with its own database; run its migrations on first start).
7.  Deploy Ollama on DGX Spark.
8.  Validate inference connectivity from the private network.
9.  Deploy the FastAPI service (management, gateway, scheduler, router).
10. Deploy Next.js.
11. Configure the administrator account and authentication.
12. Configure Nginx, DNS and TLS.
13. Deploy monitoring and alerting; confirm a backup and a restore test.
14. Run integration, load and security tests.
15. Enable developer onboarding.

# PART IX --- MONITORING, BACKUP AND OPERATIONS

## 30. Monitoring Requirements

Use Prometheus and Grafana.

Monitor:

-   GPU utilization.
-   GPU/unified memory usage.
-   Model health.
-   Model load time.
-   Requests/sec.
-   Input and output tokens/sec.
-   Active requests.
-   Queue depth.
-   Queue waiting time.
-   Time to first token.
-   Generation duration.
-   Failed and cancelled requests.
-   Routing decisions, failovers and per-deployment error rate.
-   Per-deployment queue depth, utilization and time to first token.
-   Disk capacity.
-   PostgreSQL connection and query health.
-   Redis health and memory.

Alert on:

-   Inference endpoint unavailable.
-   Model unhealthy.
-   Queue depth above threshold.
-   Queue latency above threshold.
-   Disk usage above 85%.
-   Memory pressure.
-   Repeated authentication failures.
-   Abnormal key usage.
-   Backup failure.

## 31. Backup and Recovery

PostgreSQL:

-   Daily full backups.
-   WAL archiving and point-in-time recovery where configured.
-   Encrypted off-host backup storage.
-   Periodic restore tests.

Redis:

-   Enable persistence appropriate to the deployment.
-   Treat Redis as recoverable coordination state, not the only durable
    record of requests or quota usage.
-   Rebuild queue state from PostgreSQL after failure.

Activity data:

-   Request and audit logs live in PostgreSQL and are covered by its backups.
-   Archived partitions (Phase 5) are encrypted, checksummed and restore-tested.
-   Log sinks are delivery channels, not a system of record.

Secrets:

-   Encrypted secret backup.
-   Restricted recovery access.
-   Document rotation after a suspected compromise.

Operations:

-   Document DGX Spark restart.
-   Document model reload.
-   Document gateway failover behavior.
-   Document scheduler recovery.
-   Document database restore.
-   Test the recovery process periodically.

------------------------------------------------------------------------

# PART X --- IMPLEMENTATION ROADMAP

## 32. Delivery Strategy

Build the central gateway first (done), then deepen what makes it valuable: trustworthy activity
logging, then routing optimization driven by that data. The first useful milestone was:

> An administrator creates a developer, assigns a project and quota, generates an API key, and the
> developer successfully connects OpenCode to the local DGX Spark through Nexora.

That workflow runs end to end today against the real LiteLLM proxy with a mock backend. Remaining
risk is concentrated in two places: behavior on the real DGX Spark and Ollama (Phase 0 items), and the
design of routing (Phase 6). Phases 5 and 6 can overlap: routing depends on the rollups and enriched
request log that Phase 5 introduces.

Estimates are planning figures, not delivery guarantees: Phase 5 about 1--2 weeks, Phase 6 about
2--3 weeks, Phase 7 about 1 week, with two experienced engineers and a reachable DGX Spark.

## 33. Phase 0 --- Technical Validation and Project Setup

**Status: partly done.** Done: LiteLLM-to-backend integration and streaming validated (LiteLLM 1.108.3
in front of a mock Ollama); both real Ollama nodes confirmed reachable, and the LiteLLM-to-Ollama path
verified on both with tiny requests (text, streaming usage, vision, per-deployment names, key
allow-lists); version and model-digest differences found between the nodes, repository skeleton, CI, migrations, coding standards. **Open:** confirm
DGX Spark OS/driver/CUDA, run the real Ollama nodes, benchmark the target models, confirm VPN connectivity and
public DNS. These block real capacity numbers and the routing defaults.

**Indicative duration:** 2--3 days

Tasks:

-   Confirm DGX Spark OS, driver, CUDA and container versions.
-   Confirm the Ollama version on both nodes and the OpenAI-compatible endpoints Nexora relies on
    (chat and completions, streaming, `stream_options.include_usage`, `/v1/models`).
-   Benchmark candidate coding model configurations.
-   Validate OpenAI-compatible streaming.
-   Validate LiteLLM-to-Ollama integration (provider adapter and OpenAI-compatible endpoints) on both
    nodes, including streaming and token usage in streamed responses.
-   Reconcile the two nodes: identical Ollama versions and identical model digests (today they differ),
    and decide which build is the reference.
-   Record each node's Ollama settings (`OLLAMA_NUM_PARALLEL`, `OLLAMA_MAX_LOADED_MODELS`,
    `OLLAMA_KEEP_ALIVE`, context length), installed models and available memory.
-   Measure cold-load times and steady-state time to first token and tokens/sec per model per node,
    alone and while the other workloads run (especially document extraction).
-   Confirm Ollama is bound to localhost / a private interface only, and reachable from the gateway
    host to DGX 2 and nowhere else.
-   Confirm private VPN connectivity.
-   Confirm public application host and DNS.
-   Create Git repository and development environments.
-   Establish coding standards, linting, migrations and CI.

Deliverables:

-   Validated inference endpoint.
-   Architecture decision records.
-   Repository skeleton.
-   Environment configuration.
-   Initial deployment diagram.
-   Model benchmark report.

Exit criteria:

-   Model serves successfully on DGX Spark.
-   LiteLLM can route a test request to Ollama.
-   Streaming works from a private client.
-   Public and private network boundaries are documented.

## 34. Phase 1 --- Foundation and Identity

**Status: implemented and tested.** Deferred (needs email or identity-provider infrastructure):
password reset and activation emails, MFA for administrators, OIDC/SSO.

**Indicative duration:** Week 1

Tasks:

-   Initialize Next.js and FastAPI projects.
-   Configure PostgreSQL and migrations.
-   Configure Redis.
-   Implement users and roles.
-   Implement login, logout and refresh.
-   Implement secure sessions.
-   Implement teams and memberships.
-   Implement projects and project memberships.
-   Implement backend authorization.
-   Build admin and developer navigation.
-   Create initial dashboard shells.
-   Add audit logging for privileged actions.

Deliverables:

-   Login and session management.
-   Super Admin, Team Admin and Developer roles.
-   User and team administration.
-   Project management.
-   Initial portal UI.

Exit criteria:

-   Role-based access tests pass.
-   Users cannot access unauthorized teams or projects.
-   Session revocation works.
-   Audit events are generated for privileged actions.

## 35. Phase 2 --- LiteLLM Gateway and API Keys

**Status: implemented; validated against the real LiteLLM proxy.** Lessons recorded in tests: LiteLLM
requires unique key aliases, and checks a key's model list against the model name in each request
(Nexora creates keys with the model's LiteLLM name, not its alias).

**Indicative duration:** Week 2

Tasks:

-   Deploy LiteLLM Proxy.
-   Connect LiteLLM to Ollama.
-   Register stable model aliases.
-   Integrate LiteLLM virtual key APIs.
-   Implement key creation and revocation.
-   Implement key-to-user/project mapping.
-   Implement model access policies.
-   Implement OpenAI-compatible `/v1/models`.
-   Implement chat completion proxying.
-   Validate streaming from OpenCode and other clients.
-   Add key usage display.
-   Add key expiry and rotation workflows.

Deliverables:

-   Working inference gateway.
-   Developer API key management.
-   Model selection.
-   OpenCode connection instructions.
-   Key revocation and rotation.

Exit criteria:

-   Developer key successfully authenticates.
-   Invalid and revoked keys are rejected.
-   Developer cannot access disallowed models.
-   OpenCode can connect through the gateway.
-   Master credentials are never returned to the client.

## 36. Phase 3 --- Quotas, Usage and FIFO Scheduler

**Status: implemented.** Verified with real Redis: concurrent admission order, atomic reservations,
cancellation (queued and running), client disconnects, lease expiry and recovery after a Redis wipe.
Not yet measured on real hardware: the safe default concurrency per deployment.

**Indicative duration:** Weeks 3--4

Tasks:

-   Define quota hierarchy and policy resolution.
-   Implement atomic quota reservations.
-   Implement request-count quotas.
-   Implement token-count quotas.
-   Implement concurrency limits.
-   Create inference request lifecycle records.
-   Implement Redis FIFO queues per model deployment.
-   Implement atomic queue claim.
-   Implement queue status API.
-   Implement cancellation.
-   Implement streaming proxy.
-   Implement timeout and stale-job recovery.
-   Reconcile actual token usage.
-   Add request history and CSV export.
-   Add scheduler metrics.

Deliverables:

-   Centralized FIFO scheduler.
-   Request and token quota enforcement.
-   Usage analytics.
-   Request history.
-   Queue visibility and cancellation.

Exit criteria:

-   FIFO ordering tests pass under concurrent submission.
-   Quota reservations prevent concurrent overrun.
-   Request cancellation works.
-   Scheduler recovers from restart.
-   Streaming is not buffered unnecessarily.
-   Every request is attributed to a user, project and key.

## 37. Phase 4 --- Admin Analytics and Operations

**Status: implemented.** Admin dashboard, health checks through LiteLLM, auto-resolving alerts,
Prometheus metrics, audit search, backup and restore-test scripts (exercised, including failure
paths). Not verified: Prometheus rules and the Grafana dashboard against running instances, DGX GPU
metrics, notification channels beyond in-app, WAL/PITR, off-host backup upload.

**Indicative duration:** Week 4--5, overlapping with Phase 3

Tasks:

-   Build admin dashboard.
-   Build developer usage dashboard.
-   Build team and project reports.
-   Build model management screens.
-   Build inference server health screen.
-   Build queue monitoring screen.
-   Integrate Prometheus.
-   Create Grafana dashboards.
-   Configure alerts.
-   Add audit-log search.
-   Add backup automation.
-   Add operational runbooks.

Deliverables:

-   Admin analytics.
-   Usage reports.
-   Inference monitoring.
-   Alerts and audit history.
-   Backup and recovery documentation.

Exit criteria:

-   Admin can inspect user, team and model usage.
-   Inference outages generate alerts.
-   Backup restore has been tested.
-   Queue and model health are visible.

## 38. Phase 5 --- Activity Logging Depth

**Indicative duration:** 1--2 weeks. **Status: planned.**

Tasks:

-   Enrich the request log: client IP, user agent and detected client tool, session id, correlation id
    propagated to LiteLLM and Ollama.
-   Audit hash chain, verifier job and `GET /admin/audit-logs/verify`.
-   Retention policies and monthly partitioning for `inference_requests`, `request_events` and
    audit data; archival of expired partitions to encrypted files.
-   Unified activity feed API and UI with role-scoped filters and request timelines.
-   Exports (CSV, JSON) and log sinks (webhook, syslog) with delivery retries and a dead-letter table.
-   Content-logging policy: default off, per-scope opt-in with justification, disclosure, redaction,
    field-level encryption, short retention, read auditing.
-   Anomaly and abuse alerts: unusual key use, quota probing.
-   Rollup job producing `deployment_metrics` (also required by Phase 6).
-   Cost-equivalent reporting against a configurable rate card.

Deliverables:

-   Complete, queryable, retention-managed request and audit records.
-   Verifiable audit trail.
-   Exports and integrations, and an explicit content-logging policy.

Exit criteria:

-   Every gateway request, including rejected and cancelled ones, has a request-log record with the
    new context fields.
-   Tampering with an audit row is detected by the verifier in a test.
-   Retention and archival run without losing or duplicating rows; restoring an archive works.
-   With content logging off (the default), no message content is persisted anywhere; with it on,
    content is encrypted, redacted, access-audited and expires.
-   Exports and sink payloads are free of secrets (tested with seeded fake secrets).

## 39. Phase 6 --- Routing Optimization

**Indicative duration:** 2--3 weeks. **Status: core implemented (8 November 2026); the rest is planned.**

Implemented and tested (including against real Redis, a fake LiteLLM/Ollama, and the real DGX nodes):
nodes and multi-deployment registry with weights, drain, enable, context limit and a per-node concurrency
budget; the router (fixed, least-loaded with residency awareness, weighted; deterministic, ties spread by
request hash); the Ollama node sampler (version, installed models and digests, loaded models) as the health
source for node-backed deployments; model-build equivalence (digest mismatch alert, optional reference build
that excludes stale deployments); decision records and the explain API; response headers naming the
deployment and model; failover to another deployment only when the failure looks like a connection error
and a probe confirms the node is down (otherwise never replayed); re-routing of queued requests off
unhealthy, draining or disabled deployments, in order; versioned policies; the freeze switch; the admin
Routing page.

Also implemented (9 November 2026): **workload classes and lanes** (`pms`, `coding`, `extraction`, `standard`),
assigned by a super admin to projects or keys, with reserved slots per deployment, priority-ordered borrowing of
idle capacity, a bounded hold on recently-active classes, class-aware routing scores, usage by class and the admin
class editor.

Still planned: latency-aware scoring from rollups, warm-up and keep-alive management, canary percentages,
rollups, recommendations, the replay simulator and the benchmark harness, tool-call and long-context validation
on the real nodes, and measuring DGX 2's parallelism in an agreed window (it carries production extraction).

Original task list: Requires at least two registered
deployments to demonstrate (they may share hardware initially, or use a second node when available).

Tasks:

-   Multi-deployment registry with node and per-node capacity budget, weights, tags, drain, enable (§6.1).
-   Node sampler: `/api/ps` residency, `/api/tags` inventory and model digests, Ollama version per node;
    digest-mismatch detection and the equivalence rule (§6.1); warm-up and keep-alive manager.
-   Workload classes on keys and projects, mapped to lanes with reserved capacity (§6.2).
-   Move the existing document-extraction service behind the gateway with its own key and `batch` class,
    pinned to DGX 2, then enable cross-node overflow in the stages described in §1.2.
-   Router module: health-aware, residency-aware, least-loaded, latency-aware, weighted and context-length-aware
    strategies with deterministic tie-breaking (§6.2).
-   Routing decision records and `explain` API (§6.3).
-   Failover before dispatch and re-routing of queued requests when a deployment turns unhealthy,
    never replaying a request whose outcome is unknown.
-   Optional priority lanes with reserved capacity; explicit, admin-approved fallback chains.
-   Policy versioning, canary percentage, rollback and the freeze switch (§6.6).
-   Per-deployment rollups, routing dashboards and alerts.
-   Recommendations engine with evidence, apply/dismiss workflow, and measured follow-up (§6.5).
-   Replay simulator and a benchmark harness (synthetic and replayed load).
-   Response headers naming the deployment and model that served each request.

Deliverables:

-   Routing across multiple deployments with explainable decisions.
-   Optimization recommendations and a simulator an administrator can trust and verify.
-   Safe rollout controls.

Exit criteria:

-   Load tests show routing improves p95 queue wait and time to first token over fixed routing with an
    uneven or degraded deployment.
-   Killing a deployment under load loses no requests and executes none twice.
-   Per-deployment FIFO is preserved under concurrent admission and re-routing.
-   Every routed request has a decision record that reproduces from its stored inputs.
-   Simulator error for the current configuration is within the agreed tolerance on a held-out window.
-   Freeze restores fixed routing within one routing cycle.

## 40. Phase 7 --- Production Hardening and Launch

**Indicative duration:** about 1 week. **Status: planned.**

Tasks:

-   Validate on the real DGX Spark and Ollama: model configuration, concurrency defaults, streaming
    through Nginx, long-context behavior; record the benchmark report.
-   Configure public DNS and HTTPS, WAF and ingress limits; complete network isolation.
-   Perform dependency and image security scans.
-   Perform authorization testing (broken object-level authorization, cross-team access).
-   Perform API key leakage testing (logs, exports, errors, metrics).
-   Perform quota race-condition, FIFO load, streaming disconnect and scheduler recovery tests on the
    real stack.
-   Perform database and LiteLLM-database restore tests; confirm alerting end to end.
-   Run the concurrent-developer benchmark and publish capacity guidance only from measured data.
-   Complete documentation and onboard pilot developers; collect feedback and fix critical defects.

Deliverables:

-   Production deployment, security review, load-test report, recovery runbooks and an onboarding guide.

Exit criteria:

-   All critical security tests pass.
-   Quotas, FIFO and routing behavior are verified on production-like hardware.
-   No public access to private inference or data services.
-   Monitoring, alerting and backup recovery work.
-   Pilot developers use the gateway from their own tools.

# PART XI --- TESTING AND ACCEPTANCE

## 41. Functional Testing

Test:

-   Login, logout, refresh, lockout and session revocation (MFA once implemented).
-   Role and project authorization.
-   API key creation, rotation and revocation (including at LiteLLM itself).
-   Model permission enforcement.
-   Request and token quotas; reservation and reconciliation.
-   FIFO queue order; queue cancellation; queue-full and timeout behavior.
-   Streaming responses, including clients that disconnect mid-stream.
-   Request history and usage attribution.
-   Activity feed scoping, exports and log sinks; retention and archival.
-   Audit hash-chain verification (including detecting edits and deletions).
-   Content logging off by default; policy, redaction, encryption and expiry when enabled.
-   Routing: each strategy, deterministic tie-breaking, failover, drain, canary arms, freeze,
    decision records and explanations.
-   Recommendations and the simulator against known synthetic workloads.

## 42. Security Testing

Test:

-   Broken object-level authorization and cross-team data access (requests, activity, decisions).
-   API key leakage (logs, exports, metrics, errors, audit entries).
-   Session theft and expiry; CSRF and XSS protections.
-   SQL injection; SSRF through webhook log sinks (block internal and metadata addresses).
-   Secret leakage in logs, exports and sink payloads (seed fake secrets and search for them).
-   CSV and log injection in exported fields.
-   Content-log access control and read auditing.
-   Excessive request and token consumption; quota probing.
-   Admin privilege escalation; routing and policy changes by non-admins.
-   Header spoofing of client identity, priority lane or deployment.

## 43. Load Testing

Benchmark with realistic client workloads (OpenCode and similar agents).

Test dimensions:

-   1, 2, 4, 8 and higher concurrent developers.
-   Short chat requests; long-context requests; long output requests; streaming requests.
-   Queue bursts; client disconnects; scheduler restarts.
-   Model or deployment unavailability, and recovery.
-   Uneven deployments (one slow, one failing) to evaluate each routing strategy.

Measure:

-   Requests/sec, input and output tokens/sec.
-   Queue wait time (p50/p95/p99), time to first token, end-to-end completion time.
-   Error and failover rate, duplicate-execution count (must be zero).
-   GPU/unified memory utilization; CPU and memory of the application tier.
-   Request-log write latency and rollup job duration.

Do not publish a developer capacity promise until the actual selected model and inference
configuration have been tested.

## 44. Definition of Done

Nexora is ready for organizational rollout when:

-   Developers can independently obtain API keys, and revoked keys are rejected everywhere.
-   User, team and key quotas are enforced; FIFO order is verified.
-   Streaming works with the supported clients.
-   Requests are fully attributable and usage reports are correct.
-   Activity logs are complete, retention-managed and tamper-evident; content logging is off unless
    deliberately enabled under policy.
-   Routing decisions are explainable, failover never duplicates execution, and routing can be frozen.
-   Admins can inspect queue, deployment health and routing decisions.
-   Public access is HTTPS-only; private services are inaccessible from the internet.
-   Backups and recovery are tested; monitoring and alerting are operational.
-   Behavior has been validated on the real DGX Spark and Ollama.

---

# PART XII --- ENGINEERING STANDARDS

## 45. Code Quality

-   TypeScript strict mode.
-   Python type hints.
-   Pydantic request/response validation.
-   Consistent API error handling.
-   Structured JSON logging.
-   Database migrations for schema changes.
-   Unit, integration and end-to-end tests.
-   Linting and formatting in CI.
-   Dependency pinning.
-   No secrets in source control.
-   Documented environment variables.
-   Small, reviewable pull requests.

## 46. Observability and Correlation

Use a common request correlation ID across:

-   Nginx.
-   FastAPI.
-   Scheduler.
-   LiteLLM.
-   Ollama.

The correlation ID must allow an administrator to trace an inference
request without exposing private prompts or secrets.

## 47. Reliability Principles

-   PostgreSQL is the durable source of truth.
-   Redis is recoverable queue and coordination state.
-   LiteLLM is the gateway and virtual-key provider.
-   Ollama is the inference engine.
-   Every state transition should be observable.
-   Every routing decision is recorded and explainable; routing can always be frozen to a fixed policy.
-   Every retry must be idempotent or explicitly classified as a new
    attempt.
-   Do not silently replay a model request when the original completion
    outcome is unknown.

## 48. Future Enhancements

After the core scope:

-   Multiple DGX Spark inference nodes beyond the first replica pair, and cross-site routing.
-   Auto-selection aliases with transparent, inspectable rules.
-   Per-team model preferences and team-level routing policies.
-   Additional local models and model evaluation/regression testing on logged traffic (with consent).
-   Speculative recommendations from cost-equivalent trends (capacity planning).
-   Prefix-cache and session-affinity routing if Ollama exposes the signals needed.
-   Email and chat notification channels for alerts.
-   SSO (OIDC) and administrator MFA.
-   Private document retrieval (RAG) as a separate, opt-in service.
-   Additional AI workloads beyond text generation (embeddings, speech).

# PART XIII --- BACKLOG

## 49. Epic Breakdown

| Epic | Priority | Key deliverables | Status |
|---|---|---|---|
| E01 --- Project Foundation | P0 | Repository, CI, configuration | Done |
| E02 --- Authentication | P0 | Login, sessions, roles | Done (MFA/SSO deferred) |
| E03 --- Organization | P0 | Teams, users, projects | Done |
| E04 --- Model Gateway | P0 | LiteLLM and Ollama integration | Done (real Ollama open) |
| E05 --- API Keys | P0 | Provisioning and revocation | Done |
| E06 --- Quotas | P0 | Atomic request/token controls | Done |
| E07 --- FIFO Scheduler | P0 | Queue, dispatch, cancellation, recovery | Done |
| E08 --- Usage Metering | P0 | Token and request attribution | Done |
| E09 --- Admin Portal | P0 | Users, models, queue, alerts, audit | Done |
| E10 --- Developer Portal | P0 | Keys, usage, request history | Done |
| E11 --- Monitoring | P0 | Metrics, alerts, backups | Done (Grafana/Prometheus unverified) |
| E12 --- Activity Logging | P0 | Enriched log, hash chain, retention, feed, exports, content policy | Planned (Phase 5) |
| E13 --- Routing | P0 | Multi-deployment router, decisions, failover, canary, freeze | Planned (Phase 6) |
| E14 --- Optimization | P1 | Rollups, recommendations, simulator, benchmark harness | Planned (Phase 6) |
| E15 --- Production Hardening | P0 | Real-hardware validation, security and load tests | Planned (Phase 7) |

## 50. Sprint Plan

Sprints 1--4 (foundation, gateway and keys, quotas and scheduler, analytics and operations) are
complete. Remaining:

### Sprint 5 --- Activity logging

-   Request-log enrichment and correlation ids.
-   Audit hash chain and verifier.
-   Rollup job and retention/partitioning.
-   Activity feed and exports.

### Sprint 6 --- Log policy and routing core

-   Content-logging policy, log sinks, anomaly alerts.
-   Multi-deployment registry; router with health-aware and least-loaded strategies; decision records.

### Sprint 7 --- Routing optimization

-   Latency-aware and weighted strategies, failover and queued re-routing, canary and freeze.
-   Recommendations and the replay simulator; routing dashboards.

### Sprint 8 --- Hardening

-   Real DGX Spark and Ollama validation, security and load testing, recovery testing.
-   Documentation, pilot rollout and launch.

Sprint assignments may overlap based on team size and dependencies.

---

# 51. First Release Priorities

**Status: demonstrated** (steps 1--18 below run in `tests/e2e/demo.py` against the real LiteLLM proxy
with a mock inference backend; repeat against the real DGX Spark in Phase 7).

The first end-to-end demonstration should be:

1.  Super Admin logs into Nexora.
2.  Super Admin creates a team.
3.  Super Admin creates a developer.
4.  Super Admin creates a project.
5.  Super Admin assigns the developer to the project.
6.  Super Admin assigns request and token quotas.
7.  Developer logs in.
8.  Developer generates an API key.
9.  Developer configures OpenCode with the Nexora endpoint.
10. Developer sends a coding request.
11. Nexora authenticates the key.
12. Scheduler checks quota and queues the request.
13. LiteLLM routes to Ollama on DGX Spark.
14. Response streams back to OpenCode.
15. Usage is recorded.
16. Developer sees consumption in the portal.
17. Admin sees the request in organization-wide reports.
18. When quota is exceeded, further requests are rejected with a clear
    error.

This is the minimum complete business workflow; it is the baseline that activity logging and
routing optimization build on.

------------------------------------------------------------------------

# 52. Final Architecture Decision Summary

| Decision | Selected approach |
|---|---|
| Product | Nexora |
| Purpose | Model access, activity logging, routing optimization |
| Access | OpenAI-compatible API gateway with virtual keys |
| Frontend | Next.js, React, TypeScript |
| Backend | FastAPI |
| Database | PostgreSQL |
| Queue | Redis-backed FIFO scheduler, one queue per deployment |
| Gateway | LiteLLM Proxy |
| Inference | Ollama on DGX Spark (more nodes later) |
| Routing | Deployment selection by health, load and latency; explainable, versioned, freezable |
| Optimization | Rollups, recommendations with evidence, replay simulator |
| Authentication | Secure sessions (MFA for admins planned) |
| API credentials | LiteLLM virtual keys, stored only as hashes |
| Quotas | Request and token limits, atomic reservation |
| Scheduling | FIFO per deployment queue |
| Logging | Per-request log, append-only audit with hash chain, content off by default |
| Network | Public HTTPS ingress, private service network |
| DGX access | Private VPN |
| Monitoring | Prometheus and Grafana, in-app alerts |
| Delivery | Gateway first, then activity depth, then routing |

**Final product objective:** Nexora will be the organization's single, secure and observable way to
use its own language models: every request attributable, every action logged, and every routing
decision explainable, with the data to keep improving how the hardware is used.
