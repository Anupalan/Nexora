# Nexora --- Complete Product Roadmap & Technical Specification

**Version:** 1.0\
**Date:** 8 November 2026\
**Status:** Approved for implementation planning\
**Product type:** Self-hosted, multi-user AI inference gateway and
agentic coding platform\
**Primary inference hardware:** NVIDIA DGX Spark, 128 GB unified memory\
**Primary stack:** Next.js, React, TypeScript, FastAPI, PostgreSQL,
Redis, LiteLLM, vLLM, Docker\
**Deployment model:** Public HTTPS application with private application,
inference and workspace networks

------------------------------------------------------------------------

## 1. Executive Summary

Nexora is a centralized internal AI platform that allows developers
to use organization-hosted language models through two access channels:

1.  **OpenAI-compatible API gateway** for OpenCode, Cline, Goose, Aider
    and other compatible clients.
2.  **Browser-based coding workspace** with repository management, AI
    chat, code editing, terminal execution, task tracking, diffs and Git
    workflows.

The platform centralizes developer identity, team and project access,
API key provisioning, model access, request scheduling, request/token
quotas, usage metering, audit logging, monitoring and administration.

The DGX Spark is the private inference host. Developers must not connect
directly to vLLM. All inference requests pass through authenticated
gateway and scheduling controls.

The product must be modular so additional DGX Spark systems, model
deployments and inference workers can be added without redesigning the
portal.

## 2. Product Goals

### 2.1 Primary goals

-   Provide a secure shared local-LLM service for multiple developers.
-   Give every developer individually attributable API keys.
-   Support OpenAI-compatible clients with minimal configuration
    changes.
-   Enforce request-count and token-count limits at global, team, user
    and key levels.
-   Provide a durable FIFO request queue with queue visibility and
    cancellation.
-   Measure token consumption, latency, throughput, failures and queue
    wait time.
-   Provide a custom browser-based coding workspace.
-   Isolate repositories, credentials, terminals and agent execution
    environments.
-   Give administrators a single control plane for users, teams,
    projects, models, keys, quotas and infrastructure.
-   Keep inference data within the organization's controlled
    infrastructure.

### 2.2 Non-goals for the initial release

-   Training or fine-tuning models.
-   Public multi-tenant SaaS billing.
-   Replacing GitHub as the source-control platform.
-   Building a general-purpose cloud IDE with every feature of VS Code.
-   Supporting arbitrary model runtimes beyond the defined provider
    abstraction.
-   Allowing unrestricted shell access to the host.
-   Guaranteeing a fixed number of concurrent developers before load
    testing.

## 3. Success Metrics

Track these product and operational metrics:

  -----------------------------------------------------------------------
  Area                                Metric
  ----------------------------------- -----------------------------------
  Availability                        Portal, gateway and inference
                                      availability

  Access                              Successful authentication and key
                                      provisioning

  Quotas                              Correct rejection and reservation
                                      under concurrency

  Scheduling                          FIFO ordering correctness and queue
                                      wait time

  Inference                           Requests/sec, tokens/sec, time to
                                      first token

  Reliability                         Request success rate and retry rate

  Attribution                         Percentage of requests with user,
                                      team, project and key attribution

  Workspace                           Workspace creation success and
                                      isolation test results

  Developer experience                Task completion, workspace usage
                                      and developer feedback

  Operations                          Backup success, restore test
                                      success and alert response
  -----------------------------------------------------------------------

Do not use tokens/sec alone as a measure of coding-agent productivity.
Measure completed tasks, test pass rates, tool-call reliability and time
to completion.

------------------------------------------------------------------------

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
-   View team workspace and task activity.

### 4.3 Developer

-   Access assigned projects.
-   Create and manage personal API keys.
-   Select permitted models.
-   Use the API gateway from compatible coding clients.
-   Use the browser coding workspace.
-   View personal usage, quota consumption and request history.
-   Create coding tasks and inspect results.
-   Review and approve code changes before Git operations.

