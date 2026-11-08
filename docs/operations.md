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
