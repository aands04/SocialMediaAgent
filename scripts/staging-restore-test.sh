#!/bin/sh
set -eu
: "${1:?Aufruf: staging-restore-test.sh BACKUP-ORDNER}"
: "${RESTORE_TEST_DATABASE_URL:?RESTORE_TEST_DATABASE_URL muss auf eine LEERE Testdatenbank zeigen}"
backup="$1"
test -s "$backup/database.dump" && test -s "$backup/files.tar.gz"
count="$(psql "$RESTORE_TEST_DATABASE_URL" -Atc "select count(*) from pg_catalog.pg_tables where schemaname='public'")"
test "$count" = "0" || { echo "[FEHLER] Restore-Testdatenbank ist nicht leer" >&2; exit 1; }
pg_restore --dbname="$RESTORE_TEST_DATABASE_URL" --exit-on-error "$backup/database.dump"
for table in users teams media_assets posts publication_jobs audit_logs; do
  value="$(psql "$RESTORE_TEST_DATABASE_URL" -Atc "select count(*) from $table")"
  test "$value" -gt 0 || { echo "[FEHLER] $table enthält nach Restore keine Daten" >&2; exit 1; }
  echo "[OK] $table: $value"
done
temporary="$(mktemp -d)"; trap 'rm -rf "$temporary"' EXIT
tar -C "$temporary" -xzf "$backup/files.tar.gz"
find "$temporary/generated" -type f -size +0c | grep -q . || { echo "[FEHLER] Keine referenzierbaren Grafiken"; exit 1; }
echo "[OK] Restore in leere Testdatenbank und Dateiarchiv geprüft"