### 4.4 Authorization principles

-   Enforce permissions on the backend, not only in the frontend.
-   Deny access by default.
-   Apply team and project membership checks to every resource.
-   Prevent users from reading another user's keys, prompts,
    repositories, workspaces or task output.
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
-   Connect a project to a Git repository.
-   Assign project-level roles.
-   Set project-specific model permissions and quota policies.
-   View project usage and agent tasks.

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
-   Map aliases to LiteLLM model names and vLLM deployments.
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
-   Do not claim that vLLM itself guarantees FIFO scheduling. The
    application scheduler controls admission order; vLLM handles
    inference batching and execution.
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
-   Source: API gateway or coding workspace.
-   Input tokens.
-   Output tokens.
-   Total tokens.
-   Queue wait time.
-   Time to first token.
-   Generation duration.
-   Status and error category.
-   Created, started and completed timestamps.
-   Idempotency key where applicable.

Separate these concepts:

-   **Inference request:** one model/API request.
-   **Agent task:** one user-assigned coding task.
-   **Agent execution:** one agent run or retry.
-   **Tool execution:** one shell, file, Git or other tool operation.

A single agent task can create many inference requests and tool
executions. Never count a coding task as only one model request.

Analytics:

-   Usage by user, team, project, API key and model.
-   Input/output token trends.
-   Requests per minute/day/month.
-   Queue depth and wait time.
-   Time to first token.
-   Generation throughput.
-   Success and failure rates.
-   Active users and workspaces.
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
-   Workspace worker unavailable.
-   Disk or memory pressure.
-   Repeated authentication failures.
-   Suspicious API-key usage.

Support in-app notifications initially. Add email or other channels
later.

------------------------------------------------------------------------

# PART II --- CUSTOM CODING WORKSPACE

## 6. Coding Workspace Requirements

The browser workspace is a first-class product module.

### 6.1 Workspace layout

-   Left panel: project, repository, branch and file explorer.
-   Center panel: Monaco code editor with tabs and diff view.
-   Right panel: AI agent conversation, task progress and approvals.
-   Bottom panel: terminal, test results and execution logs.

### 6.2 Project and repository integration

-   Connect GitHub repositories.
-   Support repository selection and branch selection.
-   Clone into an isolated workspace.
-   Store credentials using encrypted, scoped secret storage.
-   Never expose repository tokens to the browser or LLM prompt.
-   Enforce project membership.
-   Record repository and branch used for each task.

GitHub integration should use least-privilege installation/app
credentials where practical.

### 6.3 Agent task lifecycle

Suggested lifecycle:

`PENDING → QUEUED → RUNNING → REVIEW_REQUIRED → APPROVED → COMMITTED`

Additional terminal states:

-   FAILED
-   CANCELLED
-   REJECTED
-   EXPIRED

A task may have multiple executions. Each execution must retain its
model requests, tool events, logs and final diff references.

### 6.4 Agent capabilities

Initial tools:

-   List and read files.
-   Search repository content.
-   Create and edit files.
-   Apply patches.
-   Run approved shell commands.
-   Run tests and linters.
-   Inspect Git status and diff.
-   Create a branch.
-   Commit changes after approval.
-   Push changes after explicit approval.

### 6.5 Workspace isolation

-   One isolated container or equivalent execution environment per
    active workspace.
-   Non-root execution.
-   CPU, RAM, disk and process limits.
-   No privileged containers.
-   No Docker socket mounted inside agent containers.
-   No host filesystem access outside approved workspace mounts.
-   Restrict outbound network access.
-   Block access to cloud metadata services and internal management
    networks.
-   Use short-lived scoped repository credentials.
-   Record commands and execution results.
-   Destroy or suspend inactive workspaces according to policy.

A separate Linux workspace worker is preferred over running untrusted
agent commands on the DGX Spark.

### 6.6 Code review and Git controls

