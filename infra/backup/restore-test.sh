#!/usr/bin/env bash
# Prove a backup is restorable: restore into a throw-away database and run sanity checks.
#
#   ADMIN_URL=postgresql:///postgres ./restore-test.sh backups/nexora-<stamp>.dump
#
# ADMIN_URL must point at a maintenance database on a server where you may CREATE/DROP databases
# (use a scratch server for production-size restores). Encrypted backups: decrypt first
# (age -d -i key.txt X.age > X.dump). The restore target is always a NEW temporary database.
set -euo pipefail

BACKUP="${1:?usage: restore-test.sh <backup.dump>}"
: "${ADMIN_URL:?set ADMIN_URL, e.g. postgresql:///postgres}"
[ -f "$BACKUP" ] || { echo "no such file: $BACKUP" >&2; exit 2; }
case "$BACKUP" in *.age|*.gpg) echo "decrypt the backup first" >&2; exit 2;; esac

TMPDB="nexora_restore_test_$$_$(date +%s)"
write_metric() {
  [ -n "${METRICS_DIR:-}" ] || return 0
  mkdir -p "$METRICS_DIR"
  printf '# TYPE nexora_backup_restore_test_ok gauge\nnexora_backup_restore_test_ok %s\n' "$1" \
    > "$METRICS_DIR/nexora_restore_test.prom.$$" && mv "$METRICS_DIR/nexora_restore_test.prom.$$" "$METRICS_DIR/nexora_restore_test.prom"
}
cleanup() { rc=$?; psql "$ADMIN_URL" -qAtc "DROP DATABASE IF EXISTS \"$TMPDB\"" >/dev/null 2>&1 || true
  if [ $rc -eq 0 ]; then write_metric 1; else write_metric 0; echo "RESTORE TEST FAILED" >&2; fi; }
trap cleanup EXIT

if [ -f "$BACKUP.sha256" ]; then
  ( cd "$(dirname "$BACKUP")" && shasum -a 256 -c "$(basename "$BACKUP").sha256" ) >/dev/null \
    || { echo "CHECKSUM MISMATCH" >&2; exit 3; }
  echo "checksum ok"
fi

# Connection string for the temp database: the admin URL with its database name swapped.
case "$ADMIN_URL" in
  *://*/*) URL_T="${ADMIN_URL%/*}/$TMPDB" ;;
  *)       echo "ADMIN_URL must look like postgresql://[user@host]/postgres" >&2; exit 2 ;;
esac
psql "$ADMIN_URL" -qAtc "CREATE DATABASE \"$TMPDB\""
pg_restore --no-owner --no-privileges --exit-on-error --dbname="$URL_T" "$BACKUP"

q() { psql "$URL_T" -qAtc "$1"; }
fail() { echo "CHECK FAILED: $1" >&2; exit 1; }

[ "$(q "select count(*) from information_schema.tables where table_schema='public'")" -ge 10 ] || fail "too few tables"
[ -n "$(q "select version_num from alembic_version")" ] || fail "no alembic version"
[ "$(q "select count(*) from roles")" -ge 3 ] || fail "roles missing"
[ "$(q "select count(*) from users")" -ge 1 ] || fail "no users"
[ "$(q "select count(*) from pg_trigger where tgname='audit_logs_no_change'")" = 1 ] || fail "audit immutability trigger missing"
# the restored audit log must still refuse edits
if q "update audit_logs set action = action" >/dev/null 2>&1; then fail "audit_logs is modifiable after restore"; fi
echo "restore test ok: $(q "select count(*) from users") users, $(q "select count(*) from inference_requests") requests," \
     "$(q "select count(*) from audit_logs") audit rows, schema $(q "select version_num from alembic_version")"
