#!/bin/sh
set -eu
out="${1:-backups/$(date -u +%Y%m%dT%H%M%SZ)}"; mkdir -p "$out"
docker compose exec -T db pg_dump -U socialmedia -Fc socialmedia > "$out/database.dump"
tar --exclude='*.secret' --exclude='.env' -czf "$out/files.tar.gz" data/generated data/uploads app/templates deploy docker-compose.yml docker-compose.prod.yml
printf 'Backup: %s\n' "$out"