-   Show changed files and unified diffs.
-   Allow per-file review.
-   Require user approval before commit.
-   Require explicit approval before push.
-   Show test results.
-   Preserve task and execution history.
-   Support rollback by creating a new corrective commit or reverting a
    commit.
-   Never silently overwrite user changes.

------------------------------------------------------------------------

# PART III --- TECHNICAL ARCHITECTURE

## 7. Technology Stack

  -----------------------------------------------------------------------
  Layer                   Technology              Responsibility
  ----------------------- ----------------------- -----------------------
  Frontend                Next.js, React,         Portal and workspace UI
                          TypeScript              

  Editor                  Monaco Editor           Browser code editing

  Backend                 Python FastAPI          Management and
                                                  orchestration APIs

  Database                PostgreSQL              Persistent business
                                                  data

  Cache/Queue             Redis                   FIFO queue, counters,
                                                  events and locks

  LLM gateway             LiteLLM Proxy           Virtual keys, model
                                                  routing and usage

  Inference               vLLM                    Local model serving

  Execution               Docker                  Isolated coding
                                                  workspaces

  Reverse proxy           Nginx                   TLS and routing

  Monitoring              Prometheus/Grafana      Metrics and dashboards

  Logging                 Structured application  Troubleshooting and
                          logs                    audit correlation
  -----------------------------------------------------------------------

### 7.1 Service boundaries

1.  **Frontend:** Presentation and user interaction.
2.  **FastAPI management service:** Identity, teams, projects, keys,
    quotas, reports and administration.
3.  **Scheduler service:** Admission control, FIFO queue, reservations,
    cancellation and request lifecycle.
4.  **LiteLLM:** Virtual-key verification, model routing and provider
    usage integration.
5.  **vLLM:** Model inference on DGX Spark.
6.  **Workspace service:** Workspace lifecycle and controlled execution.
7.  **Agent worker:** Agent loop and tool execution.
8.  **PostgreSQL:** Durable source of truth.
9.  **Redis:** Fast coordination and transient state.

Avoid circular dependencies. The scheduler and workspace worker should
call well-defined internal APIs rather than directly modifying unrelated
service tables.

## 8. High-Level Architecture

``` text
Developers
  |-- Browser
  |-- OpenCode / Cline / Goose / Aider
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
    Redis FIFO Scheduler
          |
    LiteLLM Proxy
          |
    Private VPN / Application Network
          |
    DGX Spark
          |
         vLLM
          |
      Local Model

Workspace path:
Next.js -> FastAPI -> Workspace Service -> Isolated Docker Worker
                                      |
                                  Agent Runtime
                                      |
                        Scheduler / LiteLLM / vLLM
```

All model requests, including workspace agent requests, must be
attributable to a user and project.

## 9. Public and Private Network Design

Publicly expose only the HTTPS ingress.

Private services:

-   FastAPI internal endpoints.
-   PostgreSQL.
-   Redis.
-   LiteLLM administration.
-   vLLM.
-   Workspace control plane.
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
  DGX Spark/vLLM     Private VPN network
  Workspace worker   Isolated execution network

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
-   Workspace egress restrictions.
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
-   repository_url
-   default_branch
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

### 10.5 Coding workspace

#### `workspaces`

-   id
-   user_id
-   project_id
-   repository_id
-   container_id
-   status
-   resource_profile
-   created_at
-   last_active_at
-   destroyed_at

#### `repositories`

-   id
-   project_id
-   provider
-   repository_url
-   default_branch
-   credential_reference
-   created_at

#### `repository_credentials`

-   id
-   user_id
-   project_id
-   provider
-   secret_reference
-   status
-   created_at
-   expires_at

#### `agent_tasks`

-   id
-   workspace_id
-   user_id
-   project_id
-   title
-   prompt_reference
-   status
-   created_at
-   completed_at

#### `agent_executions`

