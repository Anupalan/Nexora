# Nexora operations runbook

What is implemented and verified is stated plainly; anything not yet exercised against production
hardware is marked **unverified**.

## Monitoring map
| Signal | Where | Notes |
|---|---|---|
| In-app alerts | Portal → Alerts, `GET /admin/alerts` | Raised and auto-resolved every `HEALTH_CHECK_INTERVAL_SECONDS` (30s) |
| Prometheus metrics | `GET /metrics` | Disabled (404) until `METRICS_TOKEN` is set; send `Authorization: Bearer <token>` |
| GPU / unified memory | DGX Spark DCGM exporter | Scraped by Prometheus directly, not by Nexora (**unverified** on a real DGX Spark) |
| Host disk / memory | node_exporter | Alert rules in `infra/monitoring/alerts.yml` |
| Backups | `nexora_backup_*` textfile metrics | Written by `infra/backup/*.sh` when `METRICS_DIR` is set |

In-app alert types: `inference_unavailable` (critical), `queue_depth_high`, `queue_wait_high`,
`quota_approaching` (≥90% of a day/month limit), `quota_exceeded`, `repeated_auth_failures`
(≥10 failed sign-ins in 10 minutes). Thresholds: `ALERT_*` settings in `.env.example`. Dismissing an
alert by hand is audited; if the condition persists it re-opens on the next cycle.
Not yet implemented: email/other notification channels, disk/memory in-app
alerts (use the Prometheus rules), suspicious-key-usage detection.

## Backups (PostgreSQL is the durable source of truth)
```bash
DATABASE_URL=postgresql:///nexora BACKUP_DIR=/var/backups/nexora KEEP_DAYS=14 \
  AGE_RECIPIENT=age1... METRICS_DIR=/var/lib/node_exporter/textfile infra/backup/backup.sh
```
Daily from cron/systemd. The script dumps in custom format, verifies the dump is readable, encrypts
(age or gpg, if configured), writes a SHA-256, prunes by age, and publishes success/failure metrics.
**You must copy the output off-host to encrypted storage** — the script does not upload.
WAL archiving / point-in-time recovery is not configured by this repo; enable it at the Postgres level
if your RPO is tighter than a day.

### Restore test (run weekly, and after any change to the backup process)
```bash
age -d -i key.txt backup.dump.age > backup.dump        # if encrypted
ADMIN_URL=postgresql:///postgres METRICS_DIR=... infra/backup/restore-test.sh backup.dump
```
Restores into a throw-away database (never the live one), verifies the checksum, the schema version,
roles/users, and that the audit log is still append-only, then drops it. Exit 3 = checksum mismatch.
`BackupStale` and `RestoreTestFailed` fire if it is not run or fails.

### Restoring for real
1. Stop the API and scheduler (so nothing writes), keep Redis running.
2. Create an empty database and `pg_restore --no-owner --dbname=<new> backup.dump`.
3. Point `DATABASE_URL` at it, run `alembic upgrade head` (no-op if the dump is current).
4. Flush Redis (`redis-cli flushall`) — counters and queues are **rebuilt from Postgres on API
   startup** (`ensure_counters`), and in-flight requests found in neither place are failed with
   `scheduler_restart` (never replayed).
5. Start the API; check `/health/ready` and the Alerts page.

## Scheduler and Redis
- Redis holds only recoverable coordination state (queues, leases, quota counters).
- After a Redis restart or wipe: restart the API (or wait for the sweeper); counters are rebuilt from
  `inference_requests`. Requests that were queued/running at that moment are marked `failed` with
  `scheduler_restart` or `lease_expired` — clients should retry; Nexora never silently replays a
  request whose outcome is unknown.
- A queue that appears stuck: Portal → Request Queue shows depth/active per deployment. A request whose
  client vanished is removed by the sweeper (`waiter_lost`); a dead worker's lease expires after
  `LEASE_SECONDS` (60s).
- Tune concurrency per model: Portal → Request Queue → Edit, or `PATCH /admin/deployments/{id}`.
  Keep it low until benchmarked on the real DGX Spark.

## DGX Spark / vLLM (**unverified on real hardware**)
- Restart: restart the vLLM service on the Spark, wait for the model to load, then
  Portal → Inference Servers → *Check now* (or wait 30s). The `inference_unavailable` alert clears itself.
- Model reload / change: update the model in LiteLLM's `model_list`, restart LiteLLM, and make sure the
  model registered in Nexora uses the same `model_name` as "LiteLLM model". Existing keys are created
  with that name; after renaming a model, rotate or update affected keys.
