#!/usr/bin/env bash
# Nexora PostgreSQL backup: custom-format dump, optional encryption, checksum, retention, metric.
#
#   DATABASE_URL=postgresql:///nexora BACKUP_DIR=/var/backups/nexora ./backup.sh
#
# Optional: AGE_RECIPIENT=age1...  or  GPG_RECIPIENT=ops@company.com   (encrypt before leaving the host)
#           KEEP_DAYS=14                       (delete older local backups)
#           METRICS_DIR=/var/lib/node_exporter/textfile   (writes nexora_backup_*.prom for Prometheus)
# Copy the result off-host to encrypted storage (this script does not do the upload).
set -euo pipefail

: "${DATABASE_URL:?set DATABASE_URL (a libpq URI or postgresql:///dbname)}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$BACKUP_DIR/nexora-$STAMP.dump"
mkdir -p "$BACKUP_DIR"
umask 077

write_metric() {  # $1 = 1 on success, 0 on failure
  [ -n "${METRICS_DIR:-}" ] || return 0
  mkdir -p "$METRICS_DIR"
  {
    echo "# TYPE nexora_backup_last_run_ok gauge"
    echo "nexora_backup_last_run_ok $1"
    if [ "$1" = 1 ]; then
      echo "# TYPE nexora_backup_last_success_timestamp_seconds gauge"
      echo "nexora_backup_last_success_timestamp_seconds $(date -u +%s)"
      echo "# TYPE nexora_backup_size_bytes gauge"
      echo "nexora_backup_size_bytes $(wc -c < "$FINAL" | tr -d ' ')"
    fi
  } > "$METRICS_DIR/nexora_backup.prom.$$" && mv "$METRICS_DIR/nexora_backup.prom.$$" "$METRICS_DIR/nexora_backup.prom"
}
FINAL="$OUT"
trap 'rc=$?; [ $rc -eq 0 ] || { rm -f "$OUT" "$OUT.age" "$OUT.gpg"; write_metric 0; echo "backup FAILED" >&2; }' EXIT

# Passwords come from PGPASSWORD / ~/.pgpass, never from the command line.
pg_dump --format=custom --no-owner --no-privileges --dbname="$DATABASE_URL" --file="$OUT"
pg_restore --list "$OUT" > /dev/null   # the dump must at least be readable

if [ -n "${AGE_RECIPIENT:-}" ]; then
  age -r "$AGE_RECIPIENT" -o "$OUT.age" "$OUT" && rm -f "$OUT" && FINAL="$OUT.age"
elif [ -n "${GPG_RECIPIENT:-}" ]; then
  gpg --batch --yes --trust-model always -r "$GPG_RECIPIENT" -o "$OUT.gpg" --encrypt "$OUT" && rm -f "$OUT" && FINAL="$OUT.gpg"
fi

( cd "$BACKUP_DIR" && shasum -a 256 "$(basename "$FINAL")" > "$(basename "$FINAL").sha256" )
find "$BACKUP_DIR" -maxdepth 1 -name 'nexora-*' -type f -mtime "+$KEEP_DAYS" -delete
write_metric 1
echo "backup ok: $FINAL ($(wc -c < "$FINAL" | tr -d ' ') bytes)"