-   id
-   task_id
-   attempt_number
-   model_id
-   status
-   started_at
-   completed_at
-   result_reference

#### `tool_executions`

-   id
-   execution_id
-   tool_name
-   status
-   command_reference
-   output_reference
-   started_at
-   completed_at

#### `workspace_events`

-   id
-   workspace_id
-   event_type
-   event_data JSONB
-   created_at

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
  POST     `/projects/{id}/repositories`        Connect repository
  GET      `/projects/{id}/repositories`        List repositories

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

## 19. Workspace and Agent APIs

  Method   Endpoint                           Purpose
  -------- ---------------------------------- ---------------------------
  POST     `/workspaces`                      Create workspace
  GET      `/workspaces`                      List user's workspaces
  GET      `/workspaces/{id}`                 Workspace status
  DELETE   `/workspaces/{id}`                 Stop/destroy workspace
  GET      `/workspaces/{id}/files`           Browse files
  GET      `/workspaces/{id}/files/content`   Read file
  PUT      `/workspaces/{id}/files/content`   Save file
  POST     `/workspaces/{id}/terminal`        Execute permitted command
  POST     `/agent/tasks`                     Submit coding task
  GET      `/agent/tasks/{id}`                Task details
  POST     `/agent/tasks/{id}/cancel`         Cancel task
  GET      `/agent/tasks/{id}/events`         Task event stream
  GET      `/agent/tasks/{id}/diff`           View changes
  POST     `/agent/tasks/{id}/approve`        Approve changes
  POST     `/agent/tasks/{id}/reject`         Reject changes
  POST     `/agent/tasks/{id}/commit`         Commit changes
  POST     `/agent/tasks/{id}/push`           Push approved changes

Terminal endpoints must not be general host-shell proxies. They must
execute only inside the authorized isolated workspace.

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
9.  Insert request into its deployment FIFO queue.
10. Return request identifier and queue state.

### 21.2 Execution flow

1.  Scheduler checks available inference capacity.
2.  Atomically claims the oldest queued request.
3.  Marks request as processing.
4.  Forwards it to LiteLLM.
5.  LiteLLM routes to vLLM.
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

### 21.4 FIFO and vLLM batching

FIFO applies to scheduler admission order. vLLM may batch and schedule
token generation internally. Do not promise strict token-by-token
completion ordering across concurrent requests.

Initial configuration must be benchmarked on the actual model and
runtime. Start conservatively with a small concurrency limit, then
increase it after measuring memory, latency and throughput.

------------------------------------------------------------------------

# PART VII --- FRONTEND DESIGN

## 22. Developer Portal

Navigation:

-   Dashboard
-   Coding Workspace
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
-   Workspaces
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

## 24. Coding Workspace UI

Layout:

-   Left: repository tree, branch selector and Git status.
-   Center: Monaco editor, tabs and diff viewer.
-   Right: AI chat, agent plan, task status and approval actions.
-   Bottom: terminal, build output, tests and logs.

Features:

-   Create task from natural-language prompt.
-   Select project and repository.
-   Select branch.
-   Show agent plan where supported.
-   Stream progress events.
-   Inspect file changes.
-   Review diff.
-   Run tests.
-   Approve/reject changes.
-   Commit and push only after explicit user action.

Use WebSockets or Server-Sent Events for task and workspace updates. Use
authentication and authorization for every event subscription.

------------------------------------------------------------------------

# PART VIII --- DEPLOYMENT AND INFRASTRUCTURE

## 25. Deployment Topology

### Public ingress

-   Nginx.
-   TLS certificates.
-   WAF or equivalent edge protection.
-   Public DNS.
-   Ingress request-size and rate limits.

### Application server

-   Next.js.
-   FastAPI.
-   Scheduler.
-   LiteLLM.
-   PostgreSQL.
-   Redis.
-   Monitoring agents.

### DGX Spark

-   vLLM.
-   Local model files.
-   Private VPN interface.
-   GPU and model metrics.

