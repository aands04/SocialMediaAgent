#!/bin/sh
set -eu
: "${STAGING_BACKUP_ROOT:?STAGING_BACKUP_ROOT fehlt}"
compose="docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"; target="$STAGING_BACKUP_ROOT/$stamp"; temporary="$target.tmp"
mkdir -p "$temporary"
$compose exec -T db pg_dump -U socialmedia -d socialmedia -Fc > "$temporary/database.dump"
tar -C "${STAGING_DATA_ROOT:?STAGING_DATA_ROOT fehlt}" -czf "$temporary/files.tar.gz" generated uploads provider-snapshots logs
cp docker-compose.yml docker-compose.staging.yml .env.staging.example "$temporary/"
printf '{"at":"%s","backup":"%s"}\n' "$stamp" "$target" > "$temporary/manifest.json"
mv "$temporary" "$target"
printf '{"at":"%s","backup":"%s"}\n' "$stamp" "$target" > "$STAGING_BACKUP_ROOT/last-success.json"
# Marker visible to monitoring via backup bind if configured/copied by operator.
printf '[OK] Backup %s\n' "$target"
