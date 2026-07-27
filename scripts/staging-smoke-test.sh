#!/bin/sh
set -eu
compose="docker compose --env-file .env.staging -f docker-compose.yml -f docker-compose.staging.yml"
echo "[INFO] Starte idempotenten Smoke-Test ausschließlich mit DryRunPublisher"
$compose exec -T -e SMOKE_ADMIN_PASSWORD_FILE=/run/secrets/smoke_admin_password web python -m app.staging_smoke
$compose exec -T -e SMOKE_ADMIN_PASSWORD_FILE=/run/secrets/smoke_admin_password web python -m app.staging_smoke
echo "[OK] Zweiter Lauf blieb idempotent; keine Meta-Anfrage wurde ausgeführt."