### Workspace execution host

-   Workspace control service.
-   Docker runtime.
-   Isolated containers.
-   Resource enforcement.
-   Restricted network egress.

Prefer a separate execution host for untrusted agent commands. If the
initial MVP uses the application server, enforce strong container
isolation and resource limits.

## 26. Suggested Repository Structure

``` text
nexora/
├── apps/
│   ├── web/                 # Next.js portal and workspace
│   ├── api/                 # FastAPI management APIs
│   ├── scheduler/           # FIFO admission and dispatch
│   ├── workspace-service/   # Workspace lifecycle API
│   └── agent-worker/        # Agent runtime and tools
│
├── packages/
│   ├── api-client/
│   ├── shared-types/
│   └── ui/
│
├── infra/
│   ├── docker/
│   ├── nginx/
│   ├── litellm/
│   ├── vllm/
│   ├── wireguard/
│   ├── monitoring/
│   └── compose/
│
├── database/
│   ├── migrations/
│   ├── seeds/
│   └── documentation/
│
├── tests/
│   ├── integration/
│   ├── load/
│   ├── security/
│   └── end-to-end/
│
├── docs/
│   ├── architecture.md
│   ├── api.md
│   ├── deployment.md
│   ├── security.md
│   └── operations.md
│
├── .env.example
├── README.md
└── ROADMAP.md
```

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

VLLM_BASE_URL=http://dgx-spark-private-ip:8000/v1

JWT_SECRET=<secret>
ENCRYPTION_KEY=<secret>