- While a model is unhealthy, requests still queue (up to the queue limit/timeout) and then fail with
  an upstream error; consider disabling the model (Portal → Models) to fail fast with 404.

## LiteLLM gateway
- Verified against LiteLLM 1.108.3 (see README). Needs its own Postgres database; run its migrations on
  first start. Back up its database too — virtual keys live there.
- If LiteLLM is down: new keys cannot be created (502), `/v1` requests fail with 502
  `upstream_unavailable`, and the model shows unhealthy. Nexora's own login, usage and admin pages keep
  working. Revoking a key while LiteLLM is down still blocks it in Nexora's gateway immediately; the
  LiteLLM-side delete is recorded in the audit log (`litellm_deleted: false`) — re-run the revoke after
  recovery to clean up.
- Failover to a second LiteLLM instance is not implemented.

## Secrets
Rotate on suspected compromise: `LITELLM_MASTER_KEY` (then update both LiteLLM and the API), database
and Redis credentials, `METRICS_TOKEN`, and the age/gpg backup keys (re-encrypt retained backups).
A leaked developer key: revoke it in the portal (kills it in Nexora and in LiteLLM) and issue a new one.
Back up secrets separately, encrypted, with restricted recovery access.

## Routine checklist
- Daily: Alerts page empty or understood; backup metric fresh.
- Weekly: run `restore-test.sh` on the latest backup; review Audit Logs for privileged changes.
- Per release: `alembic upgrade head` on a copy first; `tests/e2e/demo.py` against staging.

## Routing and nodes (Phase 6 core)
- **Add a node:** `POST /admin/nodes` (`name`, `runtime_url` = the Ollama base URL, optional `max_concurrent`),
  then `POST /admin/deployments` per model on it (`runtime_model` = the Ollama tag, `litellm_model` = the name
  LiteLLM serves it under, `max_active_requests` at or below the node's `OLLAMA_NUM_PARALLEL`). New
  deployment names are pushed onto existing API keys automatically; check `keys_failed` in the response.
  Ollama has no authentication: keep it reachable only from the gateway host.
- **Take a node out of service gracefully:** *Drain* its deployments on the Routing page. New requests avoid it,
  queued ones move to other deployments, running ones finish. Disable it when idle.
- **Something looks wrong:** *Freeze routing* forces the fixed strategy everywhere at once; *Resume* undoes it.
  Both are audited. To see why a request went where it did: `GET /routing/explain/{request_id}`.
- **Digest mismatch alert:** the same model tag is different builds on different nodes. Pull the same build on
  both (`ollama pull`), re-sample, then optionally set a reference build in the model's policy so a stale node
  is excluded automatically.
- **Cold models:** a request to a node without the model loaded pays a load (seconds to minutes). The router
  avoids that while a warm node has capacity; set `load_seconds` on the deployment to your measured value.
- **Failover is conservative on purpose:** it needs a connection-style error *and* a probe showing the node
  down. Any other failure is returned to the client rather than replayed.

## Workload lanes
- **Assign a class:** Projects page (project) or API Keys page (a single key); both super admin only. A key's class
  overrides its project's; unassigned traffic is `standard`. The extraction service should have its own key with class
  `extraction`; the PMS chatbot and PMS tool use `pms`; vibe-coding clients use `coding`.
- **Tune reservations:** Routing page, *Workload classes* (percent per class, priority number, hold window). Percentages
  must add up to at most 100. Classes cannot be deleted, only set to 0%. With very few slots a small class rounds to zero
  reserved slots (it still gets priority at request boundaries).
- **Slots must match the node.** Measure a node's real parallelism first (send N identical requests concurrently: if the
  total is about N times one request, it is serial). Set each deployment's `max_active_requests` to that number. If it
  is larger than Ollama really runs, requests queue inside Ollama where lanes cannot reorder them. DGX 1 measured serial
  (parallelism 1) for the 8B model on 9 Nov 2026; DGX 2 has not been measured.
- **What lanes cannot do:** they never interrupt a running request, so with one slot a chat request waits for the current
  request to finish (seconds for chat-sized work, longer for very long generations). Keep extraction requests
  reasonably sized, or raise `OLLAMA_NUM_PARALLEL` for true reserved capacity.
- **See it working:** the Routing page shows running/reserved and waiting requests per class on each deployment; Usage
  has a breakdown and filter by class; `GET /routing/explain/{id}` shows the class and its lane numbers.