WORKSPACE_SERVICE_URL=http://workspace-service:8002
```

Do not commit actual secrets. Use separate credentials for database,
LiteLLM administration, inference, Git provider access and workspace
services.

## 28. Port and Exposure Matrix

  Service               Port Exposure
  ------------------- ------ ------------------
  Nginx                  443 Public
  Next.js               3000 Internal
  FastAPI               8001 Internal
  LiteLLM               4000 Internal
  PostgreSQL            5432 Internal
  Redis                 6379 Internal
  vLLM                  8000 VPN/private only
  Workspace service     8002 Internal
  Prometheus            9090 Admin only
  Grafana               3001 Admin only

No internal service should be exposed directly through public firewall
rules.

## 29. Deployment Sequence

1.  Prepare application and workspace hosts.
2.  Configure private networking and VPN.
3.  Deploy PostgreSQL.
4.  Apply database migrations.
5.  Deploy Redis.
6.  Deploy LiteLLM.
7.  Deploy vLLM on DGX Spark.
8.  Validate inference connectivity from the private network.
9.  Deploy FastAPI.
10. Deploy scheduler.
11. Deploy Next.js.
12. Configure administrator account and authentication.
13. Deploy workspace service and isolated workers.
14. Configure Nginx, DNS and TLS.
15. Deploy monitoring and alerting.
16. Run integration, load and security tests.
17. Enable developer onboarding.

------------------------------------------------------------------------

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
-   Workspace count.
-   Container CPU and RAM.
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
-   Workspace worker failure.
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

Repositories and workspaces:

-   Push approved work to Git.
-   Do not rely on workspace containers as the only copy of source
    changes.
-   Define inactive workspace cleanup.
-   Preserve task metadata and approved diffs according to retention
    policy.

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

Build the central gateway first. The first useful milestone is:

> An administrator creates a developer, assigns a project and quota,
> generates an API key, and the developer successfully connects OpenCode
> to the local DGX Spark through Nexora.

After this milestone, build the custom browser workspace on the same
identity, quota, scheduler and inference infrastructure.

Estimated MVP: **5--8 weeks with two experienced engineers**, assuming
inference infrastructure and an application host are available. This is
a planning estimate, not a delivery guarantee.

## 33. Phase 0 --- Technical Validation and Project Setup

**Indicative duration:** 2--3 days

Tasks:

-   Confirm DGX Spark OS, driver, CUDA and container versions.
-   Validate selected vLLM version and model compatibility.
-   Benchmark candidate coding model configurations.
-   Validate OpenAI-compatible streaming.
-   Validate LiteLLM-to-vLLM integration.
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
-   LiteLLM can route a test request to vLLM.
-   Streaming works from a private client.
-   Public and private network boundaries are documented.

## 34. Phase 1 --- Foundation and Identity

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

**Indicative duration:** Week 2

Tasks:

-   Deploy LiteLLM Proxy.
-   Connect LiteLLM to vLLM.
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

## 38. Phase 5 --- Coding Workspace Foundation

**Indicative duration:** Weeks 5--6

Tasks:

-   Implement workspace lifecycle API.
-   Implement repository connection.
-   Implement scoped Git credentials.
-   Implement isolated Docker workspace provisioning.
-   Implement resource limits.
-   Implement file browser.
-   Integrate Monaco editor.
-   Implement secure file APIs.
-   Implement terminal execution inside workspace.
-   Implement workspace event streaming.
-   Implement workspace cleanup.

Deliverables:

-   Browser-based project workspace.
-   Repository browsing.
-   Code editor.
-   Terminal.
-   Isolated execution environment.

Exit criteria:

-   Developers can access only assigned repositories.
-   Workspaces are isolated.
-   Terminal commands execute inside the correct container.
-   Workspace resources are limited.
-   Workspace cleanup works.

## 39. Phase 6 --- AI Agent and Git Workflow

**Indicative duration:** Weeks 6--7

Tasks:

-   Implement agent task lifecycle.
-   Implement agent worker.
-   Integrate model gateway.
-   Implement repository-aware context loading.
-   Implement file read/search/write tools.
-   Implement patch application.
-   Implement controlled shell execution.
-   Implement test execution.
-   Implement task event stream.
-   Implement diff viewer.
-   Implement approval/rejection workflow.
-   Implement branch and commit workflow.
-   Implement explicit push approval.
-   Record inference requests and tool executions per task.

Deliverables:

-   AI coding agent.
-   Task progress view.
-   Multi-file editing.
-   Test execution.
-   Diff review.
-   Approved Git commits.

Exit criteria:

-   Agent can complete a controlled multi-file coding task.
-   Every model request is attributable to the task and developer.
-   User can inspect and reject changes.
-   No commit or push occurs without the required approval.
-   Agent cannot escape its workspace.

## 40. Phase 7 --- Production Hardening and Launch

**Indicative duration:** Week 8

Tasks:

-   Configure public DNS and HTTPS.
-   Configure WAF and ingress limits.
-   Complete network isolation.
-   Perform dependency and image security scans.
-   Perform authorization testing.
-   Perform workspace escape testing.
-   Perform API key leakage testing.
-   Perform quota race-condition tests.
-   Perform FIFO load tests.
-   Perform streaming disconnect tests.
-   Perform scheduler recovery tests.
-   Perform database restore test.
-   Perform concurrent developer benchmark.
-   Complete documentation.
-   Onboard pilot developers.
-   Collect feedback and fix critical defects.

Deliverables:

-   Production deployment.
-   Security review.
-   Load-test report.
-   Recovery runbooks.
-   Developer onboarding guide.

Exit criteria:

-   All critical security tests pass.
-   Quotas and FIFO behavior are verified.
-   No public access to private inference or data services.
-   Monitoring and backup recovery work.
-   Pilot developers can use both API and browser workspace.

------------------------------------------------------------------------

# PART XI --- TESTING AND ACCEPTANCE

## 41. Functional Testing

Test:

-   Login, logout, refresh and MFA.
-   Role and project authorization.
-   API key creation, rotation and revocation.
-   Model permission enforcement.
-   Request and token quotas.
-   Quota reservation and reconciliation.
-   FIFO queue order.
-   Queue cancellation.
-   Streaming responses.
-   Request history.
-   Usage attribution.
-   Workspace creation and cleanup.
-   Repository access.
-   File operations.
-   Terminal execution.
-   Agent task lifecycle.
-   Diff approval.
-   Git commit and push approval.

## 42. Security Testing

Test:

-   Broken object-level authorization.
-   Cross-team data access.
-   API key leakage.
-   Session theft and expiry.
-   CSRF and XSS protections.
-   SQL injection.
-   SSRF.
-   Workspace container escape.
-   Host filesystem access.
-   Docker socket exposure.
-   Cloud metadata access.
-   Internal network scanning from workspace.
-   Secret leakage in logs.
-   Excessive request and token consumption.
-   Admin privilege escalation.

## 43. Load Testing

Benchmark with realistic OpenCode and agent workloads.

Test dimensions:

-   1, 2, 4, 8 and higher concurrent developers.
-   Short chat requests.
-   Long-context requests.
-   Long output requests.
-   Streaming requests.
-   Multiple concurrent agent tasks.
-   Queue bursts.
-   Client disconnects.
-   Scheduler restarts.
-   Model unavailability.

Measure:

-   Requests/sec.
-   Input and output tokens/sec.
-   Queue wait time.
-   Time to first token.
-   End-to-end completion time.
-   Error rate.
-   GPU/unified memory utilization.
-   CPU and memory consumption.
-   Workspace resource usage.

Do not publish a developer capacity promise until the actual selected
model and inference configuration have been tested.

## 44. Definition of Done

Nexora MVP is ready for organizational rollout when:

-   Developers can independently obtain API keys.
-   Revoked keys are rejected.
-   User, team and key quotas are enforced.
-   FIFO order is verified.
-   Streaming works with supported clients.
-   Requests are fully attributable.
-   Usage reports are correct.
-   Workspace isolation passes security tests.
-   Git changes require approval.
-   Admins can inspect queue and inference health.
-   Public access is HTTPS-only.
-   Private services are inaccessible from the internet.
-   Backups and recovery are tested.
-   Monitoring and alerting are operational.

------------------------------------------------------------------------

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
-   vLLM.
-   Workspace service.
-   Agent worker.

The correlation ID must allow an administrator to trace an inference
request without exposing private prompts or secrets.

## 47. Reliability Principles

-   PostgreSQL is the durable source of truth.
-   Redis is recoverable queue and coordination state.
-   LiteLLM is the gateway and virtual-key provider.
-   vLLM is the inference engine.
-   Workspace workers are isolated and disposable.
-   Agent tasks and inference requests are separate entities.
-   Every state transition should be observable.
-   Every retry must be idempotent or explicitly classified as a new
    attempt.
-   Do not silently replay a model request when the original completion
    outcome is unknown.

## 48. Future Enhancements

After the MVP:

-   Multiple DGX Spark inference nodes.
-   Model-aware load balancing.
-   Per-model queue tuning.
-   Additional local models.
-   Team-level model preferences.
-   Advanced agent profiles.
-   Reusable project instructions.
-   MCP server integrations.
-   GitHub issue-to-agent workflows.
-   Pull request creation.
-   Scheduled agent tasks.
-   Team cost-equivalent reporting.
-   Advanced prompt and tool governance.
-   Model evaluation and regression testing.
-   Multi-node inference monitoring.
-   Optional private document retrieval/RAG.
-   Additional AI workloads beyond coding.

------------------------------------------------------------------------

# PART XIII --- INITIAL PROJECT BACKLOG

## 49. Epic Breakdown

  Epic                             Priority   Key deliverables
  -------------------------------- ---------- ----------------------------------
  E01 --- Project Foundation       P0         Repositories, CI, configuration
  E02 --- Authentication           P0         Login, sessions, roles
  E03 --- Organization             P0         Teams, users, projects
  E04 --- Model Gateway            P0         LiteLLM and vLLM integration
  E05 --- API Keys                 P0         Provisioning and revocation
  E06 --- Quotas                   P0         Atomic request/token controls
  E07 --- FIFO Scheduler           P0         Queue, dispatch and cancellation
  E08 --- Usage Metering           P0         Token and request attribution
  E09 --- Admin Portal             P0         User, model and queue management
  E10 --- Developer Portal         P0         Keys, usage and request history
  E11 --- Monitoring               P0         Metrics, alerts and logs
  E12 --- Workspace Service        P1         Isolated environments
  E13 --- Repository Integration   P1         GitHub connection
  E14 --- Code Editor              P1         Monaco and file management
  E15 --- Agent Runtime            P1         Agent loop and tool execution
  E16 --- Diff and Approval        P1         Review and Git workflow
  E17 --- Production Hardening     P0         Security, backups and load tests

P0 items are required for the gateway MVP and production foundation. P1
items complete the custom coding workspace.

## 50. Initial Sprint Plan

### Sprint 1

-   Repository and CI setup.
-   PostgreSQL schema.
-   FastAPI foundation.
-   Next.js foundation.
-   Authentication.
-   Roles and permissions.
-   Teams and projects.

### Sprint 2

-   LiteLLM integration.
-   vLLM model registration.
-   API key provisioning.
-   Key revocation.
-   OpenAI-compatible model listing and completion.
-   OpenCode integration.

### Sprint 3

-   Quota policy resolution.
-   Atomic quota reservations.
-   Redis FIFO queue.
-   Request lifecycle.
-   Streaming.
-   Cancellation and recovery.

### Sprint 4

-   Usage dashboards.
-   Request history.
-   Admin screens.
-   Monitoring and alerts.
-   Load testing and gateway MVP acceptance.

### Sprint 5

-   Workspace service.
-   Docker isolation.
-   Repository integration.
-   File explorer and Monaco.

### Sprint 6

-   Agent runtime.
-   File and terminal tools.
-   Task progress.
-   Diff and approval.
-   Git integration.

### Sprint 7--8

-   Security hardening.
-   Performance tuning.
-   Recovery testing.
-   Pilot rollout.
-   Documentation and production launch.

Sprint assignments may overlap based on team size and dependencies.

------------------------------------------------------------------------

# 51. First Release Priorities

The first release must prioritize the shared inference platform rather
than the coding UI.

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
13. LiteLLM routes to vLLM on DGX Spark.
14. Response streams back to OpenCode.
15. Usage is recorded.
16. Developer sees consumption in the portal.
17. Admin sees the request in organization-wide reports.
18. When quota is exceeded, further requests are rejected with a clear
    error.

This is the minimum complete business workflow and should be
demonstrated before investing heavily in the browser coding workspace.

------------------------------------------------------------------------

# 52. Final Architecture Decision Summary

  Decision          Selected approach
  ----------------- -----------------------------------------------
  Product           Nexora
  Access            API gateway + browser coding workspace
  Frontend          Next.js, React, TypeScript
  Backend           FastAPI
  Database          PostgreSQL
  Queue             Redis-backed FIFO scheduler
  Gateway           LiteLLM Proxy
  Inference         vLLM on DGX Spark
  Coding model      Configurable local coding model
  Workspace         Isolated Docker execution
  Authentication    Secure sessions, MFA for admins
  API credentials   LiteLLM virtual keys
  Quotas            Request and token limits
  Scheduling        FIFO per model deployment
  Usage             Per request, user, team, project and key
  Network           Public HTTPS ingress, private service network
  DGX access        Private VPN
  Monitoring        Prometheus and Grafana
  Delivery          Gateway MVP first, coding workspace second

**Final product objective:** Nexora will be the organization's
centralized, secure, multi-user AI development infrastructure. It will
provide controlled access to local LLMs, transparent usage accounting,
fair FIFO admission, isolated coding workspaces and an extensible
foundation for future internal AI applications.
